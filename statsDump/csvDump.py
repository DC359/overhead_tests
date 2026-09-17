from libx.objtmpl import StatsDumpTmpl
from util.host_meta import metadata_csv_comments
import csv
import time
import os
import json

ALL_SLICE_FIELDS = [
    "X", "Y", "Z", "T", "tasks_count", "counted_tids",
    "Supply", "Demand", "DemandSupplyRatio",
    "pct_running_x", "pct_readyq_y", "pct_contention", "pct_cpu_util",
    "x_cores", "y_cores", "xy_cores",
    "fractional", "fractional_pct_supply",
    "pct_chg_X", "pct_chg_Y", "pct_chg_Z",
    "ephemeral_x_cores", "ephemeral_y_cores", "ephemeral_count", "total_x_cores",
]



def ns_to_sec(val):
    if isinstance(val, (int, float)) and val > 0:
        return round(val / 1e9, 4)
    return 0.0


def _write_meta_comments(w, title):
    expected_on = title.get("expected_on", "")
    w.writerow(["# Expected_On: %s" % expected_on])
    for row in metadata_csv_comments(title.get("metadata") or {}):
        w.writerow([row])


class csvDump(StatsDumpTmpl):

    def dumpStats(self, title, stats):
        vm_count = title.get("vm_count", "")
        expected_on = title.get("expected_on", "")
        run_num = title.get("run", 1)
        host_tag = title.get("host", "")
        host_suffix = "_%s" % host_tag if host_tag else ""

        outdir = "results_%s" % time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(outdir, exist_ok=True)

        meta = title.get("metadata") or {}
        if meta:
            meta_path = os.path.join(outdir, "metadata%s_run%d.json" % (host_suffix, run_num))
            with open(meta_path, "w") as mf:
                json.dump(meta, mf, indent=2, sort_keys=True)
            print("Metadata JSON saved to %s" % meta_path)

        slices_file = os.path.join(outdir, "slices%s_run%d.csv" % (host_suffix, run_num))
        with open(slices_file, "w") as f:
            w = csv.writer(f)
            _write_meta_comments(w, title)
            w.writerow(["Wall_Clock", "Elapsed", "VM_Count", "Phase", "Slice",
                         "x_cores", "y_cores", "xy_cores",
                         "Demand_s", "Supply_s", "total_x_cores"])
            for tick in stats:
                for slice_name, metrics in tick["slices"].items():
                    row = [
                        tick.get("wall_clock", ""),
                        tick["time_delta"],
                        tick.get("vm_count", vm_count),
                        tick.get("phase", ""),
                        slice_name,
                        metrics.get("x_cores", ""),
                        metrics.get("y_cores", ""),
                        metrics.get("xy_cores", ""),
                        ns_to_sec(metrics.get("Demand", 0)),
                        ns_to_sec(metrics.get("Supply", 0)),
                        metrics.get("total_x_cores", ""),
                    ]
                    w.writerow(row)
        print("Slices CSV saved to %s" % slices_file)

        services_file = os.path.join(outdir, "services%s_run%d.csv" % (host_suffix, run_num))
        with open(services_file, "w") as f:
            w = csv.writer(f)
            _write_meta_comments(w, title)
            w.writerow(["Wall_Clock", "Elapsed", "VM_Count", "Phase", "Service",
                         "x_cores", "y_cores", "xy_cores", "Demand_s", "Supply_s",
                         "ephemeral_x_cores", "ephemeral_y_cores", "ephemeral_count",
                         "total_x_cores", "total_y_cores"])
            for tick in stats:
                for svc_name, metrics in tick.get("per_service", {}).items():
                    row = [
                        tick.get("wall_clock", ""),
                        tick["time_delta"],
                        tick.get("vm_count", vm_count),
                        tick.get("phase", ""),
                        svc_name,
                        metrics.get("x_cores", ""),
                        metrics.get("y_cores", ""),
                        metrics.get("xy_cores", ""),
                        ns_to_sec(metrics.get("Demand", 0)),
                        ns_to_sec(metrics.get("Supply", 0)),
                        metrics.get("ephemeral_x_cores", ""),
                        metrics.get("ephemeral_y_cores", ""),
                        metrics.get("ephemeral_count", ""),
                        metrics.get("total_x_cores", ""),
                        metrics.get("total_y_cores", ""),
                    ]
                    w.writerow(row)
        print("Services CSV saved to %s" % services_file)

        events = title.get("events", [])
        event_map = {}
        for ts, label in events:
            event_map.setdefault(ts, []).append(label)

        full_file = os.path.join(outdir, "full_slices%s_run%d.csv" % (host_suffix, run_num))
        with open(full_file, "w") as f:
            w = csv.writer(f)
            _write_meta_comments(w, title)
            w.writerow(["Wall_Clock", "Elapsed", "VM_Count", "Phase", "Slice"] + ALL_SLICE_FIELDS + ["Event"])
            emitted_events = set()
            for tick in stats:
                wc = tick.get("wall_clock", "")
                if wc in event_map:
                    for ev_label in event_map[wc]:
                        if ev_label not in emitted_events:
                            ev_row = [wc, tick["time_delta"], "", "", ">>> EVENT"] + [""] * len(ALL_SLICE_FIELDS) + [ev_label]
                            w.writerow(ev_row)
                            emitted_events.add(ev_label)
                for slice_name, metrics in tick["slices"].items():
                    row = [
                        wc,
                        tick["time_delta"],
                        tick.get("vm_count", vm_count),
                        tick.get("phase", ""),
                        slice_name,
                    ]
                    for field in ALL_SLICE_FIELDS:
                        val = metrics.get(field)
                        row.append(val if val is not None else "")
                    row.append("")
                    w.writerow(row)
            for ts, label in events:
                if label not in emitted_events:
                    ev_row = [ts, "", "", "", ">>> EVENT"] + [""] * len(ALL_SLICE_FIELDS) + [label]
                    w.writerow(ev_row)
        print("Full data CSV saved to %s" % full_file)

        events_file = os.path.join(outdir, "events%s_run%d.csv" % (host_suffix, run_num))
        with open(events_file, "w") as f:
            w = csv.writer(f)
            w.writerow(["Wall_Clock", "Event"])
            for ts, label in events:
                w.writerow([ts, label])
        print("Events CSV saved to %s" % events_file)

        has_churn = any(tick.get("churn", {}).get("details") for tick in stats)
        if has_churn:
            bpf_exits_file = os.path.join(outdir, "bpf_exits%s_run%d.csv" % (host_suffix, run_num))
            with open(bpf_exits_file, "w") as f:
                w = csv.writer(f)
                w.writerow(["Tick", "Wall_Clock", "Phase", "PID", "Comm",
                             "Service", "Run_ns", "Wait_ns"])
                for tick in stats:
                    churn = tick.get("churn", {})
                    for info in churn.get("details", []):
                        w.writerow([
                            tick.get("tick", ""),
                            tick.get("wall_clock", ""),
                            tick.get("phase", ""),
                            info.get("pid", ""),
                            info.get("comm", ""),
                            info.get("cgroup", ""),
                            info.get("run_ns", ""),
                            info.get("wait_ns", ""),
                        ])
            print("BPF exits CSV saved to %s" % bpf_exits_file)

        has_verify = any(tick.get("verify") for tick in stats)
        if has_verify:
            merged_file = os.path.join(outdir, "verification_vs_bpf%s_run%d.csv" % (host_suffix, run_num))
            with open(merged_file, "w") as f:
                w = csv.writer(f)
                w.writerow([
                    "Tick", "Wall_Clock", "Wall_Clock_End", "Phase",
                    "Service",
                    "cpu.stat_ns", "schedstat_ns", "bpf_eph_ns", "total_ns",
                    "Diff_pct", "TotalDiff_pct",
                    "bpf_exits", "bpf_hwm_dedup", "bpf_nested",
                    "bpf_depth_1", "bpf_depth_2", "bpf_depth_3", "bpf_depth_4", "bpf_depth_5",
                    "sanity_warning",
                ])
                for tick in stats:
                    verify = tick.get("verify", {})
                    if not verify:
                        continue
                    bpf_dd = tick.get("bpf_dedup", {})
                    dc = bpf_dd.get("depth_counts", {})
                    for svc, v in sorted(verify.items()):
                        cpustat = v["cpustat_ns"]
                        sched = v["schedstat_ns"]
                        diff_pct = round((sched - cpustat) * 100.0 / cpustat, 1) if cpustat > 0 else 0.0

                        svc_metrics = tick.get("per_service", {}).get(svc, {})
                        eph_xc = svc_metrics.get("ephemeral_x_cores", 0) or 0
                        tick_delta = tick.get("tick_delta_s", 5)
                        if tick_delta <= 0:
                            tick_delta = 5
                        eph_ns = int(eph_xc * tick_delta * 1000000000)
                        total_ns = sched + eph_ns
                        total_diff_pct = round((total_ns - cpustat) * 100.0 / cpustat, 1) if cpustat > 0 else 0.0

                        w.writerow([
                            tick.get("tick", ""),
                            tick.get("wall_clock", ""),
                            tick.get("wall_clock_end", ""),
                            tick.get("phase", ""),
                            svc,
                            cpustat, sched, eph_ns, total_ns,
                            diff_pct, total_diff_pct,
                            svc_metrics.get("ephemeral_count", 0) or 0,
                            bpf_dd.get("hwm_dedup", 0),
                            bpf_dd.get("nested", 0),
                            dc.get(1, 0), dc.get(2, 0), dc.get(3, 0), dc.get(4, 0), dc.get(5, 0),
                            tick.get("sanity_warning", ""),
                        ])
            print("Verification vs BPF CSV saved to %s" % merged_file)

        bpf_summary = title.get("bpf_summary", {})
        if bpf_summary:
            summary_file = os.path.join(outdir, "bpf_summary%s_run%d.csv" % (host_suffix, run_num))
            with open(summary_file, "w") as f:
                w = csv.writer(f)
                w.writerow(["metric", "value"])
                w.writerow(["total_system_slice_exits", bpf_summary.get("total_events", 0)])
                global_dc = bpf_summary.get("depth_counts", {})
                for d in sorted(global_dc.keys()):
                    w.writerow(["depth_%d" % d, global_dc[d]])
                w.writerow(["non_system_slice_exits", bpf_summary.get("non_sys_exits", 0)])
            print("BPF summary CSV saved to %s" % summary_file)
