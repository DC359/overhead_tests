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
    clone_prefix = config.get("clone_prefix", "dirty_harry")
    pattern = "%s_*" % clone_prefix

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

    vm_count = cvm.countPoweredOnVms()
    print("VMs powered on: %d" % vm_count)

    # --- VALIDATION: check workload on a sample clone ---
    svc_names = {"dirtyHarry": "dirty-harry", "fio": "fio-workload"}
    svc_name = svc_names.get(config.get("workload", {}).get("type", ""), "unknown")
    sample_vm_name = "%s_1" % clone_prefix
    detail = cvm.getVmDetail(sample_vm_name)
    sample_ip = detail.get("VM IP Addresses", "").split(",")[0].strip() if detail else ""
    if sample_ip:
        from libx.lib import run_remote_cmd
        try:
            svc_status = run_remote_cmd(sample_ip, "root", "systemctl is-active %s" % svc_name, use_password=True)
            svc_status = svc_status.decode().strip() if isinstance(svc_status, bytes) else svc_status.strip()
            print("[VALIDATION] %s on %s (%s): service=%s" % (svc_name, sample_vm_name, sample_ip, svc_status))
        except Exception as e:
            print("[VALIDATION] Could not check %s on %s (%s): %s" % (svc_name, sample_vm_name, sample_ip, e))
    else:
        print("[VALIDATION] Could not get IP for %s to check %s" % (sample_vm_name, svc_name))

    # COLLECTING (warmup + stable determined retroactively)
    wc = wall_clock_now()
    phase_timeline.append((wc, "collecting", vm_count))
    print("[%s] --- COLLECTING (VMs on: %d, waiting %ds for warmup+stable) ---" % (
        wc, vm_count, post_warmup_duration))
    time.sleep(post_warmup_duration)

    vm_count = cvm.countPoweredOnVms()
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

    num_runs = config.get("num_runs", 1)

    cvm = Cvm(cvm_ip)
    host = cvm.getHost(host_name)

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
            "host": host_name,
            "run": i + 1,
            "num_runs": num_runs,
            "num_cpus": num_cpus,
            "config": config,
            "events": all_events[i],
        }

        print("\n========== RESULTS: RUN %d ==========\n" % (i + 1))
        terminal_dump = ObjFactory.getStatsDumpObj("terminalDump")
        terminal_dump.dumpStats(title, run_results)

        csv_dump = ObjFactory.getStatsDumpObj("csvDump")
        csv_dump.dumpStats(title, run_results)

    print("\nAll %d run(s) complete." % num_runs)
    # REMINDER: Change num_runs to 3 in test.json once testing is validated.


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

    if config_file == '' or host_name == '':
        print("Config file and host name are required.")
        print(helpmsg)
        sys.exit(2)

    if cvm_ip == '':
        print("No CVM IP provided, assuming running on CVM.")

    run()


if __name__ == "__main__":
    main(sys.argv[1:])
