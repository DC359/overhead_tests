#!/usr/bin/env python3
"""Present stage — VERSION 0.1.0

Parse cgroup_cpu_snap keyed raw logs, map slice names, tag ticks
vms_off/vms_on, dump terminal/CSV.
"""

from __future__ import print_function

import os
import sys

VERSION = "0.1.0"

SLICE_NAME_MAP = {
    "cvm": "ahv-cvm.slice",
    "uvm": "ahv-uvms.slice",
    "services": "ahv.services",
}

# Float vs int field names from cgroup_cpu_snap raw/JSON.
_FLOAT_KEYS = (
    "execution_cores", "ready_cores", "execution_ready_cores",
    "x_cores", "y_cores",  # legacy
)


def _parse_kv_fields(parts):
    """Parse tokens like delta_execution=123 into a dict of ints/floats."""
    out = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        if k in _FLOAT_KEYS:
            try:
                out[k] = float(v)
            except ValueError:
                continue
        else:
            try:
                out[k] = int(v)
            except ValueError:
                continue
    return out


def _normalize_fields(fields):
    """Map legacy X/Y/Z/x_cores names onto delta_* / execution_* keys."""
    if "delta_execution" not in fields and "X" in fields:
        fields["delta_execution"] = fields["X"]
    if "delta_ready" not in fields and "Y" in fields:
        fields["delta_ready"] = fields["Y"]
    if "delta_sleep" not in fields and "Z" in fields:
        fields["delta_sleep"] = fields["Z"]
    if "execution_cores" not in fields and "x_cores" in fields:
        fields["execution_cores"] = fields["x_cores"]
    if "ready_cores" not in fields and "y_cores" in fields:
        fields["ready_cores"] = fields["y_cores"]
    return fields


def parse_cgroup_raw(content):
    """Parse cgroup_cpu_snap --format raw output.

    Returns list of dicts with slices/services/uvms field maps (normalized names).
    """
    if isinstance(content, bytes):
        content = content.decode(errors="replace")

    ticks = []
    cur = None
    kind_to_key = {"SLICE": "slices", "SERVICE": "services", "UVM": "uvms"}

    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts:
            continue

        if parts[0] == "TICK" and len(parts) >= 5:
            mono_ns = 0
            for p in parts[5:]:
                if p.startswith("MONO=") and p.endswith("ns"):
                    try:
                        mono_ns = int(p[5:-2])
                    except ValueError:
                        pass
            try:
                cur = {
                    "tick": int(parts[1]),
                    "ts_start": parts[2],
                    "ts_end": parts[3],
                    "interval_s": int(parts[4]),
                    "mono_ns": mono_ns,
                    "slices": {},
                    "services": {},
                    "uvms": {},
                }
            except ValueError:
                cur = None
        elif parts[0] == "END_TICK":
            if cur is not None:
                ticks.append(cur)
            cur = None
        elif cur is not None and parts[0] in kind_to_key and len(parts) >= 3:
            name = parts[1]
            fields = _normalize_fields(_parse_kv_fields(parts[2:]))
            if "delta_execution" not in fields or "delta_ready" not in fields:
                continue
            if "tasks" not in fields:
                fields["tasks"] = 0
            cur[kind_to_key[parts[0]]][name] = fields

    return ticks


def _build_metrics(X, Y, N, num_cpus, interval, counted_tids):
    """Cores from wall interval; Demand/Supply from execution/ready/sleep."""
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
    execution_cores = round(X * 1.0 / INTERVAL_NS, 2) if INTERVAL_NS > 0 else 0.0
    ready_cores = round(Y * 1.0 / INTERVAL_NS, 2) if INTERVAL_NS > 0 else 0.0
    execution_ready_cores = round(execution_cores + ready_cores, 2)
    fractional_pct_supply = round(fractional * 100.0 / Supply, 2) if Supply > 0 else 0.0

    return {
        "delta_execution": X, "delta_ready": Y, "delta_sleep": Z,
        "T": T, "tasks_count": N,
        "counted_tids": counted_tids,
        "Supply": Supply, "Demand": Demand,
        "DemandSupplyRatio": DemandSupplyRatio,
        "pct_running_x": pct_running_x,
        "pct_readyq_y": pct_readyq_y,
        "pct_contention": pct_contention,
        "pct_cpu_util": pct_cpu_util,
        "execution_cores": execution_cores, "ready_cores": ready_cores,
        "execution_ready_cores": execution_ready_cores,
        "fractional": fractional,
        "fractional_pct_supply": fractional_pct_supply,
    }


def _metrics_from_snap(run, wait, count, t_ns, z_ns, demand_ns, num_cpus, interval):
    """Prefer BPF Demand/delta_sleep when present; else interval×N formula."""
    m = _build_metrics(run, wait, count, num_cpus, interval, count)
    if demand_ns > 0 or z_ns > 0 or t_ns > 0:
        m["delta_sleep"] = z_ns
        m["Demand"] = demand_ns if demand_ns > 0 else run
        m["Supply"] = run
        if t_ns > 0:
            m["T"] = t_ns / 1e9
        m["DemandSupplyRatio"] = (
            (m["Demand"] * 100) // m["Supply"] if m["Supply"] > 0 else 0)
        frac = m["Demand"] - run if m["Demand"] > run else 0
        m["fractional"] = frac
        m["fractional_pct_supply"] = (
            round(frac * 100.0 / m["Supply"], 2) if m["Supply"] > 0 else 0.0)
    return m


def _metrics_from_fields(fields, num_cpus, interval):
    """Prefer BPF Demand/delta_sleep when present; else interval×N formula."""
    X = fields.get("delta_execution", 0)
    Y = fields.get("delta_ready", 0)
    N = fields.get("tasks", 0)
    z = fields.get("delta_sleep", 0)
    demand = fields.get("Demand", 0)
    return _metrics_from_snap(X, Y, N, 0, z, demand, num_cpus, interval)


def build_tick_results(raw_ticks, state_timeline, num_cpus, interval):
    """Turn parsed raw ticks into dump-ready tick_result list.

    Tick state is vms_off or vms_on from Collect's timeline (from
    vms_on_cmd_sent).
    """

    def get_state_for_tick(wc):
        state = "vms_off"
        vm_count = 0
        for t_wc, t_state, t_vms in state_timeline:
            if wc >= t_wc:
                state = t_state
                vm_count = t_vms
            else:
                break
        return state, vm_count

    all_tick_results = []
    prev_metrics = {}

    for idx, rt in enumerate(raw_ticks):
        state, vm_count = get_state_for_tick(rt["ts_start"])
        tick_result = {
            "tick": idx + 1,
            "vm_state": state,
            "wall_clock": rt["ts_start"],
            "wall_clock_end": rt["ts_end"],
            "tick_delta_s": rt["interval_s"],
            "vm_count": vm_count,
            "time_delta": "%ds" % (idx * interval) if idx > 0 else "0s",
            "slices": {},
            "per_service": {},
            "per_uvm": {},
            "cpustat_xcores": {},
            "mono_ns_start": rt["mono_ns"],
            "mono_ns_end": 0,
            "verify": {},
            "churn": {"total": 0, "born_died": 0, "born_only": 0, "died_only": 0,
                      "by_service": {}, "by_comm": {}, "details": []},
        }

        iv = rt["interval_s"] or interval

        for short_name, fields in rt["slices"].items():
            disp = SLICE_NAME_MAP.get(short_name, short_name)
            metrics = _metrics_from_fields(fields, num_cpus, iv)
            # Prefer cores printed by binary when present
            if "execution_cores" in fields:
                metrics["execution_cores"] = fields["execution_cores"]
            if "ready_cores" in fields:
                metrics["ready_cores"] = fields["ready_cores"]
            if "execution_ready_cores" in fields:
                metrics["execution_ready_cores"] = fields["execution_ready_cores"]
            elif "execution_cores" in metrics and "ready_cores" in metrics:
                metrics["execution_ready_cores"] = round(
                    metrics["execution_cores"] + metrics["ready_cores"], 2)

            pm = prev_metrics.get(disp, {})
            metrics["pct_chg_delta_execution"] = (
                round((metrics["delta_execution"] - pm["delta_execution"]) * 100.0
                      / pm["delta_execution"], 2)
                if pm.get("delta_execution", 0) > 0 else None)
            metrics["pct_chg_delta_ready"] = (
                round((metrics["delta_ready"] - pm["delta_ready"]) * 100.0
                      / pm["delta_ready"], 2)
                if pm.get("delta_ready", 0) > 0 else None)
            metrics["pct_chg_delta_sleep"] = (
                round((metrics["delta_sleep"] - pm["delta_sleep"]) * 100.0
                      / pm["delta_sleep"], 2)
                if pm.get("delta_sleep", 0) > 0 else None)
            tick_result["slices"][disp] = metrics
            prev_metrics[disp] = {
                "delta_execution": metrics["delta_execution"],
                "delta_ready": metrics["delta_ready"],
                "delta_sleep": metrics["delta_sleep"],
            }

        for svc_name, fields in rt["services"].items():
            tick_result["per_service"][svc_name] = _metrics_from_fields(
                fields, num_cpus, iv)

        for uvm_uuid, fields in rt["uvms"].items():
            tick_result["per_uvm"][uvm_uuid] = _metrics_from_fields(
                fields, num_cpus, iv)

        all_tick_results.append(tick_result)

    n_on = sum(1 for tr in all_tick_results if tr.get("vm_state") == "vms_on")
    print("  Tagged %d vms_on / %d vms_off ticks" % (
        n_on, len(all_tick_results) - n_on))
    return all_tick_results


def dump_results(title, tick_results, dumpers=None):
    """Run configured dumpers (terminal + csv by default)."""
    if dumpers is None:
        from statsDump.terminalDump import terminalDump
        from statsDump.csvDump import csvDump
        dumpers = [terminalDump(), csvDump()]
    for d in dumpers:
        d.dumpStats(title, tick_results)


def run(raw_path=None, state_timeline=None, num_cpus=1, interval=5, title=None):
    """Present entry: parse raw_path, build tick results, optionally dump.

    Returns tick_results list (or None if raw_path is missing).
    """
    print("[stages.present] v%s" % VERSION)
    if not raw_path:
        print("  (no raw_path — nothing to present)")
        return None

    with open(raw_path, "r") as f:
        content = f.read()
    raw_ticks = parse_cgroup_raw(content)
    print("  Parsed %d ticks from %s" % (len(raw_ticks), raw_path))
    if not raw_ticks:
        print("WARNING: 0 ticks parsed")
        return []

    if state_timeline is None:
        state_timeline = [(raw_ticks[0]["ts_start"], "vms_on", 0)]

    tick_results = build_tick_results(
        raw_ticks, state_timeline, num_cpus, interval)

    if title is not None:
        dump_results(title, tick_results)
    return tick_results


def smoke_parse(path):
    """Parse a real fetched cgroup_cpu_snap log and print slice cores.

    Pass a host-fetched file (e.g. ~/cgroup_cpu_snap_fetched_run1.txt), not
    synthetic data — the repo does not ship sample logs.
    """
    with open(path) as f:
        content = f.read()
    ticks = parse_cgroup_raw(content)
    print("parsed %d ticks from %s" % (len(ticks), path))
    for t in ticks[:3]:
        print("  tick %d %s->%s" % (t["tick"], t["ts_start"], t["ts_end"]))
        for name, m in t["slices"].items():
            print("    %s: execution=%s ready=%s Demand=%s tasks=%s" % (
                name,
                m.get("delta_execution", m.get("X", 0)),
                m.get("delta_ready", m.get("Y", 0)),
                m.get("Demand", 0),
                m.get("tasks", 0)))
            print("      cores: exec=%.2f ready=%.2f" % (
                float(m.get("execution_cores", m.get("x_cores", 0))),
                float(m.get("ready_cores", m.get("y_cores", 0)))))


if __name__ == "__main__":
    # Allow `python3 stages/present.py <fetched_raw_log>` from repo root.
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    if len(sys.argv) < 2:
        print("usage: present.py <fetched_cgroup_cpu_snap_raw.txt>")
        print("  (use a real log from Collect, e.g. ~/cgroup_cpu_snap_fetched_run1.txt)")
        sys.exit(1)
    smoke_parse(sys.argv[1])
