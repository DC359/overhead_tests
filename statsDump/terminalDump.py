from infra.objtmpl import StatsDumpTmpl
from infra.host_meta import format_metadata_lines
import math

SLICE_NAMES = ["ahv-cvm.slice", "ahv-uvms.slice", "ahv.services"]

TOP_SERVICES_COUNT = 10
TOP_UVMS_COUNT = 10


def fmt_ns_to_sec(val):
    if isinstance(val, (int, float)) and val > 0:
        return "%.2f" % (val / 1e9)
    return "0.00"


def _mean_std(vals):
    if not vals:
        return None, None
    n = len(vals)
    mean = sum(vals) / n
    if n < 2:
        return mean, 0.0
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    return mean, math.sqrt(var)


class terminalDump(StatsDumpTmpl):

    def dumpStats(self, title, stats):
        vm_count = title.get("vm_count", "?")
        expected_on = title.get("expected_on", "?")
        run_num = title.get("run", "?")
        num_runs = title.get("num_runs", "?")
        num_cpus = title.get("num_cpus", "?")
        meta = title.get("metadata") or {}

        print("\n" + "=" * 90)
        print("OVERHEAD TEST RESULTS  |  VMs: %s  |  Expected: %s  |  Cores: %s  |  Run: %s/%s" % (
            vm_count, expected_on, num_cpus, run_num, num_runs))
        for line in format_metadata_lines(meta):
            print("  %s" % line)
        print("=" * 90)

        # --- Top services (averaged over vms_on ticks) ---
        on_svc = {}
        for tick in stats:
            if tick.get("vm_state") != "vms_on":
                continue
            for svc_name, m in tick.get("per_service", {}).items():
                xc = m.get("execution_cores")
                if isinstance(xc, (int, float)):
                    on_svc.setdefault(svc_name, []).append(float(xc))

        if on_svc:
            svc_avg = []
            for svc_name, vals in on_svc.items():
                avg = sum(vals) / len(vals)
                if avg >= 0.005:
                    svc_avg.append((svc_name, avg))
            svc_avg.sort(key=lambda x: -x[1])
            top = svc_avg[:TOP_SERVICES_COUNT]
            if top:
                print("\n--- Top %d services by execution_cores (vms_on avg) ---" % min(TOP_SERVICES_COUNT, len(top)))
                print("%-40s %10s" % ("Service", "execution_cores"))
                print("-" * 52)
                for svc_name, avg in top:
                    print("%-40s %10.2f" % (svc_name, avg))

        # --- Top UVMs (averaged over vms_on ticks) ---
        on_uvm = {}
        for tick in stats:
            if tick.get("vm_state") != "vms_on":
                continue
            for uvm_uuid, m in tick.get("per_uvm", {}).items():
                xc = m.get("execution_cores")
                if isinstance(xc, (int, float)):
                    on_uvm.setdefault(uvm_uuid, []).append(float(xc))

        if on_uvm:
            uvm_avg = []
            for uvm_uuid, vals in on_uvm.items():
                avg = sum(vals) / len(vals)
                if avg >= 0.005:
                    uvm_avg.append((uvm_uuid, avg))
            uvm_avg.sort(key=lambda x: -x[1])
            top = uvm_avg[:TOP_UVMS_COUNT]
            if top:
                print("\n--- Top %d UVMs by execution_cores (vms_on avg) ---" % min(TOP_UVMS_COUNT, len(top)))
                print("%-40s %10s" % ("UVM_UUID", "execution_cores"))
                print("-" * 52)
                for uvm_uuid, avg in top:
                    print("%-40s %10.2f" % (uvm_uuid, avg))

        # --- vms_on summary ---
        on_ticks = [t for t in stats if t.get("vm_state") == "vms_on"]
        if on_ticks:
            print("\n--- VMs on summary (mean ± stdev) ---")
            print("%-18s %12s %12s %12s %6s" % ("Slice", "execution_cores", "Demand(s)", "Supply(s)", "n"))
            for name in SLICE_NAMES:
                xs, ds, ss = [], [], []
                for tick in on_ticks:
                    m = tick.get("slices", {}).get(name, {})
                    if not m:
                        continue
                    if isinstance(m.get("execution_cores"), (int, float)):
                        xs.append(float(m["execution_cores"]))
                    if isinstance(m.get("Demand"), (int, float)):
                        ds.append(m["Demand"] / 1e9)
                    if isinstance(m.get("Supply"), (int, float)):
                        ss.append(m["Supply"] / 1e9)
                if not xs:
                    continue
                xm, xsdev = _mean_std(xs)
                dm, dsdev = _mean_std(ds)
                sm, ssdev = _mean_std(ss)
                print("%-18s %6.2f±%-4.2f %6.2f±%-4.2f %6.2f±%-4.2f %6d" % (
                    name, xm, xsdev, dm, dsdev, sm, ssdev, len(xs)))
        else:
            print("\n[WARNING] No vms_on ticks recorded — cannot produce summary.")

        print("\n" + "=" * 90)
