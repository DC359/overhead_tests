from libx.objtmpl import StatsDumpTmpl
import csv
import time
import os

DISPLAY_FIELDS = [
    "x_cores", "y_cores", "xy_cores", "Demand", "Supply",
]

ALL_SLICE_FIELDS = [
    "X", "Y", "Z", "T", "tasks_count",
    "Supply", "Demand", "DemandSupplyRatio",
    "pct_running_x", "pct_readyq_y", "pct_contention", "pct_cpu_util",
    "x_cores", "y_cores", "xy_cores",
    "fractional", "fractional_pct_supply",
    "pct_chg_X", "pct_chg_Y", "pct_chg_Z",
]

SERVICE_FIELDS = [
    "x_cores", "y_cores", "xy_cores", "Demand", "Supply",
]


def ns_to_sec(val):
    if isinstance(val, (int, float)) and val > 0:
        return round(val / 1e9, 4)
    return 0.0


class csvDump(StatsDumpTmpl):

    def dumpStats(self, title, stats):
        vm_count = title.get("vm_count", "")
        run_num = title.get("run", 1)
        host_tag = title.get("host", "")
        host_suffix = "_%s" % host_tag if host_tag else ""

        outdir = "results_%s" % time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(outdir, exist_ok=True)

        slices_file = os.path.join(outdir, "slices%s_run%d.csv" % (host_suffix, run_num))
        with open(slices_file, "w") as f:
            w = csv.writer(f)
            w.writerow(["Wall_Clock", "Elapsed", "VM_Count", "Phase",
                         "mpstat_cpu", "top_cpu", "Slice",
                         "x_cores", "y_cores", "xy_cores",
                         "Demand_s", "Supply_s", "cpustat_xcores"])
            for tick in stats:
                cpustat_xcores = tick.get("cpustat_xcores", {})
                for slice_name, metrics in tick["slices"].items():
                    row = [
                        tick.get("wall_clock", ""),
                        tick["time_delta"],
                        tick.get("vm_count", vm_count),
                        tick.get("phase", ""),
                        tick.get("mpstat_cpu", ""),
                        tick.get("top_cpu", ""),
                        slice_name,
                        metrics.get("x_cores", ""),
                        metrics.get("y_cores", ""),
                        metrics.get("xy_cores", ""),
                        ns_to_sec(metrics.get("Demand", 0)),
                        ns_to_sec(metrics.get("Supply", 0)),
                        cpustat_xcores.get(slice_name, ""),
                    ]
                    w.writerow(row)
        print("Slices CSV saved to %s" % slices_file)

        services_file = os.path.join(outdir, "services%s_run%d.csv" % (host_suffix, run_num))
        with open(services_file, "w") as f:
            w = csv.writer(f)
            w.writerow(["Wall_Clock", "Elapsed", "VM_Count", "Phase", "Service",
                         "x_cores", "y_cores", "xy_cores", "Demand_s", "Supply_s"])
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
                    ]
                    w.writerow(row)
        print("Services CSV saved to %s" % services_file)

        full_file = os.path.join(outdir, "full_slices%s_run%d.csv" % (host_suffix, run_num))
        with open(full_file, "w") as f:
            w = csv.writer(f)
            w.writerow(["Wall_Clock", "Elapsed", "VM_Count", "Phase", "mpstat_cpu", "top_cpu", "Slice"] + ALL_SLICE_FIELDS)
            for tick in stats:
                for slice_name, metrics in tick["slices"].items():
                    row = [
                        tick.get("wall_clock", ""),
                        tick["time_delta"],
                        tick.get("vm_count", vm_count),
                        tick.get("phase", ""),
                        tick.get("mpstat_cpu", ""),
                        tick.get("top_cpu", ""),
                        slice_name,
                    ]
                    for field in ALL_SLICE_FIELDS:
                        val = metrics.get(field)
                        row.append(val if val is not None else "")
                    w.writerow(row)
        print("Full data CSV saved to %s" % full_file)
