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

### 1. Use a one-node test cluster

Use a **one-node test cluster** you are allowed to fill with clones.

This tool creates many VMs (enough to consume free host memory) and may
delete leftover test VMs. Do **not** run it on a shared or production cluster.

You need:
- CVM IP (example: `10.117.24.167`)
- AHV host name from `acli host.list` (example: `Berwick02-4`)

### 2. Get the code onto the CVM (recommended path)

CVMs often have **no `git`**. The usual flow is: clone on your **UBVM**, then
`scp` the tree to the CVM.

**UBVM** = your developer VM. **UVM** = a guest VM this tool clones for the workload.

#### On the UBVM — clone

Prefer HTTPS (no GitHub SSH key required if the repo is public):

```bash
cd ~
git clone https://github.com/DC359/overhead_tests.git
cd overhead_tests
```

If GitHub asks for a username/password on a private clone, use your GitHub
username and a Personal Access Token (not your GitHub account password).

#### On the UBVM — pack and copy to the CVM

Prefer `/home/nutanix/tmp` on the CVM. That path is deletable later; files under
`/home/nutanix` itself are protected by CVM `safe_rm` and are hard to remove.

```bash
cd ~
tar czf overhead_tests.tgz overhead_tests
scp overhead_tests.tgz nutanix@<cvm_ip>:/home/nutanix/tmp/
```

Enter the CVM `nutanix` password if asked.

#### On the CVM — unpack and open the repo

```bash
ssh nutanix@<cvm_ip>
cd /home/nutanix/tmp
tar xzf overhead_tests.tgz
cd overhead_tests
ls
```

If you already copied the archive to `/home/nutanix/` instead of `tmp`, unpack
there instead (`cd /home/nutanix && tar xzf overhead_tests.tgz`). The run works;
cleanup with `rm -rf` may be blocked until you move the tree under `tmp`.

### 3. Python 3

On the machine where you run the tool (normally the CVM):

```bash
python3 --version    # expect Python 3.x
```

### 4. Cluster access

On the CVM, confirm the host name:

```bash
acli host.list
```

### 5. `sshpass`

The script prompts for the base VM root password (or reads `VM_PASSWORD`) and
uses `sshpass` to seed an SSH key onto the base VM (`ssh-copy-id`). This repo
does **not** install `sshpass` for you — it must already be available:

```bash
command -v sshpass    # must print a path; if empty, install sshpass before setup
```

### 6. Workload binaries

Prebuilt **redis / fio / dirtyHarry** binaries are hosted on an internal
Nutanix mirror (URLs at the top of each file in `workload/`). Setup downloads
them into the guest automatically. You do not install those packages by hand;
the guest must be able to reach that mirror on the network.

### Where to run

| Location | How |
|----------|-----|
| **CVM** (preferred) | `python3 overhead.py --host <ahv_host> …` after the UBVM→CVM copy above |
| **UBVM** (optional) | Possible, but needs extra SSH keys and a small PATH fix for remote `ncli`/`acli`; prefer CVM for first runs |
| AHV hypervisor host | Not supported (`acli` / `ncli` run on the CVM) |

---

## Quick start (on the CVM)

After unpacking under `/home/nutanix/tmp/overhead_tests` (or your chosen path),
`cd` into the repo. Replace `<ahv_host>` with a name from `acli host.list`.
Do **not** pass `--cvm` when you are already on the CVM.

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

On a **one-node test cluster**, clear leftover VMs so the next run starts clean:

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

Results are written under `results_YYYYMMDD_HHMMSS/` in the directory where you
ran the command (on the CVM if you followed this guide).

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
