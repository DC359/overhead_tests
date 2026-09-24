#!/usr/bin/env python3

from util.ObjFactory import *
from util.cvm import *
import time, sys, getopt, json, datetime, math, random

cvm_ip = ''
config_file = ''
host_name = ''


def wall_clock_now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def validate_vms_and_workload(cvm, clone_prefix, svc_name, max_wait_s=60,
                              sample_pct=5, poll_every_s=5):
    """
    After power-on, sample ~5% of ACTIVE (powered-on) clone VMs at random and
    wait (up to max_wait_s) for their workload service to become active.

    Buffer-safe: samples only from powered-on VMs, so an off buffer VM is never
    picked. A sampled VM that has no IP / is unreachable is treated as
    "couldn't check" (soft), not a hard failure (may be a lagging VM).

    Returns the final powered-on count. Never aborts — warns only.
    """
    from libx.lib import run_remote_cmd

    on_names = cvm.listPoweredOnMatching(clone_prefix)
    vm_count = len(on_names)
    if vm_count == 0:
        return 0

    # 5% of active VMs, at least 1.
    sample_n = max(1, int(math.ceil(vm_count * sample_pct / 100.0)))
    sample_n = min(sample_n, vm_count)
    sampled = random.sample(on_names, sample_n)

    print("[%s] Validating VMs on and workloads running: sampling %d%% of "
          "%d active VMs -> %d VM(s): %s (up to %ds)" % (
              wall_clock_now(), sample_pct, vm_count, sample_n,
              ", ".join(sampled), max_wait_s))
    sys.stdout.flush()

    if not svc_name:
        print("[VALIDATION] No workload service/type to check; skipping "
              "workload check (VMs are on).")
        return vm_count

    start = time.time()
    active = set()
    unreachable = {}   # name -> last reason (soft)
    inactive = {}      # name -> last status (hard)

    while True:
        for name in sampled:
            if name in active:
                continue
            try:
                vm = Vm(name, cvm)
                ip = vm.getIp(timeout_s=5)
            except Exception as e:
                unreachable[name] = "no IP (%s)" % str(e).splitlines()[0][:40]
                continue
            try:
                status = run_remote_cmd(
                    ip, "root", "systemctl is-active %s" % svc_name,
                    use_password=False, timeout=10)
                status = status.decode().strip() if isinstance(status, bytes) else status.strip()
                unreachable.pop(name, None)
                if status == "active":
                    active.add(name)
                    inactive.pop(name, None)
                else:
                    inactive[name] = status
            except Exception as e:
                # SSH error: soft — may be booting / key not seeded yet
                unreachable[name] = "ssh err (%s)" % str(e).splitlines()[0][:40]

        if len(active) == sample_n:
            print("[VALIDATION] all %d sampled workloads active (%.0fs). "
                  "Continuing." % (sample_n, time.time() - start))
            return vm_count

        if time.time() - start >= max_wait_s:
            break
        time.sleep(poll_every_s)

    # Timed out: report what we saw.
    parts = ["%d/%d active" % (len(active), sample_n)]
    if inactive:
        parts.append("inactive: " + ", ".join(
            "%s=%s" % (n, s) for n, s in inactive.items()))
    if unreachable:
        parts.append("unchecked: " + ", ".join(
            "%s (%s)" % (n, r) for n, r in unreachable.items()))
    print("[VALIDATION] after %ds — %s" % (max_wait_s, "; ".join(parts)))
    if inactive:
        print("WARNING: workload not active on %d of %d sampled VM(s) — "
              "results may be invalid." % (len(inactive), sample_n))
    elif unreachable:
        print("WARNING: could not confirm workload on %d of %d sampled VM(s) "
              "(no IP / SSH) — proceeding anyway." % (len(unreachable), sample_n))
    return vm_count


def resolve_collect_duration(config):
    """
    Seconds to collect after VMs are on (the main experiment wait).

    Prefer config 'collect_duration'. If absent, fall back to the legacy
    formula: max_warmup_duration + (stable_ticks * interval).
    """
    if config.get("collect_duration") is not None:
        return max(1, int(config["collect_duration"]))
    interval = max(1, int(config.get("interval", 5)))
    stable_ticks = int(config.get("stable_ticks", 24))
    max_warmup = int(config.get("max_warmup_duration", 120))
    return max(1, max_warmup + stable_ticks * interval)


def resolve_stable_ticks_for_labeling(config, collect_duration, interval):
    """
    How many ticks the post-processor may treat as 'stable' in the fallback
    path. Timing of the run is NOT driven by this — only phase labels.
    """
    if config.get("stable_ticks") is not None:
        return max(1, int(config["stable_ticks"]))
    return max(1, int(collect_duration) // max(1, int(interval)))


def run_experiment(cvm, host, config, run_number):
    interval = max(1, int(config.get("interval", 5)))
    baseline_duration = config.get("baseline_duration", 30)
    collect_duration = resolve_collect_duration(config)
    stable_ticks = resolve_stable_ticks_for_labeling(
        config, collect_duration, interval)
    clone_prefix = config.get("clone_prefix")
    if not clone_prefix:
        print("ERROR: config is missing required field 'clone_prefix'.")
        sys.exit(2)
    pattern = "%s_*" % clone_prefix

    existing = cvm.countVmsMatching(clone_prefix)
    if existing == 0:
        print("ERROR: no VMs matching clone_prefix '%s_*' found." % clone_prefix)
        print("  Create them first (omit --skip-setup), e.g.:")
        print("    python3 overhead.py --host %s --config %s"
              % (host_name, config_file or "<config.json>"))
        print("  or: python3 setupVms.py -H %s -f %s"
              % (host_name, config_file or "<config.json>"))
        sys.exit(1)
    print("Found %d VM(s) matching clone_prefix '%s_*'." % (existing, clone_prefix))

    # The collector runs autonomously on the host. We sleep through the phases,
    # record a phase_timeline of (wall_clock, phase, vm_count), then fetch +
    # process once at the end (warmup->stable is detected retroactively).
    print("\n========== RUN %d ==========\n" % run_number)
    print("Config: interval=%ds, baseline=%ds, collect_after_vms_on=%ds "
          "(stable_ticks=%d for phase labels only)" % (
              interval, baseline_duration, collect_duration, stable_ticks))

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

    existing = cvm.countVmsMatching(clone_prefix)

    # Service name comes from config when set; else a small type→unit map.
    wl = config.get("workload", {}) or {}
    svc_names = {
        "dirtyHarry": "dirty-harry",
        "fio": "fio-workload",
        "redis": "redis-workload",
    }
    svc_name = wl.get("service") or svc_names.get(wl.get("type", ""), None)

    # Validate (and let VMs settle) in one step: sample 5% of active VMs and
    # wait up to 60s for their workload to come up. Replaces the old blind
    # 10s sleep + single-VM check.
    vm_count = validate_vms_and_workload(
        cvm, clone_prefix, svc_name, max_wait_s=60, sample_pct=5)

    print("[%s] VMs powered on: %d / %d matching clone_prefix '%s_*'" % (
        wall_clock_now(), vm_count, existing, clone_prefix))
    if vm_count == 0:
        print("ERROR: mass power-on left 0 VMs on (pattern %s)." % pattern)
        print("  Check that clones exist and acli vm.on succeeded.")
        collector.stop()
        sys.exit(1)
    if existing and vm_count < existing:
        print("WARNING: only %d of %d '%s_*' VMs are on — others may still be "
              "booting or failed to start." % (vm_count, existing, clone_prefix))

    # COLLECTING — fixed wall time after VMs on; warmup vs stable labeled later
    wc = wall_clock_now()
    phase_timeline.append((wc, "collecting", vm_count))
    print("[%s] --- COLLECTING (VMs on: %d, %ds total) ---" % (
        wc, vm_count, collect_duration))
    time.sleep(collect_duration)

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
