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

Since CVMs dont allow for direct cloning of repos from github, we clone the repo in our UBVM/local machine and scp it to cvm, to run it from there

#### On the UBVM — clone

Clone from github repo:

```bash
cd ~
git clone https://github.com/DC359/overhead_tests.git
cd overhead_tests
```



#### On the UBVM — pack and copy to the CVM

```bash
cd ~
tar czf overhead_tests.tgz overhead_tests
scp overhead_tests.tgz nutanix@<cvm_ip>:/home/nutanix
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
uses `sshpass` to install your local SSH public key onto the base VM (appends
to `authorized_keys`; does not use `ssh-copy-id`). This repo does **not**
install `sshpass` for you — it must already be available:

```bash
command -v sshpass    # must print a path; if empty, install sshpass before setup
```



### 6. Workload binaries

Prebuilt **redis / fio / dirtyHarry** binaries are hosted on an internal  
Nutanix mirror (URLs at the top of each file in `workload/`). Setup downloads  
them into the guest automatically. You do not install those packages by hand;  
the guest must be able to reach that mirror on the network.

## Quick start (on the CVM)

After unpacking under `/home/nutanix/overhead_tests` (or your chosen path), `cd` into the repo. Replace `<ahv_host>` with a name from `acli host.list`.
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



## Reading results



### Output files


| File                          | What it is                                          |
| ----------------------------- | --------------------------------------------------- |
| Terminal summary (end of run) | Stable averages + top services — start here         |
| `slices_*_runN.csv`           | One row per tick × slice (main numbers)             |
| `full_slices_*_runN.csv`      | Same ticks with every metric field                  |
| `services_*_runN.csv`         | Per host service CPU per tick                       |
| `events_*_runN.csv`           | Wall-clock markers (collection start, VM on/off, …) |
| `metadata_*_runN.json`        | Host / AHV / kernel / QEMU info for that run        |


Use `Phase == stable` rows for final numbers. Ignore `power_on` / `warmup`
unless you are debugging boot or ramp-up.

### Phases (how they are decided)


| Phase      | When                         | How assigned                                     |
| ---------- | ---------------------------- | ------------------------------------------------ |
| `baseline` | Test VMs off                 | From timeline: start → mass power-on             |
| `power_on` | VMs booting / workload check | From timeline: power-on → collecting starts      |
| `warmup`   | VMs on, CPU still settling   | After collecting starts, until UVMs look settled |
| `stable`   | Steady state                 | After UVMs settle; used for the printed summary  |


**How long collecting runs** (after VMs are on) is set by **`collect_duration`**
(seconds), or CLI `--duration N`. That is a fixed sleep. Warmup vs stable
**labels** are decided afterward from the data; they do not shorten the run.

If `collect_duration` is omitted, the legacy formula is used:
`max_warmup_duration + (stable_ticks × interval)`.

`stable_ticks` is only used for post-run phase labeling (fallback), not for
sizing the experiment wait.

Warmup vs stable **labels** are decided **after** the run, from
`ahv-uvms.slice` `x_cores`:

- Compare each tick to the previous one.
- If the relative change stays below `warmup_threshold_pct` (default **5%**)
for `warmup_consecutive` ticks (default **3**), that point becomes the
start of `stable`.
- **UVMs settle fast:** short `warmup`, then **all remaining** collecting ticks
are `stable` (can be more than `stable_ticks`).
- **UVMs never settle:** fallback — last `stable_ticks` samples → `stable`;
earlier collecting ticks → `warmup`.

`baseline_duration` is also a fixed sleep (VMs off). Power-on / guest IP /
workload check time is variable and is labeled `power_on`.

### Slices


| Slice            | Meaning                                                     |
| ---------------- | ----------------------------------------------------------- |
| `ahv-cvm.slice`  | CVM processes on the AHV host                               |
| `ahv-uvms.slice` | User / test VMs (guest workload)                            |
| `ahv.services`   | Host services under `system.slice` (libvirt, networking, …) |
| `other`          | Tasks not in the three buckets above                        |




### Fields (brief)

Common columns in `slices_*.csv` / terminal:


| Field      | Meaning                                             |
| ---------- | --------------------------------------------------- |
| `x_cores`  | Running CPU, as cores (main “how much CPU?” number) |
| `y_cores`  | Runnable but waiting (run-queue), as cores          |
| `xy_cores` | `x_cores + y_cores`                                 |
| `Demand_s` | Estimated CPU time wanted in the tick (seconds)     |
| `Supply_s` | CPU time actually received (seconds)                |
| `VM_Count` | Powered-on test VMs when the tick was labeled       |


In `full_slices_*.csv` (nanoseconds unless noted):


| Field                                  | Meaning                                                      |
| -------------------------------------- | ------------------------------------------------------------ |
| `X`                                    | Run time in the tick                                         |
| `Y`                                    | Wait / run-queue time in the tick                            |
| `Z`                                    | Idle-ish remainder of task time budget in the tick           |
| `T`                                    | Task-time budget for the tick (`interval × task count`)      |
| `Supply`                               | Same idea as supply (= `X`)                                  |
| `Demand`                               | Supply plus an estimate of unmet wait                        |
| `DemandSupplyRatio`                    | Demand relative to supply (scaled integer %)                 |
| `tasks_count` / `counted_tids`         | How many tasks were seen                                     |
| `pct_running_x` / `pct_readyq_y`       | X / Y as % of `T`                                            |
| `pct_contention`                       | Wait share of (run + wait)                                   |
| `pct_cpu_util`                         | Run time vs host CPU budget for the interval                 |
| `fractional` / `fractional_pct_supply` | Wait credited into Demand                                    |
| `pct_chg_X/Y/Z`                        | % change vs previous tick                                    |
| `ephemeral_*` / `total_x_cores`        | Short-lived tasks (schedstat path); may be empty for bpfsnap |


**Demand vs Supply:** nearly equal → little starvation. Demand much larger →  
CPU pressure. Cores ≈ `Demand_s / interval` or `Supply_s / interval`.

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


| Field                                  | Meaning                                                                         |
| -------------------------------------- | ------------------------------------------------------------------------------- |
| `vm_size_gb`                           | Memory per clone (affects how many fit)                                         |
| `clone_buffer`                         | Extra clones beyond the free-memory estimate, used to max out space on the host |
| `base_vm_name` / `clone_prefix`        | Required names for base + clones                                                |
| `workload.type`                        | `redis`, `fio`, or `dirtyHarry`                                                 |
| `interval`                             | Sample interval (seconds)                                                       |
| `baseline_duration`                    | Time with test VMs off before power-on                                          |
| `collect_duration`                     | **Total** collect time (seconds) after VMs are on; or use `--duration`          |
| `stable_ticks` / `max_warmup_duration` | Legacy timing if `collect_duration` omitted; `stable_ticks` still used for labels |
| `collector`                            | `bpfsnap` (default) or `schedstat`                                              |
| `num_runs`                             | Default **3**                                                                   |


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

