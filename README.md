# overhead-tests

Measure **AHV host / CVM CPU overhead** while a fleet of user VMs (UVMs) runs a
workload. Clones a base VM, powers them on together, and records per-cgroup CPU
usage on the AHV host (`ahv-cvm.slice`, `ahv-uvms.slice`, `ahv.services`, …).

## Quick start (unified CLI)

```bash
# On the CVM (or with --cvm <ip> from your laptop):
python3 overhead.py --host <ahv_host> --validate

# First time: setup + short smoke (creates redis_vm_* clones)
python3 overhead.py --host <ahv_host> --config sample/test_quick.json

# Same VMs, longer redis run — do NOT recreate (same clone_prefix)
python3 overhead.py --host <ahv_host> --config sample/redis.json --skip-setup
```

`test_quick.json` and `redis.json` share `redis_base` / `redis_vm` — only timings
differ. Clear clones only when switching workload/prefix or rebuilding.

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

**Always**
- Nutanix cluster (CVM + AHV host); ops via `acli` / `ncli`
- Python 3
- SSH from the CVM to the AHV host as root
- This repo checked out (includes `bpf/prebuilt/schedstat_snap`)

**Only for setup** (first time / no `--skip-setup`)
- `sshpass` — seeds an SSH key onto the base VM (`ssh-copy-id`)
- Base VM root password once (prompt, or `VM_PASSWORD` env)
- From inside the base VM, `wget` must reach the **internal package mirror**
  (URLs in `workload/*.py`). You do **not** install redis/fio by hand: setup
  downloads the binaries into the guest during `setupVms` / first `overhead.py` run.
  If the mirror is unreachable, setup fails with a download error.

**BPF collector (`bpfsnap`, default)**
- Code checks for `bpf/prebuilt/schedstat_snap` on the machine running the tool.
  - **Present (normal):** SCPs it to the host under `/root/bpfsnap/`, chmod +x,
    smoke-tests it. No clang/bpftool install needed on the host.
  - **Missing:** tries to **build on the AHV host** (needs clang/bpftool/libbpf —
    AHV hosts usually don’t have these → hard fail). Fix: restore the prebuilt
    file, or set `"collector": "schedstat"` in the config as fallback.
- Host still needs kernel features (BTF, task iter, `sched_process_exit`); if those
  are missing, bpfsnap fails and points you at the schedstat collector.

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
