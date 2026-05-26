#!/usr/bin/env python3

VERSION = "0.6"

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
    baseline_duration = config.get("baseline_duration", 15)
    stable_ticks = config.get("stable_ticks", 24)
    max_warmup_duration = config.get("max_warmup_duration", 300)
    clone_prefix = config.get("clone_prefix", "dirty_harry")
    pattern = "%s_*" % clone_prefix

    post_poweron_duration = max_warmup_duration + (stable_ticks * interval)

    print("\n========== RUN %d ==========\n" % run_number)
    print("Config: interval=%ds, baseline=%ds, max_warmup=%ds, stable_ticks=%d" % (
        interval, baseline_duration, max_warmup_duration, stable_ticks))
    print("Total collection after power-on: %ds" % post_poweron_duration)

    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(pattern)
    time.sleep(10)

    workload_type = config.get("workload", {}).get("type", "")
    clone_buffer = 0 if workload_type == "iperf" else config.get("clone_buffer", 5)

    base_on_count = cvm.countPoweredOnVms()
    total_test_vms = sum(1 for vm in cvm.getVmDetails()
                         if vm.get('Name', '').startswith(clone_prefix + "_")
                         and vm.get('Name', '')[len(clone_prefix) + 1:].isdigit())
    expected_on = base_on_count + total_test_vms - clone_buffer
    print("Base VMs on: %d, Test VMs: %d, Buffer: %d, Expected after power-on: %d" % (
        base_on_count, total_test_vms, clone_buffer, expected_on))

    collector = ObjFactory.getStatsCollectorObj("schedstat")
    collector.setup(host, interval)

    phase_timeline = []
    events = []

    # BASELINE
    wc = wall_clock_now()
    events.append((wc, "collection_started"))
    phase_timeline.append((wc, "baseline", 0))
    print("[%s] --- BASELINE (all VMs off, %ds) ---" % (wc, baseline_duration))
    time.sleep(baseline_duration)

    # MASS POWER ON
    wc_cmd_sent = wall_clock_now()
    events.append((wc_cmd_sent, "vms_on_cmd_sent"))
    phase_timeline.append((wc_cmd_sent, "power_on", 0))
    print("[%s] --- MASS POWER ON (cmd sent) ---" % wc_cmd_sent)
    on_out = cvm.vmOnAll(pattern)
    wc_api_returned = wall_clock_now()
    print("[%s] --- vmOnAll API returned ---" % wc_api_returned)
    if on_out:
        out_lower = on_out.lower()
        if any(kw in out_lower for kw in ["not enough", "insufficient", "cannot", "kOutOfMemory"]):
            print("[WARNING] Host resources exhausted — some VMs failed to power on")

    wc_all_on = None
    poll_start = time.time()
    for _ in range(30):
        time.sleep(1)
        vm_count = cvm.countPoweredOnVms()
        if vm_count >= expected_on:
            wc_all_on = wall_clock_now()
            events.append((wc_all_on, "all_vms_on"))
            print("[%s] All VMs powered on (%d/%d)" % (wc_all_on, vm_count, expected_on))
            break

    poll_elapsed = time.time() - poll_start
    if wc_all_on is None:
        vm_count = cvm.countPoweredOnVms()
        print("[%s] Power-on check timed out (30s). VMs on: %d / expected: %d" % (
            wall_clock_now(), vm_count, expected_on))

    wc = wall_clock_now()
    phase_timeline.append((wc, "collecting", vm_count))
    print("[%s] --- COLLECTING (VMs on: %d, waiting %ds for warmup+stable) ---" % (
        wc, vm_count, post_poweron_duration))

    remaining_sleep = max(0, post_poweron_duration - poll_elapsed)
    time.sleep(remaining_sleep)

    vm_count = cvm.countPoweredOnVms()
    print("[%s] VMs powered on (final): %d" % (wall_clock_now(), vm_count))

    wc = wall_clock_now()
    events.append((wc, "collection_ended"))
    collector.stop()

    wc = wall_clock_now()
    events.append((wc, "vms_off_cmd_sent"))
    print("[%s] Powering off all test VMs..." % wc)
    cvm.vmOffAll(pattern)

    host_ip = host.getHostIp()
    scp_opts = "-o StrictHostKeyChecking=no"
    if host._control_active:
        scp_opts += " -o ControlPath=%s" % host._control_socket
    from libx.lib import shell_run
    from statsCollector.schedstatCollector import LOCAL_FETCH_DIR
    shell_run("scp %s root@%s:/tmp/cgtop_log.txt %s/cgtop_log_fetched.txt" % (scp_opts, host_ip, LOCAL_FETCH_DIR))
    shell_run("scp %s root@%s:/tmp/mpstat_log.txt %s/mpstat_log_fetched.txt" % (scp_opts, host_ip, LOCAL_FETCH_DIR))
    shell_run("scp %s root@%s:/tmp/sar_log.txt %s/sar_log_fetched.txt" % (scp_opts, host_ip, LOCAL_FETCH_DIR))
    print("[%s] Fetched cgtop, mpstat, sar -> %s/*_fetched.txt" % (wall_clock_now(), LOCAL_FETCH_DIR))

    print("\n[%s] Fetching results from host and detecting phases..." % wall_clock_now())
    warmup_cfg = {
        "threshold_pct": config.get("warmup_threshold_pct", 5),
        "consecutive": config.get("warmup_consecutive", 3),
        "stable_ticks": stable_ticks,
    }
    collector.fetch_and_process(phase_timeline, warmup_cfg=warmup_cfg)

    return collector.exportStats(), vm_count, collector._num_cpus, expected_on, events, getattr(collector, '_bpf_summary', {})


def run():
    print("overheadTest v%s" % VERSION)
    with open(config_file) as f:
        config = json.load(f)

    num_runs = config.get("num_runs", 1)

    cvm = Cvm(cvm_ip)
    host = cvm.getHost(host_name)

    all_runs = []
    vm_count = 0
    num_cpus = 0
    expected_on = 0

    for run_num in range(1, num_runs + 1):
        results, count, cpus, exp_on, events, bpf_summary = run_experiment(cvm, host, config, run_num)
        all_runs.append((results, events, bpf_summary))
        vm_count = count
        num_cpus = cpus
        expected_on = exp_on

    for i, (run_results, run_events, bpf_summary) in enumerate(all_runs):
        title = {
            "vm_count": vm_count,
            "expected_on": expected_on,
            "host": host_name,
            "run": i + 1,
            "num_runs": num_runs,
            "num_cpus": num_cpus,
            "config": config,
            "events": run_events,
            "bpf_summary": bpf_summary,
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

    if config_file == '' or host_name == '':
        print("Config file and host name are required.")
        print(helpmsg)
        sys.exit(2)

    if cvm_ip == '':
        print("No CVM IP provided, assuming running on CVM.")

    run()


if __name__ == "__main__":
    main(sys.argv[1:])
