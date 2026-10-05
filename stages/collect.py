#!/usr/bin/env python3
# Power on, sample with cgroup_cpu_snap, power off
"""Collect stage — VERSION 0.1.0

Drive cgroup_cpu_snap with its native CLI. Optional --host-verify side tools.
"""

from __future__ import print_function

import os
import re
import time
import datetime
import subprocess
import json
import math
import random
import sys

VERSION = "0.1.0"

LOCAL_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "cgroup_cpu_snap")
REMOTE_BIN = "/root/cgroup_cpu_snap"
REMOTE_OUTDIR = "/var/log/cpu-stats"
REMOTE_STDOUT = "/tmp/cgroup_cpu_snap.stdout"
REMOTE_STDERR = "/tmp/cgroup_cpu_snap.stderr"
REMOTE_COLLECTOR_PID = "/tmp/cgroup_cpu_snap.collector_pid"
LOCAL_FETCH_DIR = os.path.expanduser("~")


def _s(out):
    if out is None:
        return ""
    return out.strip() if isinstance(out, str) else out.decode().strip()


def _q(cmd):
    """Wrap so HOST shell sees metacharacters."""
    return "'" + cmd + "'"


def wall_clock_now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def resolve_collect_duration(config):
    if config.get("collect_duration") is None:
        raise ValueError("config is missing required field 'collect_duration'")
    return max(1, int(config["collect_duration"]))


class SnapCollector(object):
    """Deploy / start / stop cgroup_cpu_snap on the AHV host."""

    def __init__(self, host, interval=5, host_verify=False):
        self._host = host
        self._interval = interval
        self._host_verify = host_verify
        self._num_cpus = 1
        self._collector_pid = None
        self._remote_log = None

    def deploy(self):
        from infra.ssh import shell_run
        from infra.retry import retry_call

        if not os.path.isfile(LOCAL_BIN):
            raise RuntimeError("Local collector missing: %s" % LOCAL_BIN)

        self._host.open_control()
        out = self._host.host_cmd("nproc")
        self._num_cpus = int(_s(out)) if _s(out) else 1

        host_ip = self._host.getHostIp()
        scp_opts = "-o StrictHostKeyChecking=no"
        if self._host._control_active:
            scp_opts += " -o ControlPath=%s" % self._host._control_socket

        print("[collect] deploying %s -> root@%s:%s" % (
            LOCAL_BIN, host_ip, REMOTE_BIN))
        retry_call(
            lambda: shell_run(
                "scp %s %s root@%s:%s" % (scp_opts, LOCAL_BIN, host_ip, REMOTE_BIN),
                timeout=60),
            max_attempts=2, delay=2,
            exceptions=(subprocess.CalledProcessError, TimeoutError, OSError))
        self._host.host_cmd(_q("chmod +x %s" % REMOTE_BIN))
        ok = _s(self._host.host_cmd(_q(
            "test -x %s && echo OK || echo MISSING" % REMOTE_BIN)))
        if ok != "OK":
            raise RuntimeError("Failed to deploy collector to %s" % REMOTE_BIN)
        print("[collect] collector deployed (cpus=%d)" % self._num_cpus)

    def start(self):
        # Clean stale collector / host-verify processes
        self._host.host_cmd(_q(
            'pkill -f "[c]group_cpu_snap" 2>/dev/null || true; '
            'pkill -f "[s]ystemd-cgtop" 2>/dev/null || true; '
            'pkill -f "[m]pstat" 2>/dev/null || true; '
            'pkill -f "[s]ar -q" 2>/dev/null || true; '
            "rm -f %s %s %s /tmp/cgtop_log.txt /tmp/mpstat_log.txt /tmp/sar_log.txt "
            "/tmp/cgtop_bg.pid /tmp/mpstat_bg.pid /tmp/sar_bg.pid"
            % (REMOTE_STDOUT, REMOTE_STDERR, REMOTE_COLLECTOR_PID)), quiet=True)
        time.sleep(0.3)

        self._host.host_cmd(_q("mkdir -p %s" % REMOTE_OUTDIR))

        print("[collect] starting cgroup_cpu_snap interval=%ds host_verify=%s" % (
            self._interval, self._host_verify))

        if self._host_verify:
            launch = (
                "nohup %s --interval %d --format raw --scope all --outdir %s "
                "</dev/null >%s 2>%s & echo $! > %s; "
                "nohup bash -c \"echo \\$\\$ > /tmp/cgtop_bg.pid; date > /tmp/cgtop_log.txt; "
                "exec systemd-cgtop -b -d %d -n 0 >> /tmp/cgtop_log.txt\" "
                "</dev/null >/dev/null 2>&1 & "
                "nohup bash -c \"echo \\$\\$ > /tmp/mpstat_bg.pid; "
                "exec mpstat %d >> /tmp/mpstat_log.txt\" "
                "</dev/null >/dev/null 2>&1 & "
                "nohup bash -c \"echo \\$\\$ > /tmp/sar_bg.pid; "
                "exec sar -q %d >> /tmp/sar_log.txt\" "
                "</dev/null >/dev/null 2>&1 &"
                % (REMOTE_BIN, self._interval, REMOTE_OUTDIR,
                   REMOTE_STDOUT, REMOTE_STDERR, REMOTE_COLLECTOR_PID,
                   self._interval, self._interval, self._interval)
            )
        else:
            launch = (
                "nohup %s --interval %d --format raw --scope all --outdir %s "
                "</dev/null >%s 2>%s & echo $! > %s"
                % (REMOTE_BIN, self._interval, REMOTE_OUTDIR,
                   REMOTE_STDOUT, REMOTE_STDERR, REMOTE_COLLECTOR_PID)
            )

        self._host.host_cmd(_q(launch))

        for attempt in range(8):
            time.sleep(1)
            pid = _s(self._host.host_cmd(
                "cat %s 2>/dev/null" % REMOTE_COLLECTOR_PID, quiet=True))
            if pid:
                self._collector_pid = pid
                break
        if not self._collector_pid:
            diag = _s(self._host.host_cmd(
                "cat %s 2>/dev/null" % REMOTE_STDERR, quiet=True))
            raise RuntimeError(
                "cgroup_cpu_snap failed to start (no collector_pid). stderr:\n%s"
                % (diag[-1500:] if diag else "(empty)"))

        time.sleep(2)
        alive = _s(self._host.host_cmd(_q(
            "kill -0 %s 2>/dev/null && echo ALIVE || echo DEAD"
            % self._collector_pid)))
        if alive != "ALIVE":
            diag = _s(self._host.host_cmd(
                "cat %s 2>/dev/null" % REMOTE_STDERR, quiet=True))
            raise RuntimeError(
                "cgroup_cpu_snap exited early (pid=%s). stderr:\n%s"
                % (self._collector_pid, diag[-1500:] if diag else "(empty)"))

        # Discover log path from stderr: out=/var/log/cpu-stats/...
        stderr = _s(self._host.host_cmd(
            "cat %s 2>/dev/null" % REMOTE_STDERR, quiet=True))
        m = re.search(r"out=(\S+)", stderr)
        if m:
            self._remote_log = m.group(1)
        else:
            # Fallback: newest matching file
            newest = _s(self._host.host_cmd(_q(
                "ls -1t %s/cgroup_cpu_snap_*.txt 2>/dev/null | head -1"
                % REMOTE_OUTDIR), quiet=True))
            if not newest:
                raise RuntimeError(
                    "Could not discover collector log path (no out= in stderr)")
            self._remote_log = newest

        print("[collect] RUNNING pid=%s log=%s" % (
            self._collector_pid, self._remote_log))
        return self._remote_log

    def stop(self):
        if self._collector_pid:
            # Graceful INT so C restores schedstats
            self._host.host_cmd(
                "kill -INT %s 2>/dev/null" % self._collector_pid, quiet=True)
            time.sleep(2)
            alive = _s(self._host.host_cmd(_q(
                "kill -0 %s 2>/dev/null && echo ALIVE || echo DEAD"
                % self._collector_pid), quiet=True))
            if alive == "ALIVE":
                print("[collect] still alive after INT — sending TERM")
                self._host.host_cmd(
                    "kill -TERM %s 2>/dev/null" % self._collector_pid, quiet=True)
                time.sleep(1)

        self._host.host_cmd(_q(
            'pkill -f "[c]group_cpu_snap" 2>/dev/null || true'), quiet=True)

        if self._host_verify:
            self._host.host_cmd(_q(
                "kill $(cat /tmp/cgtop_bg.pid) 2>/dev/null; "
                "kill $(cat /tmp/mpstat_bg.pid) 2>/dev/null; "
                "kill $(cat /tmp/sar_bg.pid) 2>/dev/null"), quiet=True)
            self._host.host_cmd(_q(
                'pkill -f "[s]ystemd-cgtop" 2>/dev/null || true; '
                'pkill -f "[m]pstat" 2>/dev/null || true; '
                'pkill -f "[s]ar -q" 2>/dev/null || true'), quiet=True)

        print("[collect] collector stopped (pid=%s)" % (
            self._collector_pid or "?"))

    def fetch_log(self, local_name="cgroup_cpu_snap_fetched.txt"):
        from infra.ssh import shell_run
        from infra.retry import retry_call

        if not self._remote_log:
            raise RuntimeError("No remote log path to fetch")
        host_ip = self._host.getHostIp()
        scp_opts = "-o StrictHostKeyChecking=no"
        if self._host._control_active:
            scp_opts += " -o ControlPath=%s" % self._host._control_socket
        local_path = os.path.join(LOCAL_FETCH_DIR, local_name)
        retry_call(
            lambda: shell_run(
                "scp %s root@%s:%s %s" % (
                    scp_opts, host_ip, self._remote_log, local_path),
                timeout=120),
            max_attempts=3, delay=2,
            exceptions=(subprocess.CalledProcessError, TimeoutError, OSError))
        print("[collect] fetched log -> %s" % local_path)

        if self._host_verify:
            for remote_log, local_n in (
                    ("/tmp/cgtop_log.txt", "cgtop_log_fetched.txt"),
                    ("/tmp/mpstat_log.txt", "mpstat_log_fetched.txt"),
                    ("/tmp/sar_log.txt", "sar_log_fetched.txt")):
                try:
                    retry_call(
                        lambda rl=remote_log, ln=local_n: shell_run(
                            "scp %s root@%s:%s %s/%s" % (
                                scp_opts, host_ip, rl, LOCAL_FETCH_DIR, ln),
                            timeout=60),
                        max_attempts=2, delay=2,
                        exceptions=(subprocess.CalledProcessError, TimeoutError, OSError))
                except Exception as e:
                    print("WARNING: could not fetch %s: %s" % (remote_log, e))

        return local_path


def validate_vms_and_workload(cvm, clone_prefix, svc_name, max_wait_s=60,
                              sample_pct=5, poll_every_s=5):
    """Sample ~5% of powered-on clones and wait for the workload service.

    Buffer-safe: samples only powered-on VMs. Unreachable guests are a
    warning, not a hard failure. Returns the powered-on count.
    """
    from infra.ssh import run_remote_cmd
    from infra.vm import Vm

    on_names = cvm.listPoweredOnMatching(clone_prefix)
    vm_count = len(on_names)
    if vm_count == 0:
        return 0

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
    unreachable = {}
    inactive = {}

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
                unreachable[name] = "ssh err (%s)" % str(e).splitlines()[0][:40]

        if len(active) == sample_n:
            print("[VALIDATION] all %d sampled workloads active (%.0fs). "
                  "Continuing." % (sample_n, time.time() - start))
            return vm_count

        if time.time() - start >= max_wait_s:
            break
        time.sleep(poll_every_s)

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


def run_one(cvm, host, config, run_number, host_verify=False):
    """One experiment run. Returns (tick_results, vm_count, num_cpus, events)."""
    from stages import present as stage_present

    interval = max(1, int(config.get("interval", 5)))
    baseline_duration = config.get("baseline_duration", 30)
    collect_duration = resolve_collect_duration(config)
    clone_prefix = config.get("clone_prefix")
    if not clone_prefix:
        raise ValueError("config is missing required field 'clone_prefix'")
    pattern = "%s_*" % clone_prefix

    existing = cvm.countVmsMatching(clone_prefix)
    if existing == 0:
        raise RuntimeError("no VMs matching clone_prefix '%s_*' found" % clone_prefix)
    print("Found %d VM(s) matching clone_prefix '%s_*'." % (existing, clone_prefix))

    print("\n========== RUN %d ==========\n" % run_number)
    print("Config: interval=%ds, baseline=%ds, collect_after_vms_on=%ds" % (
        interval, baseline_duration, collect_duration))

    print("Ensuring all test VMs are powered off...")
    cvm.vmOffAll(pattern)
    time.sleep(10)

    snap = SnapCollector(host, interval=interval, host_verify=host_verify)
    snap.deploy()
    events = []
    try:
        snap.start()

        state_timeline = []

        wc = wall_clock_now()
        events.append((wc, "collection_started"))
        state_timeline.append((wc, "vms_off", 0))
        print("[%s] --- BASELINE (all VMs off, %ds) ---" % (wc, baseline_duration))
        time.sleep(baseline_duration)

        t_vms_on = wall_clock_now()
        events.append((t_vms_on, "vms_on_cmd_sent"))
        print("[%s] --- MASS POWER ON ---" % t_vms_on)
        cvm.vmOnAll(pattern)

        existing = cvm.countVmsMatching(clone_prefix)
        wl = config.get("workload", {}) or {}
        svc_names = {
            "dirtyHarry": "dirty-harry",
            "fio": "fio-workload",
            "redis": "redis-workload",
        }
        svc_name = wl.get("service") or svc_names.get(wl.get("type", ""), None)

        vm_count = validate_vms_and_workload(
            cvm, clone_prefix, svc_name, max_wait_s=60, sample_pct=5)

        print("[%s] VMs powered on: %d / %d matching clone_prefix '%s_*'" % (
            wall_clock_now(), vm_count, existing, clone_prefix))
        if vm_count == 0:
            raise RuntimeError("mass power-on left 0 VMs on (pattern %s)" % pattern)
        if existing and vm_count < existing:
            print("INFO: %d of %d '%s_*' VMs are on "
                  "(extras from clone_buffer / still booting may stay off — OK, continuing)."
                  % (vm_count, existing, clone_prefix))

        state_timeline.append((t_vms_on, "vms_on", vm_count))

        wc = wall_clock_now()
        events.append((wc, "measure_started"))
        print("[%s] --- MEASURE (VMs on: %d, %ds) ---" % (
            wc, vm_count, collect_duration))
        time.sleep(collect_duration)

        vm_count = cvm.countPoweredOnMatching(clone_prefix)
        print("[%s] VMs powered on (final): %d" % (wall_clock_now(), vm_count))

        wc = wall_clock_now()
        events.append((wc, "collection_ended"))
        snap.stop()

        print("\n[%s] Fetching + presenting..." % wall_clock_now())
        local_log = snap.fetch_log(
            local_name="cgroup_cpu_snap_fetched_run%d.txt" % run_number)
        tick_results = stage_present.run(
            raw_path=local_log,
            state_timeline=state_timeline,
            num_cpus=snap._num_cpus,
            interval=interval,
            title=None,
        )
        return tick_results, vm_count, snap._num_cpus, events
    finally:
        try:
            snap.stop()
        except Exception as e:
            print("WARNING: collector stop: %s" % e)
        print("[%s] Powering off all test VMs..." % wall_clock_now())
        events.append((wall_clock_now(), "vms_off_cmd_sent"))
        try:
            cvm.vmOffAll(pattern)
        except Exception as e:
            print("WARNING: vmOffAll: %s" % e)


def run(cvm_ip, host_name, config_path, host_verify=False, cvm=None):
    """Full multi-run collect + present (orchestrator entry).

    Pass an existing `cvm` from validate to avoid a second slow ncli inventory load.
    Metadata is collected for CSV/JSON but not printed here — terminalDump prints
    it once in the results block.
    """
    print("[stages.collect] v%s — cgroup_cpu_snap path (host_verify=%s)" % (
        VERSION, host_verify))

    with open(config_path) as f:
        config = json.load(f)

    from infra.cvm import Cvm
    from infra.host_meta import collect_host_metadata
    from stages import present as stage_present

    if cvm is None:
        print("  Loading cluster inventory via existing CVM...")
        cvm = Cvm(cvm_ip)
    host = cvm.getHost(host_name)

    metadata = collect_host_metadata(host, cvm)

    num_runs = config.get("num_runs", 3)
    outdir = "results_%s" % time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(outdir, exist_ok=True)
    print("Results dir: %s" % outdir)

    completed = 0
    try:
        for run_num in range(1, num_runs + 1):
            results, count, cpus, events = run_one(
                cvm, host, config, run_num, host_verify=host_verify)
            title = {
                "vm_count": count,
                "expected_on": count,
                "host": host_name,
                "run": run_num,
                "num_runs": num_runs,
                "num_cpus": cpus,
                "config": config,
                "events": events,
                "metadata": metadata,
                "outdir": outdir,
            }
            print("\n========== RESULTS: RUN %d ==========\n" % run_num)
            stage_present.dump_results(title, results)
            completed += 1
    finally:
        print("\nFinished %d / %d run(s). Results: %s" % (
            completed, num_runs, outdir))
