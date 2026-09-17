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
        expected_on = title.get("expected_on", "?")
        run_num = title.get("run", "?")
        num_runs = title.get("num_runs", "?")
        num_cpus = title.get("num_cpus", "?")

        print("\n" + "=" * 120)
        print("OVERHEAD TEST RESULTS  |  VMs: %s  |  Expected: %s  |  Cores: %s  |  Run: %s/%s" % (
            vm_count, expected_on, num_cpus, run_num, num_runs))
        print("=" * 120)

        header = "%-10s %-10s %-10s %5s %-18s" % ("Time", "Clock", "Phase", "VMs", "Slice")
        for _, label in DISPLAY_FIELDS:
            header += " %12s" % label
        print(header)
        print("-" * len(header))

        for tick in stats:
            wc = tick.get("wall_clock", "")
            vms = tick.get("vm_count", "")
            first_slice = True
            for name in SLICE_NAMES:
                m = tick["slices"].get(name, {})
                if not m:
                    continue
                if first_slice:
                    row = "%-10s %-10s %-10s %5s %-18s" % (
                        tick["time_delta"], wc, tick.get("phase", ""), vms, name)
                    first_slice = False
                else:
                    row = "%-10s %-10s %-10s %5s %-18s" % ("", "", "", "", name)
                for field, _ in DISPLAY_FIELDS:
                    val = m.get(field, "")
                    if field in ("Demand", "Supply"):
                        row += " %12s" % fmt_ns_to_sec(val)
                    elif isinstance(val, float):
                        row += " %12.2f" % val
                    else:
                        row += " %12s" % str(val)
                eph_xc = m.get("ephemeral_x_cores")
                total_xc = m.get("total_x_cores")
                if total_xc is not None:
                    row += "  eph=%.3f total=%.3f" % (eph_xc or 0, total_xc)
                print(row)

            print("")

        if stats and stats[-1].get("per_service"):
            print("\n--- Per-Service (last tick) ---")
            svc_header = "%-35s" % "Service"
            for _, label in DISPLAY_FIELDS:
                svc_header += " %12s" % label
            svc_header += " %10s %10s %6s" % ("Eph_X", "Total_X", "Eph#")
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
                eph_xc = m.get("ephemeral_x_cores")
                total_xc = m.get("total_x_cores")
                eph_cnt = m.get("ephemeral_count")
                row += " %10s" % ("%.4f" % eph_xc if eph_xc else "")
                row += " %10s" % ("%.4f" % total_xc if total_xc else "")
                row += " %6s" % (str(eph_cnt) if eph_cnt else "")
                print(row)

        print("\n" + "=" * 120)
