# overhead-tests

Measure **AHV host CPU overhead** while many user VMs (UVMs) run a workload.

**What happens:** the tool looks at free memory on the AHV host, creates as many
clones as will fit (each sized by `vm_size_gb` in the config — default often
5 GB), powers them on together, and records CPU use in host cgroups
(`ahv-cvm.slice`, `ahv-uvms.slice`, `ahv.services`, …). You can change VM size,
clone count headroom, sample interval, how long to run, workload type, and more
in the JSON under `sample/`.

---

## Prerequisites

Things **you** need before the first command (not how the tool works inside):

1. This repo on a machine that can drive the cluster (see below).
2. **Python 3**.
3. The AHV **host name** (from `acli host.list` on the CVM).
4. **For setup only** (first create of VMs): install **`sshpass`**. The script
   will **prompt once** for the base VM root password (or use `VM_PASSWORD`).
   `sshpass` is only there so setup can run `ssh-copy-id` with that password.
5. During setup, new VMs must reach the **internal package mirror** (URLs in
   `workload/*.py`). The tool downloads redis/fio/etc. into the guest — you do
   not install those by hand.

**Where to run**

- **Preferred:** on the **CVM** (no `--cvm` flag).
- **Also fine:** from your **UBVM** (developer VM) with
  `python3 overhead.py --cvm <cvm_ip> --host …`, if that UBVM can SSH to the
  CVM as user `nutanix`.
- **Not supported:** running on the AHV hypervisor host itself (`acli`/`ncli`
  live on the CVM).

(UVMs in this tool are the **user VMs** we clone for the workload — not your UBVM.)

---

## Quick start (from the CVM)

SSH to the CVM and `cd` into this repo. Replace `<ahv_host>` with your host name.

### 1. Validate connectivity

```bash
python3 overhead.py --host <ahv_host> --validate
```

Checks CVM `acli` and SSH to the AHV host. Does not create VMs.

### 2. Small try run

```bash
python3 overhead.py --host <ahv_host> --config sample/test_quick.json
```

Creates clones (if needed) and runs a short smoke experiment. Good first check
that setup + collector work.

### 3. Clear VMs before another workload or a clean full run

Review what is on the cluster first. Delete the VMs from your previous overhead
run (base + clones). On a **dedicated test cluster**, you may clear everything;
on a **shared** cluster, only delete the names you created.

```bash
acli vm.list
# Example: remove the last quick/redis fleet
acli vm.delete redis_vm_* confirm=true
acli vm.delete redis_base confirm=true
# Dedicated test cluster only — wipe all VMs if that is safe for you:
# acli vm.delete * confirm=true
```

### 4. Normal run (example: redis)

```bash
python3 overhead.py --host <ahv_host> --config sample/redis.json
```

Setup (if needed) plus a full redis overhead experiment (default 3 runs).

### 5. Same workload again later (skip setup)

If the redis base + clones are already there and you only want another
experiment:

```bash
python3 overhead.py --host <ahv_host> --config sample/redis.json --skip-setup
```

Useful flags: `--help`, `--setup-only`, `--runs N`.

From your UBVM, add `--cvm <cvm_ip>` to each command above.

---

## Without the unified CLI

Same idea in two steps:

```bash
python3 setupVms.py  -H <ahv_host> -f sample/redis.json
python3 overheadTest.py -H <ahv_host> -f sample/redis.json
```

(Add `-i <cvm_ip>` if not running on the CVM.)

---

## Config (`sample/`)

| Field | Meaning |
|-------|---------|
| `vm_size_gb` | Memory per clone (drives how many fit) |
| `clone_buffer` | Extra clones beyond the free-memory estimate |
| `base_vm_name` / `clone_prefix` | Required naming for base + clones |
| `workload.type` | `redis`, `fio`, or `dirtyHarry` |
| `interval` | Sample interval (seconds) |
| `baseline_duration` | Time with all test VMs off before power-on |
| `stable_ticks` / `max_warmup_duration` | How long to collect after power-on |
| `collector` | `bpfsnap` (default) or `schedstat` |
| `num_runs` | Default **3** |

Examples: `sample/test_quick.json` (short), `sample/redis.json`, `sample/fio_*.json`.

---

## How it works (short)

1. **Setup** sizes the host, builds one base VM, installs the workload (download
   from the internal mirror), clones to fill memory, leaves them powered off.
2. **Experiment** powers clones on, runs the **bpfsnap** collector on the AHV
   host (ships `bpf/prebuilt/schedstat_snap` from this repo — no hand install),
   then prints a summary and writes CSVs under `results_*/`.
3. If the prebuilt BPF binary is missing, the tool tries to build on the host
   (usually fails on AHV). Use `"collector": "schedstat"` as a fallback, or
   restore `bpf/prebuilt/schedstat_snap`.

---

## Layout

```
overhead.py          # preferred CLI
setupVms.py          # build + clone
overheadTest.py      # experiment (also called by overhead.py)
sample/              # example configs
workload/            # redis / fio / dirtyHarry
statsCollector/      # bpfsnap (+ schedstat fallback)
statsDump/           # terminal + CSV
bpf/prebuilt/        # shipped eBPF collector binary
util/                # CVM / host / VM helpers
```
