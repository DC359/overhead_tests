from libx.objtmpl import StatsCollectorTmpl
from libx.lib import shell_run
import time
import tempfile
import os

SLICE_PATHS = [
    "/sys/fs/cgroup/ahv.slice/ahv-cvm.slice",
    "/sys/fs/cgroup/ahv.slice/ahv-uvms.slice",
    "/sys/fs/cgroup/system.slice",
]
SLICE_NAMES = ["ahv-cvm.slice", "ahv-uvms.slice", "ahv.services"]
SERVICE_PARENT = "/sys/fs/cgroup/system.slice"

REMOTE_SCRIPT_PATH = "/tmp/schedstat_collect.sh"

COLLECTION_SCRIPT = """#!/usr/bin/env bash
get_all_pids_under() {
  local dir="$1"
  local f="$dir/cgroup.procs"
  [[ -r "$f" ]] || return
  local pid sub
  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] && echo "$pid"
  done < "$f" 2>/dev/null
  for sub in "$dir"/*/; do
    [[ -d "$sub" ]] && get_all_pids_under "$sub"
  done
}

get_all_tids_under() {
  local dir="$1"
  local pid tid
  get_all_pids_under "$dir" | sort -u | while read -r pid; do
    [[ -z "$pid" ]] && continue
    for tid in /proc/$pid/task/*; do
      [[ -d "$tid" ]] && echo "${tid##*/}"
    done 2>/dev/null
  done | sort -u
}

SLICE_PATHS=("%s" "%s" "%s")
SLICE_NAMES=("%s" "%s" "%s")
SERVICE_PARENT="%s"

# --- VALIDATION START: launch mpstat and top in background ---
mpstat 1 1 2>/dev/null | awk '/all/ {used=100-$NF; idle=$NF} END {printf "MPSTAT %%.2f %%.2f\\n", used, idle}' > /tmp/_mpstat_out &
MPSTAT_PID=$!
top -bn2 2>/dev/null | awk -F',' '/%%Cpu/ {gsub(/[^0-9.]/,"",$4); idle=$4} END {printf "TOPSTAT %%.2f %%.2f\\n", 100-idle, idle}' > /tmp/_top_out &
TOP_PID=$!
# --- VALIDATION END ---

for i in 0 1 2; do
  sp="${SLICE_PATHS[$i]}"
  sn="${SLICE_NAMES[$i]}"
  [[ -d "$sp" ]] || continue
  while read -r tid; do
    [[ -z "$tid" ]] && continue
    f="/proc/$tid/schedstat"
    [[ -r "$f" ]] || continue
    read -r run wait _ < "$f" 2>/dev/null || continue
    echo "SLICE $sn $tid $run $wait"
  done < <(get_all_tids_under "$sp")
done

for svc_dir in "$SERVICE_PARENT"/*.service; do
  [[ -d "$svc_dir" ]] || continue
  svc_name="${svc_dir##*/}"
  while read -r tid; do
    [[ -z "$tid" ]] && continue
    f="/proc/$tid/schedstat"
    [[ -r "$f" ]] || continue
    read -r run wait _ < "$f" 2>/dev/null || continue
    echo "SERVICE $svc_name $tid $run $wait"
  done < <(get_all_tids_under "$svc_dir")
done

# --- VALIDATION START: per-slice cpu.stat usage_usec ---
for i in 0 1 2; do
  sp="${SLICE_PATHS[$i]}"
  sn="${SLICE_NAMES[$i]}"
  f="$sp/cpu.stat"
  if [[ -r "$f" ]]; then
    usec=$(grep "^usage_usec" "$f" | awk '{print $2}')
    echo "CPUSTAT $sn $usec"
  fi
done

# Wait for mpstat and top to finish, then output their results
wait $MPSTAT_PID 2>/dev/null
wait $TOP_PID 2>/dev/null
cat /tmp/_mpstat_out 2>/dev/null
cat /tmp/_top_out 2>/dev/null
rm -f /tmp/_mpstat_out /tmp/_top_out
# --- VALIDATION END ---
""" % (
    SLICE_PATHS[0], SLICE_PATHS[1], SLICE_PATHS[2],
    SLICE_NAMES[0], SLICE_NAMES[1], SLICE_NAMES[2],
    SERVICE_PARENT,
)


def parse_collection_output(output):
    slices = {}
    services = {}
    cpustat = {}
    # --- VALIDATION START ---
    mpstat_cpu = None
    top_cpu = None
    # --- VALIDATION END ---

    lines = output.split("\n") if isinstance(output, str) else output.decode().split("\n")

    for line in lines:
        stripped = line.strip()
        parts = stripped.split()

        # --- VALIDATION START: parse CPUSTAT, MPSTAT, TOPSTAT lines ---
        if len(parts) == 3 and parts[0] == "CPUSTAT":
            try:
                cpustat[parts[1]] = int(parts[2])
            except ValueError:
                pass
            continue

        if len(parts) == 3 and parts[0] == "MPSTAT":
            try:
                mpstat_cpu = float(parts[1])
            except ValueError:
                pass
            continue

        if len(parts) == 3 and parts[0] == "TOPSTAT":
            try:
                top_cpu = float(parts[1])
            except ValueError:
                pass
            continue
        # --- VALIDATION END ---

        if len(parts) != 5:
            continue

        group_type, group_name, tid, run, wait = parts
        try:
            tid = int(tid)
            run = int(run)
            wait = int(wait)
        except ValueError:
            continue

        if group_type == "SLICE":
            if group_name not in slices:
                slices[group_name] = {}
            slices[group_name][tid] = (run, wait)
        elif group_type == "SERVICE":
            if group_name not in services:
                services[group_name] = {}
            services[group_name][tid] = (run, wait)

    return slices, services, cpustat, mpstat_cpu, top_cpu


def compute_metrics(current_tids, prev_tids, num_cpus, interval):
    X = 0
    Y = 0
    new_prev = {}

    for tid, (run, wait) in current_tids.items():
        new_prev[tid] = (run, wait)
        if tid not in prev_tids:
            continue
        prev_run, prev_wait = prev_tids[tid]
        X += run - prev_run
        Y += wait - prev_wait

    N = len(current_tids)
    INTERVAL_NS = interval * 1000000000
    CPU_BUDGET_NS = interval * num_cpus * 1000000000
    T = interval * N
    T_ns = T * 1000000000
    Z = T_ns - X - Y if T_ns > (X + Y) else 0

    Supply = X
    if (X + Z) > 0:
        ratio_scaled = (X * 1000000) // (X + Z)
        fractional = (Y // 1000000) * ratio_scaled
        if fractional < 0:
            fractional = 0
        Demand = X + fractional
    else:
        fractional = 0
        Demand = X

    DemandSupplyRatio = (Demand * 100) // Supply if Supply > 0 else 0

    pct_running_x = round(X * 100.0 / T_ns, 2) if T_ns > 0 else 0.0
    pct_readyq_y = round(Y * 100.0 / T_ns, 2) if T_ns > 0 else 0.0
    pct_contention = round(Y * 100.0 / (X + Y), 2) if (X + Y) > 0 else 0.0
    pct_cpu_util = round(X * 100.0 / CPU_BUDGET_NS, 2) if CPU_BUDGET_NS > 0 else 0.0
    x_cores = round(X * 1.0 / INTERVAL_NS, 2) if INTERVAL_NS > 0 else 0.0
    y_cores = round(Y * 1.0 / INTERVAL_NS, 2) if INTERVAL_NS > 0 else 0.0
    xy_cores = round(x_cores + y_cores, 2)
    fractional_pct_supply = round(fractional * 100.0 / Supply, 2) if Supply > 0 else 0.0

    metrics = {
        "X": X, "Y": Y, "Z": Z, "T": T, "tasks_count": N,
        "Supply": Supply, "Demand": Demand,
        "DemandSupplyRatio": DemandSupplyRatio,
        "pct_running_x": pct_running_x,
        "pct_readyq_y": pct_readyq_y,
        "pct_contention": pct_contention,
        "pct_cpu_util": pct_cpu_util,
        "x_cores": x_cores, "y_cores": y_cores,
        "xy_cores": xy_cores,
        "fractional": fractional,
        "fractional_pct_supply": fractional_pct_supply,
    }

    return metrics, new_prev


class schedstatCollector(StatsCollectorTmpl):

    def setup(self, host, interval=30, label=""):  # IPERF_ADDITION: label param
        self._host = host
        self._interval = interval
        self._label = label  # IPERF_ADDITION
        self._results = []
        self._tick_count = 0
        self._prev_slices = {}
        self._prev_services = {}
        self._prev_metrics = {}

        out = self._host.host_cmd("nproc")
        self._num_cpus = int(out.strip()) if isinstance(out, str) else int(out.decode().strip())

        local_path = "/tmp/schedstat_collect.sh"
        with open(local_path, "w") as f:
            f.write(COLLECTION_SCRIPT)

        host_ip = self._host.getHostIp()
        shell_run("scp -o StrictHostKeyChecking=no %s root@%s:%s" % (local_path, host_ip, REMOTE_SCRIPT_PATH))
        print("Collection script deployed to host at %s" % REMOTE_SCRIPT_PATH)

        out = self._host.host_cmd("bash %s" % REMOTE_SCRIPT_PATH)
        slices, services, cpustat, _, _ = parse_collection_output(out)
        self._prev_cpustat = cpustat
        for name in SLICE_NAMES:
            self._prev_slices[name] = slices.get(name, {})
        for svc_name, tids in services.items():
            self._prev_services[svc_name] = tids

        print("Initial collection: %s" % ", ".join(
            "%s=%d tids" % (name, len(self._prev_slices.get(name, {}))) for name in SLICE_NAMES
        ))

    def collect_tick(self, phase="", wall_clock="", vm_count=0):
        self._tick_count += 1
        tick = self._tick_count

        out = self._host.host_cmd("bash %s" % REMOTE_SCRIPT_PATH)
        slices, services, cpustat, mpstat_cpu, top_cpu = parse_collection_output(out)

        # --- VALIDATION START: compute cpu.stat x_cores per slice ---
        cpustat_xcores = {}
        interval_usec = self._interval * 1000000
        for name in SLICE_NAMES:
            curr = cpustat.get(name, 0)
            prev = self._prev_cpustat.get(name, 0)
            delta = curr - prev
            cpustat_xcores[name] = round(delta * 1.0 / interval_usec, 2) if interval_usec > 0 else 0.0
        self._prev_cpustat = cpustat
        # --- VALIDATION END ---

        tick_result = {
            "tick": tick,
            "phase": phase,
            "wall_clock": wall_clock,
            "vm_count": vm_count,
            "time_delta": "%ds" % ((tick - 1) * self._interval) if tick > 1 else "0s",
            "slices": {},
            "per_service": {},
            "mpstat_cpu": mpstat_cpu,
            "top_cpu": top_cpu,
            "cpustat_xcores": cpustat_xcores,
        }

        for name in SLICE_NAMES:
            current = slices.get(name, {})
            prev = self._prev_slices.get(name, {})
            metrics, new_prev = compute_metrics(current, prev, self._num_cpus, self._interval)

            prev_m = self._prev_metrics.get(name, {})
            metrics["pct_chg_X"] = round((metrics["X"] - prev_m["X"]) * 100.0 / prev_m["X"], 2) if prev_m.get("X", 0) > 0 else None
            metrics["pct_chg_Y"] = round((metrics["Y"] - prev_m["Y"]) * 100.0 / prev_m["Y"], 2) if prev_m.get("Y", 0) > 0 else None
            metrics["pct_chg_Z"] = round((metrics["Z"] - prev_m["Z"]) * 100.0 / prev_m["Z"], 2) if prev_m.get("Z", 0) > 0 else None

            tick_result["slices"][name] = metrics
            self._prev_slices[name] = new_prev
            self._prev_metrics[name] = {"X": metrics["X"], "Y": metrics["Y"], "Z": metrics["Z"]}

        for svc_name, current in services.items():
            prev = self._prev_services.get(svc_name, {})
            metrics, new_prev = compute_metrics(current, prev, self._num_cpus, self._interval)
            tick_result["per_service"][svc_name] = metrics
            self._prev_services[svc_name] = new_prev

        self._results.append(tick_result)

        label_str = " [%s]" % self._label if self._label else ""
        wc_str = " %s" % wall_clock if wall_clock else ""
        vm_str = " VMs=%d" % vm_count if vm_count > 0 else ""
        mpstat_str = "mpstat=%.1f%%" % mpstat_cpu if mpstat_cpu is not None else "mpstat=N/A"
        top_str = "top=%.1f%%" % top_cpu if top_cpu is not None else "top=N/A"
        print("--- Tick %d [%s]%s %s%s%s | %s | %s ---" % (
            tick, tick_result["time_delta"], wc_str, phase, label_str, vm_str, mpstat_str, top_str))
        for name in SLICE_NAMES:
            m = tick_result["slices"][name]
            demand_s = round(m["Demand"] / 1e9, 2) if m["Demand"] else 0.0
            supply_s = round(m["Supply"] / 1e9, 2) if m["Supply"] else 0.0
            print("  %s: x_cores=%.2f y_cores=%.2f xy_cores=%.2f demand=%.2fs supply=%.2fs" % (
                name, m["x_cores"], m["y_cores"], m["xy_cores"], demand_s, supply_s))
        print("  [VALIDATION] schedstat vs cpu.stat x_cores:")
        for name in SLICE_NAMES:
            sched_xc = tick_result["slices"][name]["x_cores"]
            cpustat_xc = cpustat_xcores.get(name, 0.0)
            print("    %s: schedstat=%.2f  cpu.stat=%.2f  diff=%.2f" % (
                name, sched_xc, cpustat_xc, round(abs(sched_xc - cpustat_xc), 2)))

        return tick_result

    def runx(self, num_ticks, phase=""):
        for _ in range(num_ticks):
            time.sleep(self._interval)
            self.collect_tick(phase)

    def exportStats(self):
        return self._results
