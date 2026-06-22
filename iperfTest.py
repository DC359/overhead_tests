#!/usr/bin/env python3
"""
iperfTest.py — OVS/iperf overhead experiment.

Single-host mode (1 host, N VMs, half server / half client):
  python3 iperfTest.py -f sample/test_iperf_1host.json -H <hostname>

Two-host mode (host A = all servers, host B = all clients):
  python3 iperfTest.py -f sample/test_iperf_2host.json -S <server_host> -C <client_host>

Setup VMs first with the existing setupVms.py:
  python3 setupVms.py -f sample/test_iperf_1host.json -H <hostname>
"""

VERSION = "0.1"

from util.ObjFactory import *
from util.cvm import *
from libx.lib import run_remote_cmd, shell_run
import time
import sys
import getopt
import json
import re
import datetime

cvm_ip = ''
config_file = ''
host_name = ''
server_host_name = ''
client_host_name = ''


def wall_clock_now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def get_vm_ips(cvm, prefix, expected_count):
    """Power on VMs matching prefix, wait for all to get IPs, return {index: ip}.
    Tolerates losing up to 2 VMs (accepts expected-2 as success).
    Enforces a minimum of 8 polls (~2 min) before allowing stale-exit.
    Aborts if:
      - zero IPs after 20 polls (~5 min): networking/DHCP is broken
      - no new IPs for 10 consecutive polls AND min polls met: stalled"""
    pattern = "%s_*" % prefix
    print("Powering on %s ..." % pattern)
    cvm.vmOnAll(pattern)

    min_acceptable = max(expected_count - 2, 1)
    print("Waiting for VMs to get IPs (target=%d, acceptable=%d)..." % (expected_count, min_acceptable))

    ip_map = {}
    zero_polls = 0
    stale_polls = 0
    max_zero_polls = 20
    max_stale_polls = 10
    min_polls_before_stale_exit = 8
    last_progress_count = 0
    poll_num = 0

    while True:
        ip_map = {}
        all_vms = cvm.getVmDetails()
        for vm in all_vms:
            name = vm.get('Name', '')
            if not name.startswith(prefix + "_"):
                continue
            suffix = name[len(prefix) + 1:]
            if not suffix.isdigit():
                continue
            idx = int(suffix)
            ip_raw = vm.get('VM IP Addresses', '').split(',')[0].strip()
            if ip_raw:
                ip_map[idx] = ip_raw

        got = len(ip_map)

        if got >= expected_count:
            break

        if got >= min_acceptable and poll_num >= min_polls_before_stale_exit:
            print("[OK] Got %d/%d IPs (>= acceptable %d). Proceeding." % (got, expected_count, min_acceptable))
            break

        if got == 0:
            zero_polls += 1
            if zero_polls >= max_zero_polls:
                print("[FATAL] No IPs found after %d polls (~%ds). DHCP or networking is broken." % (
                    max_zero_polls, max_zero_polls * 15))
                break
        else:
            zero_polls = 0

        if got > last_progress_count:
            last_progress_count = got
            stale_polls = 0
        else:
            stale_polls += 1
            if stale_polls >= max_stale_polls and poll_num >= min_polls_before_stale_exit:
                print("[WARNING] Stuck at %d/%d IPs for %d polls (~%ds). Continuing with available VMs." % (
                    got, expected_count, max_stale_polls, max_stale_polls * 15))
                break

        poll_num += 1
        print("  ... got %d/%d IPs so far (poll %d, stale=%d/%d)" % (
            got, expected_count, poll_num, stale_polls, max_stale_polls))
        time.sleep(15)

    if len(ip_map) < expected_count:
        print("[WARNING] Only got %d/%d IPs. Continuing with available VMs." % (len(ip_map), expected_count))

    return ip_map


def start_iperf_servers(ip_map, port=5201):
    """SSH into each server VM and start iperf3 in server mode."""
    print("Starting iperf3 servers on %d VMs..." % len(ip_map))
    failed = 0
    for idx in sorted(ip_map.keys()):
        ip = ip_map[idx]
        try:
            cmd = "pkill -9 iperf3 2>/dev/null; sleep 1; nohup iperf3 -s -p %d > /dev/null 2>&1 &" % port
            run_remote_cmd(ip, "root", cmd, use_password=True)
        except Exception as e:
            print("  [ERROR] Failed to start server on VM %d (%s): %s" % (idx, ip, e))
            failed += 1
    print("  Servers started: %d, failed: %d" % (len(ip_map) - failed, failed))
    return failed


def start_iperf_clients(client_ip_map, server_ip_map, threads=2, port=5201):
    """SSH into each client VM and start iperf3 connecting to its paired server."""
    print("Starting iperf3 clients on %d VMs (threads=%d)..." % (len(client_ip_map), threads))
    failed = 0
    for client_idx, server_idx in zip(sorted(client_ip_map.keys()), sorted(server_ip_map.keys())):
        client_ip = client_ip_map[client_idx]
        server_ip = server_ip_map[server_idx]
        try:
            cmd = "pkill -9 iperf3 2>/dev/null; sleep 1; nohup iperf3 -c %s -p %d -P %d -t 86400 > /dev/null 2>&1 &" % (
                server_ip, port, threads)
            run_remote_cmd(client_ip, "root", cmd, use_password=True)
        except Exception as e:
            print("  [ERROR] Failed to start client on VM %d (%s) -> server %s: %s" % (
                client_idx, client_ip, server_ip, e))
            failed += 1
    print("  Clients started: %d, failed: %d" % (len(client_ip_map) - failed, failed))
    return failed


def check_vm_ssh_reachable(ip, label="VM"):
    """Check a single VM is SSH-reachable."""
    try:
        out = run_remote_cmd(ip, "root", "echo ALIVE", use_password=True)
        out = out.decode().strip() if isinstance(out, bytes) else out.strip()
        if out == "ALIVE":
            return True
    except Exception:
        pass
    print("  [CHECK] %s (%s): SSH not reachable" % (label, ip))
    return False


def check_iperf_binary(ip, label="VM"):
    """Check iperf3 binary exists on the VM."""
    try:
        out = run_remote_cmd(ip, "root", "iperf3 --version 2>&1 | head -1", use_password=True)
        out = out.decode().strip() if isinstance(out, bytes) else out.strip()
        if "iperf" in out.lower():
            return True
    except Exception:
        pass
    print("  [CHECK] %s (%s): iperf3 binary NOT FOUND" % (label, ip))
    return False


def check_iperf_running(ip, role, label="VM"):
    """Check iperf3 process is running with the expected role (-s or -c)."""
    flag = "-s" if role == "server" else "-c"
    try:
        out = run_remote_cmd(ip, "root", "pgrep -a iperf3 || echo NO_IPERF", use_password=True)
        out = out.decode().strip() if isinstance(out, bytes) else out.strip()
        if "iperf3" in out and flag in out:
            return True
        if "NO_IPERF" in out:
            print("  [CHECK] %s (%s): iperf3 %s NOT running" % (label, ip, role))
        else:
            print("  [CHECK] %s (%s): iperf3 running but not as %s. Output: %s" % (label, ip, role, out))
    except Exception as e:
        print("  [CHECK] %s (%s): check failed: %s" % (label, ip, e))
    return False


def validate_iperf_pair(server_ip, client_ip, port=5201):
    """Check that iperf3 is running on both a server and client VM."""
    print("[VALIDATION] Checking iperf3 pair: server=%s, client=%s" % (server_ip, client_ip))
    ok = True

    if not check_iperf_running(server_ip, "server", "server"):
        ok = False
    else:
        print("  Server (%s): iperf3 server is RUNNING" % server_ip)

    if not check_iperf_running(client_ip, "client", "client"):
        ok = False
    else:
        print("  Client (%s): iperf3 client is RUNNING" % client_ip)

    return ok


def validate_bulk_iperf(ip_map, role, sample_count=5):
    """Spot-check that iperf3 is running on a sample of VMs."""
    indices = sorted(ip_map.keys())
    check_indices = indices[:sample_count] if len(indices) > sample_count else indices
    running = 0
    failed = 0
    for idx in check_indices:
        if check_iperf_running(ip_map[idx], role, "%s_vm_%d" % (role, idx)):
            running += 1
        else:
            failed += 1
    total = len(ip_map)
    print("[VALIDATION] %s spot-check: %d/%d sampled are running (total VMs: %d)" % (
        role.upper(), running, len(check_indices), total))
    if failed > 0:
        print("[WARNING] %d/%d sampled %ss are NOT running!" % (failed, len(check_indices), role))
    return failed == 0


def validate_post_ip_discovery(ip_map, prefix, expected):
    """After IP discovery, verify we got enough IPs and a sample VM is reachable."""
    got = len(ip_map)
    print("[CHECK] IP discovery for '%s': expected=%d, got=%d" % (prefix, expected, got))
    if got < expected:
        print("[WARNING] Missing %d VMs — they may not have booted or got DHCP." % (expected - got))
    if got == 0:
        print("[ERROR] No IPs found at all! Cannot proceed.")
        return False

    sample_idx = sorted(ip_map.keys())[0]
    sample_ip = ip_map[sample_idx]
    print("[CHECK] Verifying SSH to sample VM %s_%d (%s)..." % (prefix, sample_idx, sample_ip))
    if not check_vm_ssh_reachable(sample_ip, "%s_%d" % (prefix, sample_idx)):
        print("[ERROR] Cannot SSH to sample VM. Network or boot issue.")
        return False
    print("[CHECK] SSH to %s_%d: OK" % (prefix, sample_idx))

    print("[CHECK] Verifying iperf3 binary on %s_%d..." % (prefix, sample_idx))
    if not check_iperf_binary(sample_ip, "%s_%d" % (prefix, sample_idx)):
        print("[ERROR] iperf3 not found on sample VM. Did setupVms.py complete successfully?")
        return False
    print("[CHECK] iperf3 binary on %s_%d: OK" % (prefix, sample_idx))

    return True


def count_prefix_vms(cvm, prefix):
    """Count how many VMs exist with the given prefix."""
    count = 0
    all_vms = cvm.getVmDetails()
    for vm in all_vms:
        name = vm.get('Name', '')
        if name.startswith(prefix + "_"):
            suffix = name[len(prefix) + 1:]
            if suffix.isdigit():
                count += 1
    return count



def run_experiment_1host(cvm, host, config, run_number):
    """Single-host experiment: N VMs on one host, split into servers and clients."""
    interval = config.get("interval", 5)
    baseline_duration = config.get("baseline_duration", 30)
    stable_ticks = config.get("stable_ticks", 24)
    max_warmup_duration = config.get("max_warmup_duration", 300)
    clone_prefix = config.get("clone_prefix", "iperf_vm")
    pattern = "%s_*" % clone_prefix
    threads = config.get("workload", {}).get("config", {}).get("threads", 2)
    port = config.get("workload", {}).get("config", {}).get("port", 5201)

    post_traffic_duration = max_warmup_duration + (stable_ticks * interval)

    print("\n========== RUN %d (single-host) ==========\n" % run_number)
    print("Config: interval=%ds, baseline=%ds, max_warmup=%ds, stable_ticks=%d" % (
        interval, baseline_duration, max_warmup_duration, stable_ticks))

    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(pattern)
    time.sleep(10)

    total_vms = count_prefix_vms(cvm, clone_prefix)
    if total_vms == 0:
        print("[ERROR] No VMs found with prefix '%s'. Run setupVms.py first." % clone_prefix)
        sys.exit(1)
    if total_vms % 2 != 0:
        total_vms -= 1
        print("[INFO] Odd number of VMs, using %d (skipping last one)." % total_vms)
    num_pairs = total_vms // 2
    print("Total VMs: %d, Pairs: %d (servers: 1-%d, clients: %d-%d)" % (
        total_vms, num_pairs, num_pairs, num_pairs + 1, total_vms))

    base_on_count = cvm.countPoweredOnVms()
    expected_on = base_on_count + total_vms

    collector_id = config.get("collector", "bpfsnap")
    print("[COLLECTOR] using '%s' (%s)" % (
        collector_id,
        "full eBPF snapshot+exit" if collector_id == "bpfsnap" else "schedstat /proc-poll + bpftrace"))
    collector = ObjFactory.getStatsCollectorObj(collector_id)
    collector.setup(host, interval)

    phase_timeline = []
    events = []

    # BASELINE
    wc = wall_clock_now()
    events.append((wc, "collection_started"))
    phase_timeline.append((wc, "baseline", 0))
    print("[%s] --- BASELINE (all VMs off, %ds) ---" % (wc, baseline_duration))
    time.sleep(baseline_duration)

    # POWER ON ALL VMs and get IPs
    wc = wall_clock_now()
    events.append((wc, "vms_on_cmd_sent"))
    phase_timeline.append((wc, "power_on", 0))
    print("[%s] --- POWER ON & IP DISCOVERY ---" % wc)
    ip_map = get_vm_ips(cvm, clone_prefix, total_vms)
    wc = wall_clock_now()
    events.append((wc, "all_vms_on"))
    print("Got %d IPs." % len(ip_map))

    if not validate_post_ip_discovery(ip_map, clone_prefix, total_vms):
        print("[FATAL] IP discovery checks failed. Powering off and aborting.")
        collector.stop()
        cvm.vmOffAll(pattern)
        sys.exit(1)

    actual_vms = len(ip_map)
    if actual_vms % 2 != 0:
        actual_vms -= 1
    num_pairs = actual_vms // 2

    sorted_indices = sorted(ip_map.keys())[:actual_vms]
    server_indices = sorted_indices[:num_pairs]
    client_indices = sorted_indices[num_pairs:]

    server_ip_map = {i: ip_map[i] for i in server_indices}
    client_ip_map = {i: ip_map[i] for i in client_indices}

    print("Actual VMs with IPs: %d, Pairs: %d" % (actual_vms, num_pairs))
    print("Server VMs (indices %s): %d VMs" % (
        "%d-%d" % (server_indices[0], server_indices[-1]), len(server_ip_map)))
    print("Client VMs (indices %s): %d VMs" % (
        "%d-%d" % (client_indices[0], client_indices[-1]), len(client_ip_map)))

    # START IPERF SERVERS
    print("--- STARTING IPERF SERVERS ---")
    start_iperf_servers(server_ip_map, port)
    time.sleep(10)

    print("--- CHECKING IPERF SERVERS ---")
    validate_bulk_iperf(server_ip_map, "server")

    # START IPERF CLIENTS
    wc = wall_clock_now()
    events.append((wc, "iperf_traffic_started"))
    print("--- STARTING IPERF CLIENTS ---")
    start_iperf_clients(client_ip_map, server_ip_map, threads, port)
    time.sleep(10)

    print("--- CHECKING IPERF CLIENTS ---")
    validate_bulk_iperf(client_ip_map, "client")

    first_server_ip = server_ip_map[server_indices[0]]
    first_client_ip = client_ip_map[client_indices[0]]
    if not validate_iperf_pair(first_server_ip, first_client_ip, port):
        print("[WARNING] First iperf pair validation failed. Traffic may not be flowing.")

    # COLLECTING (warmup + stable determined retroactively)
    vm_count = cvm.countPoweredOnVms()
    wc = wall_clock_now()
    phase_timeline.append((wc, "collecting", vm_count))
    print("[%s] --- COLLECTING (VMs on: %d, waiting %ds for warmup+stable) ---" % (
        wc, vm_count, post_traffic_duration))
    time.sleep(post_traffic_duration)

    vm_count = cvm.countPoweredOnVms()
    print("[%s] VMs powered on (final): %d" % (wall_clock_now(), vm_count))

    wc = wall_clock_now()
    events.append((wc, "collection_ended"))
    collector.stop()

    print("[%s] Powering off all test VMs..." % wall_clock_now())
    wc = wall_clock_now()
    events.append((wc, "vms_off_cmd_sent"))
    cvm.vmOffAll(pattern)

    host_ip = host.getHostIp()
    scp_opts = "-o StrictHostKeyChecking=no"
    if host._control_active:
        scp_opts += " -o ControlPath=%s" % host._control_socket
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

    return collector.exportStats(), vm_count, collector._num_cpus, expected_on, events


def run_experiment_2host(cvm, server_host, client_host, config, run_number):
    """Two-host experiment: all servers on host A, all clients on host B."""
    interval = config.get("interval", 5)
    baseline_duration = config.get("baseline_duration", 30)
    stable_ticks = config.get("stable_ticks", 24)
    max_warmup_duration = config.get("max_warmup_duration", 300)
    server_prefix = config.get("server_clone_prefix", "iperf_server")
    client_prefix = config.get("client_clone_prefix", "iperf_client")
    server_pattern = "%s_*" % server_prefix
    client_pattern = "%s_*" % client_prefix
    threads = config.get("workload", {}).get("config", {}).get("threads", 2)
    port = config.get("workload", {}).get("config", {}).get("port", 5201)

    post_traffic_duration = max_warmup_duration + (stable_ticks * interval)

    print("\n========== RUN %d (two-host) ==========\n" % run_number)
    print("Config: interval=%ds, baseline=%ds, max_warmup=%ds, stable_ticks=%d" % (
        interval, baseline_duration, max_warmup_duration, stable_ticks))

    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(server_pattern)
    cvm.vmOffAll(client_pattern)
    time.sleep(10)

    num_servers = count_prefix_vms(cvm, server_prefix)
    num_clients = count_prefix_vms(cvm, client_prefix)
    if num_servers == 0 or num_clients == 0:
        print("[ERROR] No server or client VMs found. Run setupVms.py for both hosts first.")
        sys.exit(1)

    num_pairs = min(num_servers, num_clients)
    print("Server VMs: %d, Client VMs: %d, Pairs: %d" % (num_servers, num_clients, num_pairs))

    collector_id = config.get("collector", "bpfsnap")
    print("[COLLECTOR] using '%s' (%s)" % (
        collector_id,
        "full eBPF snapshot+exit" if collector_id == "bpfsnap" else "schedstat /proc-poll + bpftrace"))
    server_collector = ObjFactory.getStatsCollectorObj(collector_id)
    server_collector.setup(server_host, interval, label="%s server" % server_host_name)
    client_collector = ObjFactory.getStatsCollectorObj(collector_id)
    client_collector.setup(client_host, interval, label="%s client" % client_host_name)

    phase_timeline = []

    # BASELINE
    wc = wall_clock_now()
    phase_timeline.append((wc, "baseline", 0))
    print("[%s] --- BASELINE (all VMs off, %ds) ---" % (wc, baseline_duration))
    time.sleep(baseline_duration)

    # POWER ON SERVERS
    wc = wall_clock_now()
    phase_timeline.append((wc, "power_on", 0))
    print("[%s] --- POWER ON SERVERS ---" % wc)
    server_ip_map = get_vm_ips(cvm, server_prefix, num_pairs)
    print("Got %d server IPs." % len(server_ip_map))

    if not validate_post_ip_discovery(server_ip_map, server_prefix, num_pairs):
        print("[FATAL] Server IP discovery checks failed. Aborting.")
        server_collector.stop()
        client_collector.stop()
        cvm.vmOffAll(server_pattern)
        sys.exit(1)

    # START IPERF SERVERS
    print("--- STARTING IPERF SERVERS ---")
    start_iperf_servers(server_ip_map, port)
    time.sleep(10)

    print("--- CHECKING IPERF SERVERS ---")
    validate_bulk_iperf(server_ip_map, "server")

    # POWER ON CLIENTS
    print("--- POWER ON CLIENTS ---")
    client_ip_map = get_vm_ips(cvm, client_prefix, num_pairs)
    print("Got %d client IPs." % len(client_ip_map))

    if not validate_post_ip_discovery(client_ip_map, client_prefix, num_pairs):
        print("[FATAL] Client IP discovery checks failed. Aborting.")
        server_collector.stop()
        client_collector.stop()
        cvm.vmOffAll(server_pattern)
        cvm.vmOffAll(client_pattern)
        sys.exit(1)

    # Recompute pairs based on actual IPs obtained
    num_pairs = min(len(server_ip_map), len(client_ip_map))
    print("Actual pairs (based on IPs): %d" % num_pairs)

    # START IPERF CLIENTS
    print("--- STARTING IPERF CLIENTS ---")
    start_iperf_clients(client_ip_map, server_ip_map, threads, port)
    time.sleep(10)

    print("--- CHECKING IPERF CLIENTS ---")
    validate_bulk_iperf(client_ip_map, "client")

    server_indices = sorted(server_ip_map.keys())
    client_indices = sorted(client_ip_map.keys())
    if server_indices and client_indices:
        if not validate_iperf_pair(server_ip_map[server_indices[0]], client_ip_map[client_indices[0]], port):
            print("[WARNING] First iperf pair validation failed. Traffic may not be flowing.")

    # COLLECTING (warmup + stable determined retroactively)
    vm_count = cvm.countPoweredOnVms()
    wc = wall_clock_now()
    phase_timeline.append((wc, "collecting", vm_count))
    print("[%s] --- COLLECTING (VMs on: %d, waiting %ds for warmup+stable) ---" % (
        wc, vm_count, post_traffic_duration))
    time.sleep(post_traffic_duration)

    vm_count = cvm.countPoweredOnVms()
    print("[%s] VMs powered on (final): %d" % (wall_clock_now(), vm_count))

    server_collector.stop()
    client_collector.stop()

    print("[%s] Powering off all test VMs..." % wall_clock_now())
    cvm.vmOffAll(server_pattern)
    cvm.vmOffAll(client_pattern)

    warmup_cfg = {
        "threshold_pct": config.get("warmup_threshold_pct", 5),
        "consecutive": config.get("warmup_consecutive", 3),
        "stable_ticks": stable_ticks,
    }
    print("\n[%s] Fetching results from server host and detecting phases..." % wall_clock_now())
    server_collector.fetch_and_process(phase_timeline, warmup_cfg=warmup_cfg)
    print("\n[%s] Fetching results from client host and detecting phases..." % wall_clock_now())
    client_collector.fetch_and_process(phase_timeline, warmup_cfg=warmup_cfg)

    return (
        server_collector.exportStats(),
        client_collector.exportStats(),
        vm_count,
        server_collector._num_cpus,
    )


def dump_results(title, stats, label=""):
    prefix = " (%s)" % label if label else ""
    print("\n========== RESULTS%s ==========\n" % prefix)
    terminal_dump = ObjFactory.getStatsDumpObj("terminalDump")
    terminal_dump.dumpStats(title, stats)
    csv_dump = ObjFactory.getStatsDumpObj("csvDump")
    csv_dump.dumpStats(title, stats)


def run():
    print("iperfTest v%s" % VERSION)
    with open(config_file) as f:
        config = json.load(f)

    num_runs = config.get("num_runs", 1)
    cvm = Cvm(cvm_ip)

    is_two_host = server_host_name != '' and client_host_name != ''

    if is_two_host:
        server_host = cvm.getHost(server_host_name)
        client_host = cvm.getHost(client_host_name)
        print("Mode: TWO-HOST (server=%s, client=%s)" % (server_host_name, client_host_name))

        for run_num in range(1, num_runs + 1):
            server_stats, client_stats, vm_count, num_cpus = run_experiment_2host(
                cvm, server_host, client_host, config, run_num)

            base_title = {
                "vm_count": vm_count,
                "run": run_num,
                "num_runs": num_runs,
                "num_cpus": num_cpus,
                "config": config,
            }

            server_title = dict(base_title, host=server_host_name)
            dump_results(server_title, server_stats, "server-host %s run %d" % (server_host_name, run_num))

            client_title = dict(base_title, host=client_host_name)
            dump_results(client_title, client_stats, "client-host %s run %d" % (client_host_name, run_num))

    else:
        host = cvm.getHost(host_name)
        print("Mode: SINGLE-HOST (%s)" % host_name)

        for run_num in range(1, num_runs + 1):
            stats, vm_count, num_cpus, exp_on, run_events = run_experiment_1host(cvm, host, config, run_num)

            title = {
                "vm_count": vm_count,
                "expected_on": exp_on,
                "host": host_name,
                "run": run_num,
                "num_runs": num_runs,
                "num_cpus": num_cpus,
                "config": config,
                "events": run_events,
            }
            dump_results(title, stats, "run %d" % run_num)

    print("\nAll %d run(s) complete." % num_runs)


def main(argv):
    global cvm_ip, config_file, host_name, server_host_name, client_host_name

    helpmsg = (
        "iperfTest.py — OVS/iperf overhead experiment\n"
        "\n"
        "Single-host:\n"
        "  python3 iperfTest.py -f <config> -H <hostname> [-i <cvmip>]\n"
        "\n"
        "Two-host:\n"
        "  python3 iperfTest.py -f <config> -S <server_host> -C <client_host> [-i <cvmip>]\n"
    )
    try:
        opts, args = getopt.getopt(argv, "hi:f:H:S:C:", [
            "cvm=", "config=", "host=", "server-host=", "client-host="])
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
        elif opt in ("-S", "--server-host"):
            server_host_name = arg
        elif opt in ("-C", "--client-host"):
            client_host_name = arg

    if config_file == '':
        print("Config file is required.")
        print(helpmsg)
        sys.exit(2)

    if host_name == '' and (server_host_name == '' or client_host_name == ''):
        print("Either -H <host> (single-host) or -S <server> -C <client> (two-host) is required.")
        print(helpmsg)
        sys.exit(2)

    if cvm_ip == '':
        print("No CVM IP provided, assuming running on CVM.")

    run()


if __name__ == "__main__":
    main(sys.argv[1:])
