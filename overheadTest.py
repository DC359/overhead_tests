#!/usr/bin/env python3

from util.ObjFactory import *
from util.cvm import *
import time, sys, getopt, json, datetime

cvm_ip = ''
config_file = ''
host_name = ''


def wall_clock_now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def run_experiment(cvm, host, config, run_number):
    interval = config.get("interval", 5)
    baseline_duration = config.get("baseline_duration", 30)
    stable_ticks = config.get("stable_ticks", 24)
    max_warmup_duration = config.get("max_warmup_duration", 120)
    clone_prefix = config.get("clone_prefix")
    if not clone_prefix:
        print("ERROR: config is missing required field 'clone_prefix'.")
        sys.exit(2)
    pattern = "%s_*" % clone_prefix

    existing = cvm.countVmsMatching(clone_prefix)
    if existing == 0:
        print("ERROR: no VMs matching clone_prefix '%s*' found." % clone_prefix)
        print("  Create them first (omit --skip-setup), e.g.:")
        print("    python3 overhead.py --host %s --config %s"
              % (host_name, config_file or "<config.json>"))
        print("  or: python3 setupVms.py -H %s -f %s"
              % (host_name, config_file or "<config.json>"))
        sys.exit(1)
    print("Found %d VM(s) matching clone_prefix '%s*'." % (existing, clone_prefix))

    # The collector runs autonomously on the host. We sleep through the phases,
    # record a phase_timeline of (wall_clock, phase, vm_count), then fetch +
    # process once at the end (warmup->stable is detected retroactively).
    post_warmup_duration = max_warmup_duration + (stable_ticks * interval)

    print("\n========== RUN %d ==========\n" % run_number)
    print("Config: interval=%ds, baseline=%ds, max_warmup=%ds, stable_ticks=%d" % (
        interval, baseline_duration, max_warmup_duration, stable_ticks))

    # Ensure all VMs are off before starting
    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(pattern)
    time.sleep(10)

    # Setup collector
    collector_id = config.get("collector", "bpfsnap")
    print("[COLLECTOR] using '%s' (%s)" % (
        collector_id,
        "full eBPF snapshot+exit" if collector_id == "bpfsnap" else "schedstat /proc-poll + bpftrace"))
    collector = ObjFactory.getStatsCollectorObj(collector_id)
    collector.setup(host, interval)

    phase_timeline = []
    events = []

    # BASELINE (all VMs off)
    wc = wall_clock_now()
    events.append((wc, "collection_started"))
    phase_timeline.append((wc, "baseline", 0))
    print("[%s] --- BASELINE (all VMs off, %ds) ---" % (wc, baseline_duration))
    time.sleep(baseline_duration)

    # MASS POWER ON
    wc = wall_clock_now()
    events.append((wc, "vms_on_cmd_sent"))
    phase_timeline.append((wc, "power_on", 0))
    print("[%s] --- MASS POWER ON ---" % wc)
    cvm.vmOnAll(pattern)
    time.sleep(10)

    existing = cvm.countVmsMatching(clone_prefix)
    vm_count = cvm.countPoweredOnMatching(clone_prefix)
    print("VMs powered on: %d / %d matching clone_prefix '%s*'" % (
        vm_count, existing, clone_prefix))
    if vm_count == 0:
        print("ERROR: mass power-on left 0 VMs on (pattern %s)." % pattern)
        print("  Check that clones exist and acli vm.on succeeded.")
        collector.stop()
        sys.exit(1)
    if existing and vm_count < existing:
        print("WARNING: only %d of %d '%s*' VMs are on — others may still be "
              "booting or failed to start." % (vm_count, existing, clone_prefix))

    # --- VALIDATION: wait for guest IP, then check workload on a sample clone ---
    # Service name comes from config when set; else a small type→unit map.
    wl = config.get("workload", {}) or {}
    svc_names = {
        "dirtyHarry": "dirty-harry",
        "fio": "fio-workload",
        "redis": "redis-workload",
    }
    svc_name = wl.get("service") or svc_names.get(wl.get("type", ""), None)
    sample_vm_name = "%s_1" % clone_prefix
    sample_ip = ""
    try:
        sample_vm = Vm(sample_vm_name, cvm)
        print("[VALIDATION] waiting for IP on %s (up to 90s)..." % sample_vm_name)
        sample_ip = sample_vm.getIp(timeout_s=90)
    except Exception as e:
        print("[VALIDATION] Could not get IP for %s: %s" % (sample_vm_name, e))

    if sample_ip and svc_name:
        from libx.lib import run_remote_cmd
        try:
            svc_status = run_remote_cmd(
                sample_ip, "root",
                "systemctl is-active %s" % svc_name, use_password=False)
            svc_status = svc_status.decode().strip() if isinstance(svc_status, bytes) else svc_status.strip()
            print("[VALIDATION] %s on %s (%s): service=%s" % (
                svc_name, sample_vm_name, sample_ip, svc_status))
            if svc_status != "active":
                print("WARNING: workload service is not active — results may be invalid.")
        except Exception as e:
            print("[VALIDATION] Could not check %s on %s (%s): %s" % (
                svc_name, sample_vm_name, sample_ip, e))
    elif sample_ip and not svc_name:
        print("[VALIDATION] %s has IP %s (no workload.service / known type to check)" % (
            sample_vm_name, sample_ip))
    elif not sample_ip:
        print("WARNING: proceeding without guest workload check (no IP yet).")

    # COLLECTING (warmup + stable determined retroactively)
    wc = wall_clock_now()
    phase_timeline.append((wc, "collecting", vm_count))
    print("[%s] --- COLLECTING (VMs on: %d, waiting %ds for warmup+stable) ---" % (
        wc, vm_count, post_warmup_duration))
    time.sleep(post_warmup_duration)

    vm_count = cvm.countPoweredOnMatching(clone_prefix)
    print("[%s] VMs powered on (final): %d" % (wall_clock_now(), vm_count))

    wc = wall_clock_now()
    events.append((wc, "collection_ended"))
    collector.stop()

    # Power off after run
    print("[%s] Powering off all test VMs..." % wall_clock_now())
    events.append((wall_clock_now(), "vms_off_cmd_sent"))
    cvm.vmOffAll(pattern)

    print("\n[%s] Fetching results from host and detecting phases..." % wall_clock_now())
    warmup_cfg = {
        "threshold_pct": config.get("warmup_threshold_pct", 5),
        "consecutive": config.get("warmup_consecutive", 3),
        "stable_ticks": stable_ticks,
    }
    collector.fetch_and_process(phase_timeline, warmup_cfg=warmup_cfg)

    return collector.exportStats(), vm_count, collector._num_cpus, events


def run():
    with open(config_file) as f:
        config = json.load(f)

    num_runs = config.get("num_runs", 3)

    cvm = Cvm(cvm_ip)
    host = cvm.getHost(host_name)

    from util.host_meta import collect_host_metadata, format_metadata_lines
    print("\nCollecting host metadata...")
    metadata = collect_host_metadata(host, cvm)
    for line in format_metadata_lines(metadata):
        print("  %s" % line)

    all_runs = []
    all_events = []
    vm_count = 0
    num_cpus = 0

    for run_num in range(1, num_runs + 1):
        results, count, cpus, events = run_experiment(cvm, host, config, run_num)
        all_runs.append(results)
        all_events.append(events)
        vm_count = count
        num_cpus = cpus

    # Dump results for each run
    for i, run_results in enumerate(all_runs):
        title = {
            "vm_count": vm_count,
            "expected_on": vm_count,
            "host": host_name,
            "run": i + 1,
            "num_runs": num_runs,
            "num_cpus": num_cpus,
            "config": config,
            "events": all_events[i],
            "metadata": metadata,
        }

        print("\n========== RESULTS: RUN %d ==========\n" % (i + 1))
        terminal_dump = ObjFactory.getStatsDumpObj("terminalDump")
        terminal_dump.dumpStats(title, run_results)

        csv_dump = ObjFactory.getStatsDumpObj("csvDump")
        csv_dump.dumpStats(title, run_results)

    print("\nAll %d run(s) complete." % num_runs)


def main(argv):
    global cvm_ip, config_file, host_name

    helpmsg = 'overheadTest.py -i <cvmip> -f <configjson> -H <hostname>'
    try:
        opts, args = getopt.getopt(argv, "hi:f:H:", ["cvm=", "config=", "host="])
    except getopt.GetoptError:
        print(helpmsg)
        sys.exit(2)

    for opt, arg in opts:
        if opt == '-h':
            print(helpmsg)
            sys.exit()
        elif opt in ("-i", "--cvm"):
            cvm_ip = arg
        elif opt in ("-f", "--config"):
            config_file = arg
        elif opt in ("-H", "--host"):
            host_name = arg

    if config_file == '':
        config_file = 'sample/redis.json'
        print("No config provided, defaulting to %s" % config_file)

    if host_name == '':
        print("Host name is required.")
        print(helpmsg)
        sys.exit(2)

    if cvm_ip == '':
        print("No CVM IP provided, assuming running on CVM.")

    run()


if __name__ == "__main__":
    main(sys.argv[1:])
