# overhead-tests

This tools measures, the CPU footprint of CVM , different services and UVMs in different scenarios. We run a fleet of UVMs with different workloads running on them while measuring how much **AHV host CPU** it costs.

How does the experiment run?

1. **Setup** (unless skipped) : creates one base VM, installs workload on it, clones it and deploys `cgroup_cpu_snap` to the AHV host and swtiches all UVMs off after setup is completed.
2. **Collect :** Once collection script starts, we mass power on the UVMs and sample while on and after certain fixed time, and powers all the UVMs back off. We can run this experiment any number of times (default `num_runs` is 3 unless the config or `--runs` says otherwise) we dont need to run setup after first run.
3. **Present** — After the experiments are run, we fetch the log, print summary, and the final results at write `results_*/` .

One CLI runs the full path: A JSON config chooses the workload, VM names, and timings

```bash
python3 orchestrator.py --host <ahv_host> --config configs/<file>.json
```

---

## Before you start

Use a **one-node test cluster** you are allowed to fill with clones.
Do **not** run this on shared or production clusters.

You need:

- CVM IP  (example: `10.117.24.167`)
- AHV host name  from `acli host.list` (example: `Berwick02-4`)
- Python 3 and `sshpass` on the CVM (setup installs an SSH key into guests)

```bash
python3 --version
command -v sshpass
```

Setup downloads redis / fio / dirtyHarry from an internal Nutanix mirror into
the guest. The guest must reach that mirror; if not, setup fails when it tries
to download.

---

## Get the code onto the CVM

CVMs usually cannot clone from GitHub. Pack from your laptop/UBVM and copy.

**On the laptop / UBVM:**

```bash
cd ~
tar czf overhead_tests.tgz overhead-tests
scp -O overhead_tests.tgz nutanix@<cvm_ip>:/home/nutanix/
```

(`-O` = legacy SCP; needed on many CVMs.)

**On the CVM:**

```bash
ssh nutanix@<cvm_ip>
cd ~
tar xzf overhead_tests.tgz
cd overhead-tests

##verify if succesfully unpacked
ls orchestrator.py bin/cgroup_cpu_snap
```

---

## Run (on the CVM)

Do **not** pass `--cvm` when you are already on the CVM.
Replace `<ahv_host>` with a name from `acli host.list`.

### 1. Validate

Checks CVM inventory, acli, SSH to AHV, host metadata, and the collector
binary. No VMs created, no measurement.

```bash
python3 orchestrator.py --host <ahv_host> --validate
```

### 2. First experiment

**Short redis** (good first proof; same fleet names as full redis):

```bash
python3 orchestrator.py --host <ahv_host> --config configs/test_quick.json
```

**Or full redis** (longer collect, more runs):

```bash
python3 orchestrator.py --host <ahv_host> --config configs/redis.json
```

If matching VMs already exist, the tool prompts: reuse / clear+recreate / abort.

### 3. Measure again (same fleet)

Only after a successful setup for **that same config**:

```bash
python3 orchestrator.py --host <ahv_host> \
  --config configs/test_quick.json --skip-setup --runs 1
```

Use the same `--config` as the fleet you built (`test_quick` and `redis` share
`redis_base` / `redis_vm_*`, so either works against that redis fleet).

Results go under `results_YYYYMMDD_HHMMSS/` in the current directory.

### Useful flags


| Flag               | Meaning                                                    |
| ------------------ | ---------------------------------------------------------- |
| `--validate`       | Connectivity + metadata only                               |
| `--setup-only`     | Setup (+ deploy collector); no experiment                  |
| `--skip-setup`     | Use existing clones for this config                        |
| `--clear-existing` | Delete matching base/clones, then setup                    |
| `--runs N`         | Override `num_runs`                                        |
| `--duration N`     | Override `collect_duration` (seconds after workload check) |
| `--interval N`     | Override sample interval                                   |
| `--host-verify`    | Also run cgtop/mpstat/sar during collect                   |
| `--version`        | Print CLI version                                          |


---

## Read the results

**Start with the terminal summary** — slice means and top services/UVMs over
`vms_on` ticks.

Then open CSVs under `results_*/` and filter on `VM_State == vms_on`.


| File                     | What it is                            |
| ------------------------ | ------------------------------------- |
| Terminal summary         | `vms_on` averages + top services/UVMs |
| `slices_*_runN.csv`      | Per tick × slice                      |
| `full_slices_*_runN.csv` | Same ticks, full metric fields        |
| `services_*_runN.csv`    | Per host service                      |
| `uvms_*_runN.csv`        | Per UVM                               |
| `events_*_runN.csv`      | Exact event times                     |
| `metadata_*_runN.json`   | Host / AHV / kernel / QEMU            |




### What `vms_off` / `vms_on` mean

During a run:

1. VMs stay **off** for `baseline_duration`
2. Mass **power-on** + soft workload check (up to ~60s)
3. Keep sampling for `collect_duration`
4. Power **off** again

Each sample tick is tagged from its **start** time vs the power-on command:

To caluclate averages shown in terminal summary we only use `vms_on` **ticks**.
Events mark exact times: `vms_on_cmd_sent`, `measure_started` (when
`collect_duration` begins), `collection_ended`, `vms_off_cmd_sent`.

### Details (slices and fields)


| Slice            | Meaning                            |
| ---------------- | ---------------------------------- |
| `ahv-cvm.slice`  | CVM on the AHV host                |
| `ahv-uvms.slice` | User / test VMs                    |
| `ahv.services`   | Host services under `system.slice` |



| Field                   | Meaning                               |
| ----------------------- | ------------------------------------- |
| `execution_cores`       | Running CPU (cores)                   |
| `ready_cores`           | Runnable waiting (cores)              |
| `Demand_s` / `Supply_s` | Wanted vs received CPU time (seconds) |


If fewer clones are powered on than created, that is often `clone_buffer`
headroom — the run continues (INFO, not a failure).

---

## Choose a config

Configs live in `configs/`. Edit or copy a file to change duration, VM names,
or workload knobs. Setup and Collect follow whatever you pass with `--config`.


| Config              | Workload   | VM names                             | interval / collect / runs | Notes                 |
| ------------------- | ---------- | ------------------------------------ | ------------------------- | --------------------- |
| `test_quick.json`   | redis      | `redis_base` / `redis_vm_*`          | 10s / 100s / 1            | Short first run       |
| `redis.json`        | redis      | same                                 | 15s / 600s / 3            | Full redis            |
| `redis_char.json`   | redis      | same                                 | 5s / 600s / 3             | Finer sample interval |
| `test.json`         | dirtyHarry | `dirty_harry_base` / `dirty_harry_*` | 30s / 480s / 3            | Own fleet             |
| `test_fio.json`     | fio        | `fio_base` / `fio_vm_*`              | 5s / 420s / 3             | randread, 8 jobs      |
| `fio_smallvm.json`  | fio        | `fio_small_*`                        | 5s / 420s / 3             | 3 GB VMs              |
| `fio_randrw.json`   | fio        | `fio_rw_*`                           | 5s / 420s / 3             | randrw                |
| `fio_highjobs.json` | fio        | `fio_hj_*`                           | 5s / 420s / 3             | 32 jobs               |


`test_quick`, `redis`, and `redis_char` share the same redis fleet. Fio and
dirtyHarry need their own setup.

### Important fields


| Field                           | Meaning                                  |
| ------------------------------- | ---------------------------------------- |
| `vm_size_gb`                    | Memory per clone                         |
| `clone_buffer`                  | Extra clones beyond free-memory estimate |
| `base_vm_name` / `clone_prefix` | Required names                           |
| `workload.type`                 | `redis`, `fio`, or `dirtyHarry`          |
| `interval`                      | Sample interval (seconds)                |
| `baseline_duration`             | VMs off before power-on                  |
| `collect_duration`              | Timed measure after workload check       |
| `num_runs`                      | Default 3                                |


Measurement is always `cgroup_cpu_snap` (no collector field in config).

---

## How it works

```text
orchestrator.py
  → stages/setup.py      create/clone VMs, install workload,
                         deploy bin/cgroup_cpu_snap → /root/ on AHV
  → stages/validate.py   CVM inventory + acli + SSH
  → stages/collect.py    start cgroup_cpu_snap
                         baseline → power-on → measure → stop (SIGINT)
  → stages/present.py    fetch log, tag vms_off/vms_on, terminal + CSV
```

The collector binary is vendored at `bin/cgroup_cpu_snap` and copied to the host
at setup/collect time. Rebuild notes: `bin/README.md`.

---

## Layout

```text
orchestrator.py       # public CLI
stages/               # setup, validate, collect, present
bin/cgroup_cpu_snap   # vendored eBPF collector
configs/              # experiment JSON
infra/                # CVM / host / VM / SSH helpers
workloads/            # guest workload installers
statsDump/            # terminal + CSV
```

---

## Clearing VMs

On a disposable one-node test cluster only:

```bash
acli vm.list
acli vm.delete * confirm=true
```

