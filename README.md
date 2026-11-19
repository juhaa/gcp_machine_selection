# gcp_spot.py

Command line tool that shows current Google Cloud Spot VM prices as a table and
recommends the cheapest machine type for a given vCPU/memory requirement.

Prices come from <https://cloud.google.com/spot-vms/pricing>. The page embeds the
price tables for every region, so one download covers all regions. Parsed prices are
cached for 24 hours in `~/.cache/gcp-spot-prices/prices.json` (`--refresh` to refetch).

Requires Python 3.9+, no extra packages.

## Usage

```bash
./gcp_spot.py                                  # N1 + N2 standard/highmem/highcpu + custom rates, europe-west1
./gcp_spot.py -r europe-north1 -f n2,n2d,e2    # other region and families
./gcp_spot.py -t highmem -s price              # only highmem, cheapest first
./gcp_spot.py --cpu 8 --mem 32                 # recommend cheapest with >= 8 vCPUs and >= 32 GiB
./gcp_spot.py --cpu 8 --mem 32 -t custom       # same, custom machine types only
./gcp_spot.py --cpu 16 -f all -r all           # cheapest 16+ vCPU machine in any pd-standard family/region
./gcp_spot.py --cpu 8 --mem 32 -f all -p       # include preemption risk (needs gcloud)
./gcp_spot.py --cpu 8 -f all --disk any        # also families without pd-standard (C3, N4, ...)
./gcp_spot.py --cpu 8 -f all --disk pd-balanced --no-arm  # x86 families that work with Batch defaults
./gcp_spot.py --list-regions
./gcp_spot.py --list-families -r europe-north1
```

| Option | Default | Meaning |
| --- | --- | --- |
| `-r, --region` | `europe-west1` | Region code, or `all` |
| `-f, --family` | `n1,n2` | Comma-separated families (`n1,n2,e2,c3,...`), or `all` |
| `-t, --type` | `standard,highmem,highcpu,custom` | Comma-separated types, or `all` (adds `megamem`, `standard-lssd`, ...) |
| `--no-hyperdisk` | | Leave out families without Persistent Disk support (see below) |
| `--disk TYPE` | `pd-standard` | Only families that support boot disk `pd-standard`, `pd-balanced`, `pd-ssd` or `hyperdisk-balanced`; `any` for all families |
| `--no-arm` | | Leave out Arm families (C4A, N4A, T2A) |
| `--cpu N` | | Minimum vCPUs |
| `--mem GIB` | | Minimum memory in GiB |
| `-p, --preemption` | | Fetch preemption history with gcloud and rank by estimated price including reruns |
| `-s, --sort` | `price` with `--cpu/--mem`, else `name` | Sort by `name`, hourly `price`, price per physical `core`, or estimated price `est` (needs `-p`) |
| `-n, --limit` | 10 with `--cpu/--mem`, else all | Max rows shown |
| `--refresh` | | Ignore caches |

When `--cpu`/`--mem` is given, the tool prints the cheapest matching machine in your
selection, plus a cheaper machine outside the selected families/types if one exists.

## Cores and notes

Most families count each hyperthread as a vCPU, so 2 vCPUs = 1 physical core. T2D, T2A,
C4A, N4A and H3 have SMT disabled: 1 vCPU = 1 core. The `Cores` and `USD/core/hour`
columns make these comparable for CPU-bound jobs; use `-s core` to sort by them.
`--list-families` shows which families are which.

The `Notes` column flags families that need extra setup in Google Batch:

- `Arm` (C4A, N4A, T2A): Arm CPU; container images must be built for arm64.
- `Hyperdisk` (N4, N4D, N4A, C4, C4D, C4A, C4N, M4, Z4M, Z4D): no Persistent Disk
  support. Batch's default `pd-balanced` boot disk won't work; set the boot disk
  type to `hyperdisk-balanced`.

By default only families that support `pd-standard` disks are shown, so Hyperdisk
families only appear with `--disk any` or `--disk hyperdisk-balanced`. T2A (Arm)
supports `pd-standard`; leave it out with `--no-arm`. `--no-hyperdisk` leaves out the
Hyperdisk families even with `--disk any`. Filtered families are also left out of the
"cheaper outside your selection" suggestion and `--list-families`.

`--disk TYPE` keeps only families that can use that disk type. The default is the
cheaper (but slower, HDD-backed) `pd-standard`; every family that supports it also
supports Batch's default `pd-balanced`. `--disk any` turns the filter off. Bootable disk
types per family, from Google's machine family docs:

| Disk type | Families |
| --- | --- |
| `pd-standard` | N1, N2, N2D, E2, C2, C2D, T2D, T2A |
| `pd-balanced` | the above, plus C3, H3, M1, M2, M3, Z3 |
| `pd-ssd` | as `pd-balanced`, except H3 |
| `hyperdisk-balanced` | C3, H3, M1, M2, M3, Z3 and the Hyperdisk-only families |

N1, N2, N2D, E2 and C2D support Hyperdisk Balanced only on request to Google's account
team, so they are not listed for it. Families missing from this list (new ones) are
left out unless `--disk any` is given. In Batch, set the type in
`instancePolicy.bootDisk.type` (and `instancePolicy.disks[].newDisk.type` for extra disks).

## Preemption risk (`-p`)

Spot VMs can be preempted at any time, and how often varies a lot between machine types,
sizes and regions (in Nov 2026, 3% for c2d-standard-8 but 41% for n4-standard-8 in
europe-west1, and 78% for n2-standard-8 in europe-north1). With `-p` the tool runs

```bash
gcloud beta compute advice capacity-history --provisioning-model=SPOT \
    --machine-type=MACHINE_TYPE --types=PREEMPTION --region=REGION
```

for the candidates (8 in parallel, results cached for 24 hours in
`~/.cache/gcp-spot-prices/preemption.json`) and adds these columns:

- `Preempted` / `7 days`: share of Spot VMs of that type in that region that ended by
  preemption rather than by their owner, averaged over the days with data in the last
  ~30 days and the last 7 days.
- `Days`: days with data. Days with too little usage are missing; machines with fewer
  than 7 days are not recommended. Custom machine types are listed but not checked
  (the API has no daily data for them, only one 30-day entry), so their preemption
  columns show `-`.
- `Est. USD/hour`: hourly price × (1 + p / (2 × (1 − p))), the expected cost if each run
  is preempted with probability p and a preempted run loses half its work before being
  rerun. This is a rough model: the rate depends on how long other people's VMs run, not
  on your tasks, so use it to compare machines rather than as a probability. Checkpointing
  lowers the real cost of preemptions.

With `--cpu`/`--mem` the tool checks candidates in order of hourly price and stops once no
unchecked machine can beat the best estimate (the estimate is never below the hourly
price), at most 60 gcloud calls per run. The table is sorted by hourly price (`-s est` sorts
by the estimate instead). It prints both the cheapest machine per hour and the cheapest
including preemption risk.

Requirements: the gcloud `beta` component (`gcloud components install beta`) and an
authenticated account. Queries use gcloud's default project; set another one with
`CLOUDSDK_CORE_PROJECT=my-project ./gcp_spot.py ...`. Some regions may fail for a project
(e.g. "resource ... is not ready"); those machines show `?` and a warning is printed.
The prices in this API match the pricing page exactly (checked for 27 machine/region
combinations).

For Google Batch, combine this with retries: set `maxRetryCount` and a lifecycle policy
that retries on exit code 50001 (VM preempted).

Sources: Google's machine family docs
([general-purpose](https://cloud.google.com/compute/docs/general-purpose-machines),
[compute-optimized](https://cloud.google.com/compute/docs/compute-optimized-machines),
[memory-optimized](https://cloud.google.com/compute/docs/memory-optimized-machines),
[storage-optimized](https://cloud.google.com/compute/docs/storage-optimized-machines),
[network-optimized](https://cloud.google.com/compute/docs/network-optimized-machines)).
These lists are hard-coded, so new families need adding by hand.

## Custom machine types

Custom machine types (N1, N2, N2D, N4, N4D, N4A, E2) are priced per vCPU and per GiB.
With `--cpu`/`--mem`, the tool finds the cheapest valid custom shape for each family,
following Google's
[custom machine type limits](https://cloud.google.com/compute/docs/instances/creating-instance-with-custom-machine-type)
(allowed vCPU counts, GiB per vCPU, 256 MB memory steps). Memory above the per-vCPU
maximum is billed as extended memory, which is only used where the pricing page lists
a Spot price for it (N2, N2D, N4). Shapes are named as gcloud expects, e.g.
`n2-custom-8-32768` or `n2-custom-4-40960-ext`, so you can pass them to
`--machine-type`. N1 custom types have no prefix (`custom-8-32768`).

Without `--cpu`/`--mem`, the tool lists the custom per-vCPU and per-GiB rates below
the table. Use `-t standard,highmem,highcpu` to leave custom types out.

Notes:
- Monthly price = hourly × 730, same as on the pricing page.
- Spot prices are dynamic and vary a lot between regions, so try `-r all`.
- If Google changes the page layout, parsing may break; the tool then exits with an error.
