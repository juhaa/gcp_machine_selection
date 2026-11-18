#!/usr/bin/env python3
"""Show current Google Cloud Spot VM prices and recommend the cheapest machine type.

Prices are scraped from https://cloud.google.com/spot-vms/pricing. The page embeds
the pricing tables for every region in a JSON data blob, so one download covers all
regions. Parsed prices are cached for a day (see --refresh).

With --preemption, historical Spot preemption rates are fetched with
`gcloud beta compute advice capacity-history` and used to estimate the real cost
including reruns of preempted work.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRICING_URL = "https://cloud.google.com/spot-vms/pricing?hl=en"
CACHE_FILE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "gcp-spot-prices" / "prices.json"
PREEMPTION_CACHE_FILE = CACHE_FILE.with_name("preemption.json")
CACHE_TTL = 24 * 3600
PREEMPTION_MIN_DAYS = 7  # machines with fewer days of preemption data aren't recommended
MAX_PREEMPTION_QUERIES = 60  # gcloud calls per run, one per machine type and region
GCLOUD_WORKERS = 8
CACHE_VERSION = 2  # bump when the cached data format changes
HOURS_PER_MONTH = 730  # what Google uses for the monthly prices on the page

DEFAULT_REGION = "europe-west1"
DEFAULT_FAMILIES = ["n1", "n2"]
DEFAULT_TYPES = ["standard", "highmem", "highcpu", "custom"]
ARM_FAMILIES = {"c4a", "n4a", "t2a"}
# Families with simultaneous multithreading (SMT) disabled: each vCPU is a whole physical
# core. Elsewhere a vCPU is one hyperthread, i.e. half a core.
NO_SMT_FAMILIES = {"c4a", "h3", "n4a", "t2a", "t2d"}
# Families that can't use Persistent Disk (pd-*) and need Hyperdisk instead.
HYPERDISK_ONLY_FAMILIES = {"c4", "c4a", "c4d", "c4n", "m4", "n4", "n4a", "n4d", "z4d", "z4m"}
NOTE_LEGEND = {
    "Arm": "Arm CPU; needs arm64 (or multi-arch) container images.",
    "Hyperdisk": "no Persistent Disk support; Google Batch's default pd-balanced boot disk "
                 "won't work, set the boot disk type to hyperdisk-balanced.",
}
PREEMPTION_LEGEND = (
    "Preemption: share of Spot VMs of the type in the region that ended by preemption rather than\n"
    "  by their owner, averaged over the days with data in the last ~30 days (and last 7 days).\n"
    "Est. USD/hour: hourly price plus the expected cost of rerunning preempted work, assuming each\n"
    "  run is preempted with that probability and loses half a run: price * (1 + p / (2 * (1 - p))).\n"
    f"  Machines with fewer than {PREEMPTION_MIN_DAYS} days of data aren't recommended; custom machine\n"
    "  types usually have none."
)

# Custom machine type limits, from
# https://cloud.google.com/compute/docs/instances/creating-instance-with-custom-machine-type
#   vcpus:        allowed vCPU counts
#   mem_per_vcpu: (min, max) GiB per vCPU; memory above max is billed as extended memory
#   max_memory:   total GiB limit when using extended memory
# Extended memory is only offered where the pricing page lists a Spot price for it.
CUSTOM_LIMITS = {
    "n1": {"vcpus": [1] + list(range(2, 97, 2)), "mem_per_vcpu": (0.9, 6.5), "max_memory": 624},
    "n2": {"vcpus": list(range(2, 33, 2)) + list(range(36, 81, 4)), "mem_per_vcpu": (0.5, 8), "max_memory": 864},
    "n2d": {"vcpus": [2, 4, 8, 16, 32, 48, 64, 80, 96], "mem_per_vcpu": (0.5, 8), "max_memory": 768},
    "n4": {"vcpus": list(range(2, 81, 2)), "mem_per_vcpu": (2, 8), "max_memory": 640},
    "n4d": {"vcpus": [2, 4, 8, 16, 32, 48, 64, 80, 96], "mem_per_vcpu": (0.5, 8), "max_memory": 768},
    "n4a": {"vcpus": list(range(1, 65)), "mem_per_vcpu": (2, 8), "max_memory": 512},
    "e2": {"vcpus": list(range(2, 33, 2)), "mem_per_vcpu": (0.5, 8), "max_memory": 256},
}
MEMORY_STEP = 0.25  # custom memory must be a multiple of 256 MB

DATA_BLOB_RE = re.compile(
    r"AF_initDataCallback\(\{key: '[^']*', hash: '[^']*', data:(.*?), sideChannel: \{\}\}\);</script>", re.S
)
TAG_RE = re.compile(r"<[^>]+>")
REGION_RE = re.compile(r"\(([a-z]+-[a-z]+\d+)\)\s*$")
HOURLY_PRICE_RE = re.compile(r"\$([\d,]*\.?\d+)\s*/\s*1\s*(?:gibibyte\s+)?hour\b")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# --------------------------------------------------------------------------- fetching


def fetch_page() -> str:
    req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode("utf-8")


def iter_tables(node):
    """Yield pricing tables found in the page's data blob.

    A table looks like [.., [region labels], .., [per-region entries], .., table_id].
    """
    if not isinstance(node, list):
        return
    if (
        len(node) >= 4
        and isinstance(node[-1], str)
        and isinstance(node[1], list)
        and node[1]
        and isinstance(node[1][0], str)
        and REGION_RE.search(node[1][0])
        and isinstance(node[3], list)
    ):
        yield node
    for child in node:
        yield from iter_tables(child)


def cell_text(cell) -> str:
    try:
        return html.unescape(TAG_RE.sub("", cell[3][1][1])).replace("\xa0", " ").strip()
    except (IndexError, TypeError):
        return ""


def parse_number(text: str) -> float | None:
    m = NUMBER_RE.search(text)
    return float(m.group(0).replace(",", "")) if m else None


def parse_hourly_price(text: str) -> float | None:
    m = HOURLY_PRICE_RE.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def split_machine_name(name: str) -> tuple[str, str]:
    """n2-highmem-8 -> ("n2", "highmem"); c3-standard-4-lssd -> ("c3", "standard-lssd")."""
    family, *rest = name.split("-")
    return family, "-".join(p for p in rest if not p.isdigit())


def custom_rate_key(item: str) -> str | None:
    item = item.lower()
    if "extended" in item:
        return "extended"
    if "vcpu" in item:
        return "vcpu"
    if "memory" in item:
        return "memory"
    return None


def parse_prices(page: str) -> dict:
    """Return {"regions": {code: label}, "machines": [...], "custom": {region: {family: rates}}}."""
    regions = {}
    machines = {}
    custom = {}
    for blob_match in DATA_BLOB_RE.finditer(page):
        try:
            blob = json.loads(blob_match.group(1))
        except ValueError:
            continue
        for table in iter_tables(blob):
            for entry in table[3]:
                try:
                    header = [cell_text(c) for c in entry[0][0][0]]
                    rows, region_label = entry[1], entry[2]
                except (IndexError, TypeError):
                    continue
                region_match = REGION_RE.search(region_label or "")
                if not header or not region_match:
                    continue
                region = region_match.group(1)
                kind = header[0].lower()

                if kind == "item":
                    # Custom machine rates: "Custom vCPUs", "Custom Memory", "Extended custom memory".
                    family = table[-1].strip("﻿").split("-")[0]
                    for row in rows:
                        cells = [cell_text(c) for c in row[0]]
                        key = custom_rate_key(cells[0]) if cells else None
                        price = parse_hourly_price(cells[-1]) if cells else None
                        if key and price is not None:
                            custom.setdefault(region, {}).setdefault(family, {}).setdefault(key, price)
                    continue

                # Otherwise keep only predefined machine type tables (skips e.g. GPU tables).
                if not kind.startswith(("machine type", "vm shape")):
                    continue
                regions[region] = region_label
                for row in rows:
                    cells = [cell_text(c) for c in row[0]]
                    if len(cells) < 4 or not cells[0]:
                        continue
                    price = parse_hourly_price(cells[-1])
                    vcpus, memory = parse_number(cells[1]), parse_number(cells[2])
                    if price is None or vcpus is None or memory is None:
                        continue  # monthly price entry or unparseable row
                    name = cells[0].split()[0]
                    family, mtype = split_machine_name(name)
                    machines.setdefault((region, name), {
                        "region": region,
                        "name": name,
                        "family": family,
                        "type": mtype,
                        "vcpus": int(vcpus),
                        "memory": memory,
                        "hourly": price,
                    })
    return {"regions": regions, "machines": list(machines.values()), "custom": custom}


def load_prices(refresh: bool) -> dict:
    cached = None
    try:
        cached = json.loads(CACHE_FILE.read_text())
        if cached.get("version") != CACHE_VERSION:
            cached = None
        elif not refresh and time.time() - cached["fetched"] < CACHE_TTL:
            return cached
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        cached = None

    print(f"Fetching prices from {PRICING_URL} ...", file=sys.stderr)
    try:
        data = parse_prices(fetch_page())
    except (urllib.error.URLError, OSError) as e:
        if cached:
            print(f"warning: fetch failed ({e}); using cached prices", file=sys.stderr)
            return cached
        sys.exit(f"error: could not fetch pricing page: {e}")
    if not data["machines"]:
        sys.exit("error: no prices found in the pricing page; its layout may have changed")

    data["version"] = CACHE_VERSION
    data["fetched"] = time.time()
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data))
    except OSError as e:
        print(f"warning: could not write cache {CACHE_FILE}: {e}", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- custom machine types


def round_up(value: float, step: float) -> float:
    return math.ceil(value / step - 1e-9) * step


def cheapest_custom(region: str, family: str, rates: dict, cpu: int | None, mem: float | None) -> dict | None:
    """Cheapest valid custom machine of a family with >= cpu vCPUs and >= mem GiB, or None."""
    limits = CUSTOM_LIMITS.get(family)
    if not limits or "vcpu" not in rates or "memory" not in rates:
        return None
    min_ratio, max_ratio = limits["mem_per_vcpu"]
    extended_rate = rates.get("extended")
    best = None
    for vcpus in limits["vcpus"]:
        if cpu is not None and vcpus < cpu:
            continue
        # N1 needs at least 1 GiB per vCPU with 1 or 2 vCPUs.
        ratio = 1.0 if family == "n1" and vcpus <= 2 else min_ratio
        memory = round_up(max(mem or 0, ratio * vcpus), MEMORY_STEP)
        base = min(memory, max_ratio * vcpus)
        extended = memory - base
        if extended and (extended_rate is None or memory > limits["max_memory"]):
            continue
        hourly = vcpus * rates["vcpu"] + base * rates["memory"] + extended * (extended_rate or 0)
        if best is None or hourly < best["hourly"]:
            # gcloud machine type name; N1 custom types have no series prefix.
            prefix = "" if family == "n1" else f"{family}-"
            best = {
                "region": region,
                "name": f"{prefix}custom-{vcpus}-{round(memory * 1024)}" + ("-ext" if extended else ""),
                "family": family,
                "type": "custom",
                "vcpus": vcpus,
                "memory": memory,
                "extended": extended,
                "hourly": hourly,
            }
    return best


def custom_options(custom_rates: dict, regions: list[str], cpu: int | None, mem: float | None) -> list[dict]:
    """Cheapest custom machine per family and region meeting the requirements."""
    options = []
    for region in regions:
        for family, rates in custom_rates.get(region, {}).items():
            best = cheapest_custom(region, family, rates, cpu, mem)
            if best:
                options.append(best)
    return options


# --------------------------------------------------------------------------- preemption history


class PreemptionError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal  # a setup problem that makes every query fail


def query_preemption(region: str, machine_type: str) -> list[list]:
    """Return [[start, end, rate], ...] preemption history for a machine type in a region."""
    cmd = [
        "gcloud", "beta", "compute", "advice", "capacity-history", "--provisioning-model=SPOT",
        f"--machine-type={machine_type}", "--types=PREEMPTION", f"--region={region}", "--format=json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    except FileNotFoundError:
        raise PreemptionError("gcloud not found; install the Google Cloud CLI", fatal=True) from None
    except subprocess.TimeoutExpired:
        raise PreemptionError("gcloud timed out") from None
    if proc.returncode != 0:
        if "components: [beta]" in proc.stderr:
            raise PreemptionError("gcloud beta commands are not installed; run: gcloud components install beta",
                                  fatal=True)
        lines = proc.stderr.strip().splitlines()
        errors = [line for line in lines if line.startswith("ERROR")]
        raise PreemptionError((errors or lines or [f"gcloud exited with status {proc.returncode}"])[0])
    history = json.loads(proc.stdout or "{}").get("preemptionHistory", [])
    return sorted(
        [e["interval"]["startTime"], e["interval"]["endTime"], e.get("preemptionRate", 0.0)] for e in history
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def preemption_stats(history: list[list]) -> dict:
    """Average the daily preemption rates over all days with data and over the last 7 days.

    For machine types with little usage (e.g. custom types) the API returns a single entry
    spanning the whole period instead of daily values; such entries are not counted as data.
    """
    daily = [(parse_time(s), rate) for s, e, rate in history if parse_time(e) - parse_time(s) <= timedelta(days=2)]
    recent_start = datetime.now(timezone.utc) - timedelta(days=7)
    recent = [rate for start, rate in daily if start >= recent_start]
    return {
        "days": len(daily),
        "rate": sum(rate for _, rate in daily) / len(daily) if daily else None,
        "rate_7d": sum(recent) / len(recent) if recent else None,
    }


def preemption_key(m: dict) -> str:
    return f"{m['region']}/{m['name']}"


def load_preemption_cache() -> dict:
    try:
        cache = json.loads(PREEMPTION_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in cache.items() if isinstance(v, dict) and now - v.get("fetched", 0) < CACHE_TTL}


def save_preemption_cache(cache: dict) -> None:
    try:
        PREEMPTION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREEMPTION_CACHE_FILE.write_text(json.dumps(cache))
    except OSError as e:
        print(f"warning: could not write cache {PREEMPTION_CACHE_FILE}: {e}", file=sys.stderr)


def fetch_preemption(machines: list[dict], cache: dict) -> list[tuple[str, str]]:
    """Set m["preemption"] for each machine (None if the query failed), querying gcloud in
    parallel for those not cached. Returns the failed queries as (region, error message).
    """
    missing = [m for m in machines if preemption_key(m) not in cache]
    failures = {}  # error message -> machines
    if missing:
        noun = "machine type" if len(missing) == 1 else "machine types"
        print(f"Checking preemption history of {len(missing)} {noun} with gcloud ...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=GCLOUD_WORKERS) as pool:
            futures = {pool.submit(query_preemption, m["region"], m["name"]): m for m in missing}
            for future in as_completed(futures):
                m = futures[future]
                try:
                    cache[preemption_key(m)] = {"fetched": time.time(), "history": future.result()}
                except PreemptionError as e:
                    if e.fatal:
                        sys.exit(f"error: {e}")
                    failures.setdefault(str(e), []).append(m)
        for message, failed in failures.items():
            names = ", ".join(f"{m['name']} in {m['region']}" for m in failed)
            print(f"warning: no preemption history for {names}: {message}", file=sys.stderr)
    for m in machines:
        entry = cache.get(preemption_key(m))
        m["preemption"] = preemption_stats(entry["history"]) if entry else None
    return [(m["region"], message) for message, failed in failures.items() for m in failed]


def exit_if_setup_problem(failures: list[tuple[str, str]], any_success: bool, single_region: bool) -> None:
    """Exit when no query has worked and the failures can't be blamed on one region.

    Some regions can fail for a project (e.g. "resource ... is not ready") while others work,
    so failures in a single region of a multi-region search are only warned about.
    """
    if failures and not any_success and (single_region or len({region for region, _ in failures}) > 1):
        sys.exit(f"error: preemption history query failed: {failures[0][1]}")


def has_preemption_data(m: dict) -> bool:
    stats = m.get("preemption")
    return bool(stats) and stats["days"] >= PREEMPTION_MIN_DAYS


def effective_price(m: dict) -> float | None:
    """Hourly price plus the expected cost of rerunning preempted work, or None without data.

    Treats the preemption rate p as the chance that a run is preempted before it finishes and
    assumes a preempted run loses half a run on average: the expected number of preempted runs
    is p / (1 - p), so the expected cost is price * (1 + p / (2 * (1 - p))).
    """
    stats = m.get("preemption")
    if not stats or stats["rate"] is None:
        return None
    p = stats["rate"]
    return math.inf if p >= 1 else m["hourly"] * (1 + p / (2 * (1 - p)))


def check_preemption(by_price: list[dict], min_rows: int, cache: dict) -> tuple[int, float]:
    """Fetch preemption data for candidates in order of hourly price until no unchecked one can
    beat the best estimated price (the estimate is never below the hourly price).

    Returns how many candidates were checked and the best estimated price among those with data.
    """
    checked, best = 0, math.inf
    failures, any_success = [], False
    single_region = len({m["region"] for m in by_price}) == 1
    while checked < min(len(by_price), MAX_PREEMPTION_QUERIES):
        if checked >= min_rows and by_price[checked]["hourly"] >= best:
            break
        batch = by_price[checked:min(checked + GCLOUD_WORKERS, MAX_PREEMPTION_QUERIES)]
        batch_failures = fetch_preemption(batch, cache)
        failures += batch_failures
        any_success = any_success or len(batch_failures) < len(batch)
        exit_if_setup_problem(failures, any_success, single_region)
        checked += len(batch)
        best = min([best] + [effective_price(m) for m in batch if has_preemption_data(m)])
    return checked, best


# --------------------------------------------------------------------------- output


def vcpus_per_core(family: str) -> int:
    return 1 if family in NO_SMT_FAMILIES else 2


def cores(m: dict) -> float:
    return m["vcpus"] / vcpus_per_core(m["family"])


def core_price(m: dict) -> float:
    return m["hourly"] / cores(m)


def family_notes(family: str) -> list[str]:
    notes = []
    if family in ARM_FAMILIES:
        notes.append("Arm")
    if family in HYPERDISK_ONLY_FAMILIES:
        notes.append("Hyperdisk")
    return notes


def describe(m: dict, show_region: bool) -> str:
    notes = family_notes(m["family"])
    tags = f" [{', '.join(notes)}]" if notes else ""
    where = f" in {m['region']}" if show_region else ""
    extended = f" incl. {m['extended']:g} GiB extended" if m.get("extended") else ""
    text = (
        f"{m['name']}{tags}{where}: {m['vcpus']} vCPUs ({cores(m):g} cores), {m['memory']:g} GiB{extended}, "
        f"${m['hourly']:.6f}/hour (${m['hourly'] * HOURS_PER_MONTH:.2f}/month, ${core_price(m):.6f}/core/hour)"
    )
    if "preemption" in m:
        stats = m["preemption"]
        if stats is None:
            text += "\n    preemption history query failed"
        elif stats["days"]:
            recent = f", last 7 days {stats['rate_7d']:.0%}" if stats["rate_7d"] is not None else ""
            text += (
                f"\n    preempted {stats['rate']:.0%} ({stats['days']} days of data{recent}), "
                f"est. ${effective_price(m):.6f}/hour with reruns"
            )
        else:
            text += "\n    no preemption data"
    return text


def print_table(headers: list[str], rows: list[list[str]], align: str) -> None:
    """Print rows as aligned columns; align has an 'l' or 'r' per column. Empty columns are left out."""
    keep = [i for i in range(len(headers)) if any(row[i] for row in rows)]
    headers = [headers[i] for i in keep]
    rows = [[row[i] for i in keep] for row in rows]
    align = [align[i] for i in keep]
    widths = [max(len(r[i]) for r in [headers] + rows) for i in range(len(headers))]

    def fmt(row):
        return "  ".join(
            cell.ljust(w) if a == "l" else cell.rjust(w) for cell, w, a in zip(row, widths, align)
        ).rstrip()

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))


def print_legend(notes: set[str]) -> None:
    if notes:
        print()
        for note in sorted(notes):
            print(f"{note}: {NOTE_LEGEND[note]}")


def print_machines(machines: list[dict], show_region: bool) -> set[str]:
    """Print the machine table and return the notes shown in it."""
    headers = ["Region", "Machine type", "vCPUs", "Cores", "Memory (GiB)", "USD/hour", "USD/month",
               "USD/core/hour", "Preempted", "7 days", "Days", "Est. USD/hour", "Notes"]
    rows, notes_shown = [], set()
    for m in machines:
        notes = family_notes(m["family"])
        notes_shown.update(notes)
        # Preemption columns stay empty (and are left out) unless --preemption was used.
        preempted = recent = days = estimate = ""
        if "preemption" in m:
            stats = m["preemption"]
            if stats is None:  # query failed
                preempted = recent = days = estimate = "?"
            else:
                preempted = f"{stats['rate']:.0%}" if stats["rate"] is not None else "-"
                recent = f"{stats['rate_7d']:.0%}" if stats["rate_7d"] is not None else "-"
                days = str(stats["days"])
                estimate = f"{effective_price(m):.6f}" if stats["rate"] is not None else "-"
        rows.append([
            m["region"] if show_region else "", m["name"], str(m["vcpus"]), f"{cores(m):g}", f"{m['memory']:g}",
            f"{m['hourly']:.6f}", f"{m['hourly'] * HOURS_PER_MONTH:.2f}", f"{core_price(m):.6f}",
            preempted, recent, days, estimate, ", ".join(notes),
        ])
    print_table(headers, rows, "llrrrrrrrrrrl")
    return notes_shown


def print_custom_rates(custom_rates: dict, regions: list[str], families: list[str] | None,
                       show_region: bool) -> set[str]:
    """Print the custom machine rate table and return the notes shown in it."""
    headers = ["Region", "Family", "USD/vCPU/hour", "USD/core/hour", "USD/GiB/hour", "USD/ext. GiB/hour", "Notes"]
    rows, notes_shown = [], set()
    for region in regions:
        rates_by_family = custom_rates.get(region, {})
        for family in families or sorted(rates_by_family):
            rates = rates_by_family.get(family)
            if family not in CUSTOM_LIMITS or not rates or "vcpu" not in rates or "memory" not in rates:
                continue
            notes = family_notes(family)
            notes_shown.update(notes)
            extended = f"{rates['extended']:.6f}" if "extended" in rates else "-"
            rows.append([
                region if show_region else "", family, f"{rates['vcpu']:.6f}",
                f"{rates['vcpu'] * vcpus_per_core(family):.6f}", f"{rates['memory']:.6f}", extended, ", ".join(notes),
            ])
    if rows:
        print("Custom machine types (vCPUs and memory priced separately):\n")
        print_table(headers, rows, "llrrrrl")
    return notes_shown


# --------------------------------------------------------------------------- CLI


def comma_list(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Show current Google Cloud Spot VM prices and recommend the cheapest machine type.",
        epilog=(
            "examples:\n"
            f"  %(prog)s                              N1/N2 prices in {DEFAULT_REGION}\n"
            "  %(prog)s -r europe-north1 -f n2,e2,n2d\n"
            "  %(prog)s --cpu 8 --mem 32             cheapest N1/N2 with >= 8 vCPUs and >= 32 GiB\n"
            "  %(prog)s --cpu 8 --mem 32 -t custom   same, custom machine types only\n"
            "  %(prog)s --cpu 16 -f all -r all       cheapest machine with >= 16 vCPUs anywhere\n"
            "  %(prog)s -f n2,n4,t2d -s core         compare price per physical core\n"
            "  %(prog)s --cpu 8 -f all -p            include preemption risk (needs gcloud)\n"
            "  %(prog)s --list-families              show available families and types\n"
            "\n"
            "Most families count each hyperthread as a vCPU (2 vCPUs = 1 core); T2D, T2A, C4A,\n"
            "N4A and H3 have one vCPU per physical core. USD/core/hour compares them fairly.\n"
            "\n"
            "--preemption runs `gcloud beta compute advice capacity-history` for the candidates\n"
            "(gcloud's default project and account are used) and recommends the machine with\n"
            "the lowest estimated price including reruns of preempted work."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-r", "--region", default=DEFAULT_REGION,
                   help=f"region code, e.g. europe-north1, or 'all' (default: {DEFAULT_REGION})")
    p.add_argument("-f", "--family", type=comma_list, default=DEFAULT_FAMILIES,
                   help=f"comma-separated machine families, or 'all' (default: {','.join(DEFAULT_FAMILIES)})")
    p.add_argument("-t", "--type", type=comma_list, default=DEFAULT_TYPES,
                   help=f"comma-separated machine types, or 'all' (default: {','.join(DEFAULT_TYPES)})")
    p.add_argument("--cpu", type=int, metavar="N", help="minimum number of vCPUs needed")
    p.add_argument("--mem", type=float, metavar="GIB", help="minimum memory needed, in GiB")
    p.add_argument("-p", "--preemption", action="store_true",
                   help="fetch historical preemption rates with gcloud and estimate the price including reruns")
    p.add_argument("-s", "--sort", choices=["name", "price", "core", "est"],
                   help="sort by name, hourly price, price per physical core, or estimated price including "
                        "preemption (default: est with --preemption and --cpu/--mem, price with --cpu/--mem, "
                        "otherwise name)")
    p.add_argument("-n", "--limit", type=int,
                   help="max rows to show (default: 10 with --cpu/--mem, otherwise all)")
    p.add_argument("--refresh", action="store_true",
                   help="ignore the caches and fetch fresh prices and preemption data")
    p.add_argument("--list-regions", action="store_true", help="list available regions and exit")
    p.add_argument("--list-families", action="store_true",
                   help="list machine families and their types in the selected region(s) and exit")
    args = p.parse_args(argv)
    if args.sort == "est" and not args.preemption:
        p.error("--sort est requires --preemption")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    data = load_prices(args.refresh)
    regions = data["regions"]
    custom_rates = data["custom"]

    if args.list_regions:
        for code in sorted(regions):
            print(f"{code:28} {regions[code]}")
        return 0

    region = args.region.lower()
    all_regions = region == "all"
    if not all_regions and region not in regions:
        sys.exit(f"error: unknown region '{args.region}'; see --list-regions")
    scope = sorted(regions) if all_regions else [region]
    in_region = [m for m in data["machines"] if m["region"] in scope]

    if args.list_families:
        types_by_family = {}
        for m in in_region:
            types_by_family.setdefault(m["family"], set()).add(m["type"])
        for r in scope:
            for family in custom_rates.get(r, {}):
                if family in CUSTOM_LIMITS:
                    types_by_family.setdefault(family, set()).add("custom")
        rows = [
            [family, ", ".join(sorted(types)), "1 core" if family in NO_SMT_FAMILIES else "1/2 core",
             ", ".join(family_notes(family))]
            for family, types in sorted(types_by_family.items())
        ]
        print_table(["Family", "Types", "1 vCPU =", "Notes"], rows, "llll")
        print_legend({note for family in types_by_family for note in family_notes(family)})
        return 0

    families = None if "all" in args.family else args.family
    types = None if "all" in args.type else args.type
    show_custom = types is None or "custom" in types
    requirements = args.cpu is not None or args.mem is not None

    def is_selected(m):
        return (families is None or m["family"] in families) and (types is None or m["type"] in types)

    def meets_requirements(m):
        return (args.cpu is None or m["vcpus"] >= args.cpu) and (args.mem is None or m["memory"] >= args.mem)

    # Custom machine types can only be sized once requirements are known.
    options = in_region + (custom_options(custom_rates, scope, args.cpu, args.mem) if requirements else [])
    selected = [m for m in options if is_selected(m)]
    has_custom_rates = show_custom and any(
        f in CUSTOM_LIMITS and (families is None or f in families) for r in scope for f in custom_rates.get(r, {})
    )
    if not selected and not has_custom_rates:
        sys.exit("error: no machine types match the selected region/families/types; see --list-families")

    candidates = [m for m in selected if meets_requirements(m)]

    def name_key(m):
        family_rank = families.index(m["family"]) if families else 0
        type_rank = types.index(m["type"]) if types else (
            DEFAULT_TYPES.index(m["type"]) if m["type"] in DEFAULT_TYPES else len(DEFAULT_TYPES))
        return (family_rank, m["family"], type_rank, m["type"], m["vcpus"], m["memory"], m["region"])

    def price_key(m):
        return (m["hourly"], m["vcpus"], m["memory"], m["name"], m["region"])

    def core_price_key(m):
        # Rounded so that rounding noise in the listed prices doesn't reorder sizes of one type.
        return (round(core_price(m), 6),) + price_key(m)

    def est_key(m):
        estimate = effective_price(m)
        return (estimate if estimate is not None else math.inf,) + price_key(m)

    sort_keys = {"name": name_key, "price": price_key, "core": core_price_key, "est": est_key}
    sort = args.sort or (("est" if args.preemption else "price") if requirements else "name")
    limit = args.limit if args.limit is not None else (10 if requirements else None)

    unchecked, hidden = [], []
    best_estimate = math.inf
    if args.preemption:
        preemption_cache = {} if args.refresh else load_preemption_cache()
        if requirements:
            # Check machines in order of hourly price, only as far as one could still win.
            candidates.sort(key=price_key)
            checked, best_estimate = check_preemption(candidates, limit, preemption_cache)
            candidates, unchecked = candidates[:checked], candidates[checked:]
        else:
            candidates.sort(key=name_key if sort == "est" else sort_keys[sort])
            hidden = candidates[limit:] if limit is not None else []
            candidates = candidates[:limit]
            if len(candidates) > MAX_PREEMPTION_QUERIES:
                sys.exit(f"error: --preemption checks at most {MAX_PREEMPTION_QUERIES} machine types; "
                         "narrow the selection with -f/-t or -n")
            failures = fetch_preemption(candidates, preemption_cache)
            exit_if_setup_problem(failures, len(failures) < len(candidates), single_region=not all_regions)
        save_preemption_cache(preemption_cache)
    candidates.sort(key=sort_keys[sort])

    fetched = datetime.fromtimestamp(data["fetched"]).strftime("%Y-%m-%d %H:%M")
    where = "all regions" if all_regions else regions[region]
    print(f"Spot VM prices, {where} (fetched {fetched})\n")

    notes_shown = set()
    if candidates:
        notes_shown |= print_machines(candidates[:limit], show_region=all_regions)
        more = len(hidden) + (max(0, len(candidates) - limit) if limit is not None else 0)
        if more:
            print(f"... {more} more (use -n to show more)")
        if unchecked and unchecked[0]["hourly"] >= best_estimate:
            print(f"... {len(unchecked)} more not checked for preemption: "
                  "their hourly price is above the best estimated price")
        elif unchecked:
            print(f"... {len(unchecked)} more not checked for preemption (at most {MAX_PREEMPTION_QUERIES} "
                  "gcloud queries per run); a cheaper estimate may exist, narrow the selection with -f/-t")
    elif requirements:
        print("No selected machine type meets the requirements.")

    if not requirements:
        if show_custom:
            if candidates:
                print()
            notes_shown |= print_custom_rates(custom_rates, scope, families, show_region=all_regions)
        print_legend(notes_shown)
        if args.preemption:
            print(f"\n{PREEMPTION_LEGEND}")
        return 0

    needs = " and ".join(
        ([f">= {args.cpu} vCPUs"] if args.cpu is not None else [])
        + ([f">= {args.mem:g} GiB memory"] if args.mem is not None else [])
    )
    highlighted = []
    best = min(candidates, key=price_key) if candidates else None
    if best:
        safest = None
        if args.preemption:
            with_data = [m for m in candidates if has_preemption_data(m)]
            safest = min(with_data, key=est_key) if with_data else None
        also = ", also after preemption risk" if safest is best else ""
        print(f"\nCheapest machine with {needs}{also}:")
        print(f"  {describe(best, all_regions)}")
        highlighted.append(best)
        if safest is not None and safest is not best:
            print("\nCheapest including preemption risk:")
            print(f"  {describe(safest, all_regions)}")
            highlighted.append(safest)
        elif args.preemption and safest is None:
            print(f"\nNo checked machine has {PREEMPTION_MIN_DAYS}+ days of preemption data to rank by risk.")

    # Point out a cheaper option outside the selected families/types, if any.
    others = [m for m in options if not is_selected(m) and meets_requirements(m)]
    alt = min(others, key=price_key) if others else None
    if alt and (best is None or alt["hourly"] < best["hourly"]):
        label = "Cheaper outside" if best else "Cheapest option outside"
        print(f"\n{label} your selection (try -f all / -t all):")
        print(f"  {describe(alt, all_regions)}")
        highlighted.append(alt)
    elif best is None:
        print("Try other regions (-r all).")
    print_legend(notes_shown | {note for m in highlighted for note in family_notes(m["family"])})
    if args.preemption:
        print(f"\n{PREEMPTION_LEGEND}")
    return 0 if candidates else 1


if __name__ == "__main__":
    sys.exit(main())
