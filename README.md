# overhead-tests

Measure **AHV host / CVM CPU overhead** while a fleet of user VMs (UVMs) runs a
workload. Clones a base VM, powers them on together, and records per-cgroup CPU
usage on the AHV host (`ahv-cvm.slice`, `ahv-uvms.slice`, `ahv.services`, …).

## Quick start (unified CLI)

```bash
# On the CVM (or with --cvm <ip> from your laptop):
python3 overhead.py --host <ahv_host> --validate
python3 overhead.py --host <ahv_host> --config sample/test_quick.json
python3 overhead.py --host <ahv_host> --config sample/redis.json --skip-setup
```

Useful flags: `--help`, `--validate`, `--skip-setup`, `--setup-only`, `--runs N`.

Legacy entrypoints still work: `setupVms.py` then `overheadTest.py`.

## How it works

1. **Setup** — build a base VM, install the workload as a systemd service, clone
   as many as the host can fit (`setupVms.py`, or automatic via `overhead.py`).
2. **Experiment** — power on clones, collect with bpfsnap (default) or
   schedstat, then print a short summary and write CSVs under `results_*/`.

## Config (`sample/`)

| field | meaning |
|-------|---------|
| `collector` | `bpfsnap` (preferred) or `schedstat` |
| `vm_size_gb` | memory per clone |
| `interval` | sample interval (s) |
| `baseline_duration` | time with VMs off before power-on |
| `stable_ticks` / `max_warmup_duration` | how long to collect after power-on |
| `clone_buffer` | extra clones beyond free-memory estimate |
| `base_vm_name` / `clone_prefix` | required naming |
| `workload.type` | `redis`, `fio`, or `dirtyHarry` |
| `num_runs` | default **3** (use 1 only when you set it) |

Examples: `sample/test_quick.json` (smoke), `sample/redis.json`, `sample/fio_*.json`.

## Workloads

- **redis** — in-guest `redis-server` + `redis-benchmark`
- **fio** — disk I/O
- **dirtyHarry** — memory dirtying

## Prerequisites

- Nutanix cluster (CVM + AHV host); ops via `acli` / `ncli`
- Python 3; `sshpass` for seeding SSH onto the base VM
- Local (gitignored) `.rcmd.exp` / `.rscp.exp` for password SSH/SCP to VMs
- Workload binaries from the internal mirror (see `workload/*.py`)
- Prebuilt eBPF binary: `bpf/prebuilt/schedstat_snap` (see `bpf/prebuilt/README.md`)

## Output

Terminal: host metadata, top services, stable mean ± stdev.  
Files under `results_*/`: `metadata_*.json`, `slices_*.csv`, `services_*.csv`,
`full_slices_*.csv`, `events_*.csv` (plus bpf debug CSVs only if schedstat has data).

## Layout

```
overhead.py          # preferred CLI entrypoint
setupVms.py          # build + clone base VM
overheadTest.py      # experiment runner (also used by overhead.py)
util/                # Cvm / Host / Vm / credentials / metadata
workload/            # redis / fio / dirtyHarry
statsCollector/      # bpfsnap (+ schedstat fallback)
statsDump/           # terminal + csv
bpf/                 # eBPF sources + prebuilt binary
sample/              # example configs
schedstat_collect.c  # companion binary for schedstat collector
```
