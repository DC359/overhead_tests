#!/usr/bin/env python3

from util.ObjFactory import *
from util.cvm import *
import time, sys, getopt, json

cvm_ip = ''
config_file = ''
host_name = ''


def run_experiment(cvm, host, config, run_number):
    interval = config.get("interval", 30)
    baseline_ticks = config.get("baseline_duration", 60) // interval
    stabilization_ticks = config.get("stabilization_wait", 120) // interval
    stable_ticks = config.get("stable_duration", 120) // interval
    clone_prefix = config.get("clone_prefix", "dirty_harry")
    pattern = "%s_*" % clone_prefix

    print("\n========== RUN %d ==========\n" % run_number)

    # Ensure all VMs are off before starting
    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(pattern)
    time.sleep(10)

    # Setup collector
    collector = ObjFactory.getStatsCollectorObj("schedstat")
    collector.setup(host, interval)

    # BASELINE
    print("--- BASELINE (all VMs off) ---")
    for _ in range(baseline_ticks):
        time.sleep(interval)
        collector.collect_tick(phase="baseline")

    # MASS POWER ON
    print("--- MASS POWER ON ---")
    cvm.vmOnAll(pattern)

    # WARMUP
    print("--- WARMUP (VMs booting, dirty harry starting) ---")
    for _ in range(stabilization_ticks):
        time.sleep(interval)
        collector.collect_tick(phase="warmup")

    vm_count = cvm.countPoweredOnVms()
    print("VMs powered on: %d" % vm_count)

    # --- VALIDATION START: check workload on a sample clone ---
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
    # --- VALIDATION END ---

    # STABLE
    print("--- STABLE MEASUREMENT ---")
    for _ in range(stable_ticks):
        time.sleep(interval)
        collector.collect_tick(phase="stable")

    # Power off after run
    print("Powering off all test VMs...")
    cvm.vmOffAll(pattern)

    return collector.exportStats(), vm_count, collector._num_cpus


def run():
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

    # Dump results for each run
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
