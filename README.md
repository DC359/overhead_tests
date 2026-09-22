# overhead-tests

Measure AHV host CPU overhead while a fleet of user VMs (UVMs) run a workload.

The tool estimates how many VMs fit in free host memory (each sized by
`vm_size_gb` in the config), creates that many clones, powers them on together,
and records CPU time in host cgroups (`ahv-cvm.slice`, `ahv-uvms.slice`,
`ahv.services`, …). VM size, clone headroom, timing, workload, and collector are
all configurable in JSON under `sample/`.

---

## Prerequisites

Complete these before the first run.

### 1. Get the code

Clone this repository onto the **CVM** (recommended), or onto your **UBVM**
if you will use `--cvm <cvm_ip>`.

```bash
git clone git@github.com:DC359/overhead_tests.git
cd overhead_tests
```

### 2. Python 3

```bash
python3 --version    # expect Python 3.x
```

### 3. Cluster access

- You need a Nutanix cluster and the AHV **host name**.
- On the CVM, confirm:

```bash
acli host.list
```

### 4. `sshpass`

The script prompts for the base VM root password (or reads `VM_PASSWORD`) and
uses `sshpass` to seed an SSH key onto the base VM (`ssh-copy-id`). This repo
does **not** install `sshpass` for you — it must already be available:

```bash
command -v sshpass    # must print a path; if empty, install sshpass before setup
```

### 5. Workload binaries

Prebuilt **redis / fio / dirtyHarry** binaries are hosted on an internal
Nutanix mirror (URLs at the top of each file in `workload/`). Setup downloads
them into the guest automatically. You do not install those packages by hand;
the guest must be able to reach that mirror on the network.

### Where to run

| Location | How |
|----------|-----|
| **CVM** (preferred) | `python3 overhead.py --host <ahv_host> …` |
| **UBVM** (optional) | Same commands plus `--cvm <cvm_ip>`, if the UBVM can SSH to the CVM as `nutanix` |
| AHV hypervisor host | Not supported (`acli` / `ncli` run on the CVM) |

**UBVM** = your developer VM. **UVM** = a user VM this tool clones for the workload.

---

## Quick start (on the CVM)

SSH to the CVM, `cd` into the repo, replace `<ahv_host>` with a name from
`acli host.list`.

### 1. Validate connectivity

```bash
# Confirms CVM acli and SSH to the AHV host; creates no VMs
python3 overhead.py --host <ahv_host> --validate
```

### 2. Small try run

```bash
# Short smoke test: setup (if needed) + brief experiment
python3 overhead.py --host <ahv_host> --config sample/test_quick.json
```

### 3. Clear VMs before another workload or a clean full run

On a **test cluster**, clear leftover VMs so the next run starts clean:

```bash
acli vm.list
acli vm.delete * confirm=true
```

Only do a full `vm.delete *` on a cluster where deleting every VM is acceptable.

### 4. Normal run (example: redis)

```bash
# Full redis experiment (setup if needed; default 3 runs)
python3 overhead.py --host <ahv_host> --config sample/redis.json
```

### 5. Repeat the same workload later

```bash
# Clones already exist — experiment only
python3 overhead.py --host <ahv_host> --config sample/redis.json --skip-setup
```

Useful flags: `--help`, `--setup-only`, `--runs N`.

From a UBVM, add `--cvm <cvm_ip>` to each `overhead.py` command above.

---

## Without the unified CLI

```bash
python3 setupVms.py     -H <ahv_host> -f sample/redis.json
python3 overheadTest.py -H <ahv_host> -f sample/redis.json
```

If not on the CVM, add `-i <cvm_ip>` to both.

---

## Configuration

Configs live in `sample/`. Important fields:

| Field | Meaning |
|-------|---------|
| `vm_size_gb` | Memory per clone (affects how many fit) |
| `clone_buffer` | Extra clones beyond the free-memory estimate, used to max out space on the host |
| `base_vm_name` / `clone_prefix` | Required names for base + clones |
| `workload.type` | `redis`, `fio`, or `dirtyHarry` |
| `interval` | Sample interval (seconds) |
| `baseline_duration` | Time with test VMs off before power-on |
| `stable_ticks` / `max_warmup_duration` | Collection length after power-on |
| `collector` | `bpfsnap` (default) or `schedstat` |
| `num_runs` | Default **3** |

Examples: `sample/test_quick.json`, `sample/redis.json`, `sample/fio_*.json`.

---

## How it works

1. **Setup** — create base VM, download workload binaries from the internal
   mirror into the guest, enable a systemd service, clone to fill host memory.
2. **Experiment** — power on clones; run **bpfsnap** on the AHV host using the
   prebuilt binary at `bpf/prebuilt/schedstat_snap` (shipped in this repo);
   print a summary and write CSVs under `results_*/`.
3. If that prebuilt file is missing, the tool tries to compile on the host
   (usually fails on AHV). Restore the prebuilt binary, or set
   `"collector": "schedstat"` in the config.

---

## Layout

```
overhead.py           # preferred CLI
setupVms.py           # build + clone
overheadTest.py       # experiment runner
sample/               # example configs
workload/             # redis / fio / dirtyHarry (+ mirror URLs)
statsCollector/       # bpfsnap (+ schedstat fallback)
statsDump/            # terminal + CSV output
bpf/prebuilt/         # shipped eBPF collector binary
util/                 # CVM / host / VM helpers
```
