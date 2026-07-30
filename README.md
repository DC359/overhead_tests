# overhead-tests

A framework for measuring **AHV host / CVM CPU overhead** while a fleet of user
VMs (UVMs) runs a chosen workload. It clones a base VM N times, powers them on
together, and records per-cgroup CPU usage (`cvm.slice`, `system.slice`, etc.)
on the AHV host across baseline → power-on → warmup → stable phases.

## How it works (two steps)

The flow is always **setup once, then run the experiment (repeatedly)**:

1. **`setupVms.py`** — builds a base VM (disk + NIC), installs the workload as a
   systemd service, powers it off, and clones it as many times as the host can
   fit. Run this once per workload/config.
2. **`overheadTest.py`** — powers on all clones, runs the stats collector on the
   host through the phases, then fetches + processes the data and dumps results
   (terminal table + CSV).

```bash
# Step 1: build + clone the base VM (installs the workload)
./setupVms.py -i <cvm_ip> -f sample/redis.json -H <ahv_host_name>

# Step 2: run the overhead experiment
./overheadTest.py -i <cvm_ip> -f sample/redis.json -H <ahv_host_name>
```

Flags (both scripts): `-i <cvm_ip>` (omit if running on the CVM), `-f <config.json>`,
`-H <ahv_host_name>` (required).

## Config files (`sample/`)

Each JSON config selects the collector, VM sizing, phase timing, and the
workload. Key fields:

| field | meaning |
|-------|---------|
| `collector` | `bpfsnap` (eBPF snapshot+exit, preferred) or `schedstat` (/proc poll) |
| `vm_size_gb` | memory per clone |
| `interval` | collector sample interval (s) |
| `baseline_duration` | baseline phase length (all VMs off) |
| `stable_ticks` | number of samples that define the stable window |
| `max_warmup_duration` | cap on warmup detection (s) |
| `clone_buffer` | headroom left when computing max clones |
| `base_vm_name` / `clone_prefix` | naming for the base VM and its clones |
| `workload.type` | `redis`, `fio`, `iperf`, or `dirtyHarry` |
| `workload.config` | workload-specific knobs |

Ready-made configs: `sample/redis.json`, `sample/redis_char.json`,
`sample/fio_*.json`, `sample/test_iperf_*.json`, etc.

## Workloads (`workload/`)

- **`redis`** — in-memory KV load. `redis-server` + a continuous
  `redis-benchmark` loop run inside each VM against `127.0.0.1`. Persistence is
  off (`--save "" --appendonly no`) so it's CPU/syscall-bound, not I/O-bound.
- **`fio`** — disk I/O generator.
- **`iperf`** — network throughput (clones are pinned to the target host).
- **`dirtyHarry`** — memory-dirtying workload.

## Prerequisites

- **Runs against a Nutanix cluster.** You need a reachable CVM and the AHV host
  name; VM create/clone/power ops go through the CVM's `acli`/`ncli`.
- **Python 3** on the machine you run from.
- **`sshpass`** installed locally (used to seed the SSH key onto the base VM).
- **SSH/SCP password helpers**: `.rcmd.exp` and `.rscp.exp` are gitignored and
  must exist locally (they wrap password-based ssh/scp to the VMs).
- **Workload binaries mirror**: `redis` and `iperf` workloads download prebuilt
  binaries from an internal mirror (see the URL constant at the top of each
  `workload/*.py`). The base VMs have no usable apt repo, only this mirror — so
  the mirror must be reachable from the VMs, or the binaries pre-placed.
- **eBPF collector binary**: `bpf/prebuilt/schedstat_snap` is a statically
  libbpf-linked binary shipped to the host so no build toolchain is needed there.
  See `bpf/prebuilt/README.md` for how it's built/regenerated.

## Output

`overheadTest.py` prints a per-cgroup summary table to the terminal and writes a
CSV (via `statsDump/`). Results directories (`results_*/`) are gitignored.

## Layout

```
overheadTest.py      # run the experiment
setupVms.py          # build + clone the base VM
util/                # Cvm/Host/Vm wrappers + ObjFactory
workload/            # redis / fio / iperf / dirtyHarry workload definitions
statsCollector/      # bpfsnap + schedstat collectors
statsDump/           # terminal + csv output
bpf/                 # eBPF collector sources + prebuilt binary
sample/              # example config JSONs
```
