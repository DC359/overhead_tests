from libx.objtmpl import StatsDumpTmpl

SLICE_NAMES = ["ahv-cvm.slice", "ahv-uvms.slice", "ahv.services"]

DISPLAY_FIELDS = [
    ("x_cores", "X Cores"),
    ("y_cores", "Y Cores"),
    ("xy_cores", "XY Cores"),
    ("Demand", "Demand(s)"),
    ("Supply", "Supply(s)"),
]


def fmt_ns_to_sec(val):
    if isinstance(val, (int, float)) and val > 0:
        return "%.2f" % (val / 1e9)
    return "0.00"


class terminalDump(StatsDumpTmpl):

    def dumpStats(self, title, stats):
        vm_count = title.get("vm_count", "?")
        run_num = title.get("run", "?")
        num_runs = title.get("num_runs", "?")
        num_cpus = title.get("num_cpus", "?")

        print("\n" + "=" * 120)
        print("OVERHEAD TEST RESULTS  |  VMs: %s  |  Cores: %s  |  Run: %s/%s" % (vm_count, num_cpus, run_num, num_runs))
        print("=" * 120)

        header = "%-10s %-10s %-10s %5s %8s %8s %-18s" % ("Time", "Clock", "Phase", "VMs", "mpstat%", "top%", "Slice")
        for _, label in DISPLAY_FIELDS:
            header += " %12s" % label
        print(header)
        print("-" * len(header))

        for tick in stats:
            wc = tick.get("wall_clock", "")
            vms = tick.get("vm_count", "")
            mpstat = tick.get("mpstat_cpu")
            top = tick.get("top_cpu")
            mpstat_str = "%.1f" % mpstat if mpstat is not None else ""
            top_str = "%.1f" % top if top is not None else ""
            first_slice = True
            for name in SLICE_NAMES:
                m = tick["slices"].get(name, {})
                if first_slice:
                    row = "%-10s %-10s %-10s %5s %8s %8s %-18s" % (
                        tick["time_delta"], wc, tick.get("phase", ""), vms, mpstat_str, top_str, name)
                    first_slice = False
                else:
                    row = "%-10s %-10s %-10s %5s %8s %8s %-18s" % ("", "", "", "", "", "", name)
                for field, _ in DISPLAY_FIELDS:
                    val = m.get(field, "")
                    if field in ("Demand", "Supply"):
                        row += " %12s" % fmt_ns_to_sec(val)
                    elif isinstance(val, float):
                        row += " %12.2f" % val
                    else:
                        row += " %12s" % str(val)
                print(row)

            cpustat_xcores = tick.get("cpustat_xcores", {})
            if cpustat_xcores:
                indent = " " * 53
                print("%s[VALIDATION] schedstat vs cpu.stat x_cores:" % indent)
                for name in SLICE_NAMES:
                    sched_xc = tick["slices"].get(name, {}).get("x_cores", 0)
                    cpustat_xc = cpustat_xcores.get(name, 0.0)
                    print("%s  %s: sched=%.2f  cpu.stat=%.2f  diff=%.2f" % (
                        indent, name, sched_xc, cpustat_xc, round(abs(sched_xc - cpustat_xc), 2)))
            print("")

        if stats and stats[-1].get("per_service"):
            print("\n--- Per-Service (last tick) ---")
            svc_header = "%-35s" % "Service"
            for _, label in DISPLAY_FIELDS:
                svc_header += " %12s" % label
            print(svc_header)
            print("-" * len(svc_header))

            last_tick = stats[-1]
            for svc_name, m in sorted(last_tick.get("per_service", {}).items()):
                row = "%-35s" % svc_name
                for field, _ in DISPLAY_FIELDS:
                    val = m.get(field, "")
                    if field in ("Demand", "Supply"):
                        row += " %12s" % fmt_ns_to_sec(val)
                    elif isinstance(val, float):
                        row += " %12.2f" % val
                    else:
                        row += " %12s" % str(val)
                print(row)

        print("\n" + "=" * 120)
