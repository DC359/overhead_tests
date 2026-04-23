#!/usr/bin/env python3

VERSION = "0.4"

from util.ObjFactory import *
from util.cvm import *
import time, sys, getopt, json, threading, datetime


cvm_ip = ''
config_file = ''
host_name = ''


def wall_clock_now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def run_experiment(cvm, host, config, run_number):
    interval = config.get("interval", 30)
    baseline_duration = config.get("baseline_duration", 60)
    stabilization_wait = config.get("stabilization_wait", 120)
    stable_duration = config.get("stable_duration", 120)
    clone_prefix = config.get("clone_prefix", "dirty_harry")
    pattern = "%s_*" % clone_prefix

    total_ticks = (baseline_duration + stabilization_wait + stable_duration) // interval

    print("\n========== RUN %d ==========\n" % run_number)

    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(pattern)
    time.sleep(10)

    collector = ObjFactory.getStatsCollectorObj("schedstat")
    collector.setup(host, interval)

    phase_lock = threading.Lock()
    current_phase = {"phase": "baseline", "vm_count": 0}
    stop_event = threading.Event()

    def collector_loop():
        """Background thread: collects a tick every <interval> seconds on wall clock."""
        tick_num = 0
        while not stop_event.is_set():
            stop_event.wait(interval)
            if stop_event.is_set():
                break
            tick_num += 1
            with phase_lock:
                phase = current_phase["phase"]
                vms = current_phase["vm_count"]
            collector.collect_tick(phase=phase, wall_clock=wall_clock_now(), vm_count=vms)

    collector_thread = threading.Thread(target=collector_loop, daemon=True)

    print("[%s] Starting continuous collection (interval=%ds, total_ticks=%d)" % (wall_clock_now(), interval, total_ticks))
    collector_thread.start()

    # BASELINE
    print("[%s] --- BASELINE (all VMs off) ---" % wall_clock_now())
    time.sleep(baseline_duration)

    # MASS POWER ON
    print("[%s] --- MASS POWER ON ---" % wall_clock_now())
    with phase_lock:
        current_phase["phase"] = "power_on"
    on_out = cvm.vmOnAll(pattern)
    if on_out:
        out_lower = on_out.lower()
        if any(kw in out_lower for kw in ["not enough", "insufficient", "cannot", "kOutOfMemory"]):
            print("[WARNING] Host resources exhausted — some VMs failed to power on")
        else:
            print("All VMs powered on successfully.")

    # WARMUP
    print("[%s] --- WARMUP ---" % wall_clock_now())
    with phase_lock:
        current_phase["phase"] = "warmup"
        current_phase["vm_count"] = cvm.countPoweredOnVms()
    print("[%s] VMs on: %d" % (wall_clock_now(), current_phase["vm_count"]))
    time.sleep(stabilization_wait)

    vm_count = cvm.countPoweredOnVms()
    with phase_lock:
        current_phase["vm_count"] = vm_count
    print("[%s] VMs powered on (final): %d" % (wall_clock_now(), vm_count))

    # STABLE
    print("[%s] --- STABLE MEASUREMENT ---" % wall_clock_now())
    with phase_lock:
        current_phase["phase"] = "stable"
    time.sleep(stable_duration)

    stop_event.set()
    collector_thread.join(timeout=10)

    print("[%s] Powering off all test VMs..." % wall_clock_now())
    cvm.vmOffAll(pattern)

    return collector.exportStats(), vm_count, collector._num_cpus


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

    for run_num in range(1, num_runs + 1):
        results, count, cpus = run_experiment(cvm, host, config, run_num)
        all_runs.append(results)
        vm_count = count
        num_cpus = cpus

    for i, run_results in enumerate(all_runs):
        title = {
            "vm_count": vm_count,
            "host": host_name,
            "run": i + 1,
            "num_runs": num_runs,
            "num_cpus": num_cpus,
            "config": config,
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
