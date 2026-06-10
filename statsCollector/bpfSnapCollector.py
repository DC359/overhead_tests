"""BPF snapshot + exit collector (libbpf/CO-RE).

Drop-in alternative to schedstatCollector that replaces /proc polling + streaming
bpftrace with a single in-kernel BPF program:

  * a 5s task-iterator sweep accounts every LIVE thread (run/wait delta vs a
    per-thread baseline kept in a kernel map), and
  * a sched_process_exit hook accounts threads that DIE between sweeps.

Both accumulate per-slice / per-service run+wait totals into per-CPU kernel maps;
userspace only copies the small scoreboards once per tick. This captures
long-running, newborn, dying and ephemeral threads with no per-event streaming
(so nothing can be "lost").

It emits the same per-tick `tick_result` structure as schedstatCollector, so the
existing csvDump / terminalDump consume it unchanged. Select it via
ObjFactory.getStatsCollectorObj("bpfsnap").
"""

from libx.objtmpl import StatsCollectorTmpl
from libx.lib import shell_run
from statsCollector.schedstatCollector import (
    _build_metrics, SLICE_NAMES, SLICE_PATHS, SERVICE_PARENT, LOCAL_FETCH_DIR,
)
import os
import time

# Local source dir holding the BPF + loader + Makefile.
BPF_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bpf")
BPF_SRC_FILES = ["schedstat_snap.bpf.c", "schedstat_snap.c", "Makefile"]

# A precompiled, statically-libbpf-linked binary built elsewhere (see
# bpf/build_static.sh / bpf/prebuilt/README.md). If present we ship it as-is and
# skip the on-host clang/bpftool/libbpf build entirely — the AHV host has no
# build toolchain.
PREBUILT_BIN = os.path.join(BPF_SRC_DIR, "prebuilt", "schedstat_snap")

# Remote (host) paths. The AHV host mounts /tmp, /var/tmp and /home noexec, so the
# binary MUST live under an exec-capable path; /root is the only one available.
# Write-only artifacts (results/pid/stderr) can stay in /tmp (noexec blocks exec,
# not writes).
REMOTE_BUILD_DIR = "/root/bpfsnap"
REMOTE_BIN = REMOTE_BUILD_DIR + "/schedstat_snap"
REMOTE_RESULTS_PATH = "/tmp/bpfsnap_results.txt"
REMOTE_PID_PATH = "/tmp/bpfsnap.pid"
REMOTE_STDERR_PATH = "/tmp/bpfsnap_stderr.txt"

# system.slice id is sys (level 1); ahv-cvm / ahv-uvms are level 2. Must match
# the SLICE_PATHS ordering: [cvm, uvms, system.slice].
SCHEDSTATS_PATH = "/proc/sys/kernel/sched_schedstats"


def _s(out):
    if out is None:
        return ""
    return out.strip() if isinstance(out, str) else out.decode().strip()


def _q(cmd):
    """Wrap a remote command in single quotes so shell metacharacters
    (|, &&, ||, ;, redirections) survive the SSH hop and are interpreted by the
    HOST shell rather than the intermediate CVM shell. The command must not
    contain single quotes itself — use double quotes inside if needed."""
    return "'" + cmd + "'"


def parse_snap_results(content):
    """Parse the aggregated BPF results file.

    Format per tick:
        TICK <n> <ts_start> <ts_end> <interval_s> MONO=<ns>
        SLICE <name> <run_ns> <wait_ns> <count>
        SERVICE <name> <run_ns> <wait_ns> <count>
        END_TICK

    Returns a list of dicts:
        {tick, ts_start, ts_end, interval_s, mono_ns,
         slices: {name: (run, wait, count)},
         services: {name: (run, wait, count)}}
    """
    ticks = []
    cur = None
    lines = content.split("\n") if isinstance(content, str) else content.decode().split("\n")
    for line in lines:
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "TICK" and len(parts) >= 5:
            mono_ns = 0
            for p in parts[5:]:
                if p.startswith("MONO=") and p.endswith("ns"):
                    try:
                        mono_ns = int(p[5:-2])
                    except ValueError:
                        pass
            try:
                cur = {
                    "tick": int(parts[1]),
                    "ts_start": parts[2],
                    "ts_end": parts[3],
                    "interval_s": int(parts[4]),
                    "mono_ns": mono_ns,
                    "slices": {},
                    "services": {},
                }
            except ValueError:
                cur = None
        elif parts[0] == "END_TICK":
            if cur is not None:
                ticks.append(cur)
            cur = None
        elif cur is not None and parts[0] in ("SLICE", "SERVICE") and len(parts) >= 5:
            try:
                run = int(parts[2]); wait = int(parts[3]); count = int(parts[4])
            except ValueError:
                continue
            key = "slices" if parts[0] == "SLICE" else "services"
            cur[key][parts[1]] = (run, wait, count)
    return ticks


class bpfSnapCollector(StatsCollectorTmpl):

    def setup(self, host, interval=5, label=""):
        self._host = host
        self._interval = interval
        self._label = label
        self._results = []
        self._num_cpus = 0
        self._loop_pid = None
        self._schedstats_changed = False
        self._schedstats_old = None
        self._use_prebuilt = os.path.isfile(PREBUILT_BIN)

        print("=" * 72)
        print("[COLLECTOR=bpfsnap] FULL eBPF snapshot+exit collector (libbpf/CO-RE)")
        print("  in-kernel: iter/task 5s sweep + sched_process_exit hook")
        print("  NO /proc polling, NO streaming bpftrace")
        print("  deploy mode: %s" % (
            "PREBUILT binary (no on-host build)" if self._use_prebuilt
            else "build on host (clang+bpftool+libbpf-devel)"))
        print("=" * 72)

        host.open_control()

        out = self._host.host_cmd("nproc")
        self._num_cpus = int(_s(out)) if _s(out) else 1

        host_ip = self._host.getHostIp()
        scp_opts = "-o StrictHostKeyChecking=no"
        if self._host._control_active:
            scp_opts += " -o ControlPath=%s" % self._host._control_socket

        self._preflight()

        # Clean any stale collector + outputs.
        self._host.host_cmd(_q(
            "pkill -f schedstat_snap 2>/dev/null || true; "
            "rm -f %s %s %s" % (REMOTE_RESULTS_PATH, REMOTE_PID_PATH, REMOTE_STDERR_PATH)))
        time.sleep(0.3)
        stale = _s(self._host.host_cmd(_q("pgrep -c schedstat_snap 2>/dev/null || echo 0")))
        if stale != "0":
            print("WARNING: %s stale schedstat_snap process(es) — force-killing" % stale)
            self._host.host_cmd(_q("pkill -9 -f schedstat_snap 2>/dev/null || true"))

        # Deploy to the host. Prefer the precompiled binary; only build on-host
        # when no prebuilt binary is shipped in the repo.
        self._host.host_cmd("mkdir -p %s" % REMOTE_BUILD_DIR)
        if self._use_prebuilt:
            print("Shipping precompiled schedstat_snap to host (%s)..." % REMOTE_BIN)
            shell_run("scp %s %s root@%s:%s" % (scp_opts, PREBUILT_BIN, host_ip, REMOTE_BIN))
            self._host.host_cmd(_q("chmod +x %s" % REMOTE_BIN))
            bin_ok = _s(self._host.host_cmd(_q("test -x %s && echo OK || echo MISSING" % REMOTE_BIN)))
            if bin_ok != "OK":
                raise RuntimeError(
                    "Failed to deploy prebuilt schedstat_snap to %s (is it noexec? "
                    "must be an exec-capable path)." % REMOTE_BIN)
            # Fail fast with a readable message if the binary can't even start
            # (e.g. a missing shared lib). It prints usage and returns 1 with no args.
            smoke = _s(self._host.host_cmd(_q("%s >/dev/null 2>&1; echo rc=$?" % REMOTE_BIN)))
            if smoke.startswith("rc=126") or smoke.startswith("rc=127"):
                diag = _s(self._host.host_cmd(_q("ldd %s 2>&1 | grep -i 'not found' || true" % REMOTE_BIN)))
                raise RuntimeError(
                    "Prebuilt schedstat_snap will not execute on host (%s). "
                    "Missing libs:\n%s" % (smoke, diag or "(none reported)"))
            print("Prebuilt schedstat_snap deployed and executable on host.")
        else:
            for fname in BPF_SRC_FILES:
                local = os.path.join(BPF_SRC_DIR, fname)
                if not os.path.isfile(local):
                    raise RuntimeError("BPF source missing locally: %s" % local)
                shell_run("scp %s %s root@%s:%s/%s" % (scp_opts, local, host_ip, REMOTE_BUILD_DIR, fname))

            print("Building BPF collector on host (clang + bpftool + libbpf)...")
            build_out = self._host.host_cmd(
                "'cd %s && make clean >/dev/null 2>&1; make 2>&1'" % REMOTE_BUILD_DIR)
            build_out = _s(build_out)
            bin_ok = _s(self._host.host_cmd(_q("test -x %s && echo OK || echo MISSING" % REMOTE_BIN)))
            if bin_ok != "OK":
                print("BPF build output:\n%s" % build_out[-2000:])
                raise RuntimeError(
                    "BPF collector build failed on host. Verify clang/bpftool/libbpf-devel, "
                    "or fall back to the bpftrace-based 'schedstat' collector.")
            print("BPF collector built successfully on host.")

        # Launch it (mirrors schedstatCollector's nohup pattern).
        self._host.host_cmd(
            "'nohup bash -c \"echo \\$\\$ > %s; exec %s %d %s %s %s %s %s %s\" "
            "</dev/null >>%s 2>&1 &'"
            % (REMOTE_PID_PATH, REMOTE_BIN, interval,
               REMOTE_RESULTS_PATH, REMOTE_PID_PATH,
               SLICE_PATHS[0], SLICE_PATHS[1], SLICE_PATHS[2], SERVICE_PARENT,
               REMOTE_STDERR_PATH))

        # Wait for the collector to report its pid.
        for attempt in range(6):
            time.sleep(2)
            pid_out = _s(self._host.host_cmd("cat %s 2>/dev/null" % REMOTE_PID_PATH))
            if pid_out:
                self._loop_pid = pid_out
                break
            if attempt == 5:
                diag = _s(self._host.host_cmd("cat %s 2>/dev/null" % REMOTE_STDERR_PATH))
                print("Collector stderr:\n%s" % (diag[-1500:] if diag else "(empty)"))
                raise RuntimeError("BPF collector failed to start — %s not created" % REMOTE_PID_PATH)

        time.sleep(3)
        alive = _s(self._host.host_cmd(
            _q("kill -0 %s 2>/dev/null && echo ALIVE || echo DEAD" % self._loop_pid)))
        if alive != "ALIVE":
            diag = _s(self._host.host_cmd("cat %s 2>/dev/null" % REMOTE_STDERR_PATH))
            print("Collector stderr:\n%s" % (diag[-1500:] if diag else "(empty)"))
            raise RuntimeError("BPF collector (pid=%s) exited shortly after start" % self._loop_pid)

        # Verify the BPF programs are actually loaded in the kernel — concrete
        # proof the full-BPF path (not a fallback) is live this run.
        prog_show = _s(self._host.host_cmd(
            _q('bpftool prog show 2>/dev/null | grep -cE "snap_iter|snap_exit" || echo 0')))
        iter_attached = _s(self._host.host_cmd(
            _q("bpftool link show 2>/dev/null | grep -ci iter || echo 0")))
        if prog_show == "0":
            print("WARNING: could not confirm snap_iter/snap_exit BPF progs via bpftool "
                  "(collector running, but verify it is the BPF path)")
        else:
            print("[verify] BPF programs loaded in kernel: %s prog(s) matching snap_iter/snap_exit, "
                  "%s iter link(s) attached" % (prog_show, iter_attached))

        print("BPF collector RUNNING (pid=%s, interval=%ds, cpus=%d) — full eBPF path confirmed"
              % (self._loop_pid, interval, self._num_cpus))

    def _preflight(self):
        """Enable wait/Y accounting and verify the host can run the libbpf design.
        Aborts with a clear message (pointing to the bpftrace fallback) if not."""
        # 1) schedstats — required for wait-time (Y). Enable + remember.
        cur = _s(self._host.host_cmd(_q("cat %s 2>/dev/null || echo NA" % SCHEDSTATS_PATH)))
        if cur == "NA" or cur == "":
            print("[preflight] WARNING: %s not exposed — wait-time (Y) may be unavailable" % SCHEDSTATS_PATH)
        elif cur != "1":
            self._host.host_cmd(_q("sysctl -w kernel.sched_schedstats=1 >/dev/null 2>&1 || "
                                   "echo 1 > %s" % SCHEDSTATS_PATH))
            now = _s(self._host.host_cmd(_q("cat %s 2>/dev/null || echo NA" % SCHEDSTATS_PATH)))
            if now == "1":
                self._schedstats_changed = True
                self._schedstats_old = cur
                print("[preflight] schedstats was OFF (%s); enabled it (=1) so wait-time is recorded "
                      "(will restore to %s on stop)" % (cur, cur))
            else:
                print("[preflight] WARNING: failed to enable schedstats (still %s); Y will be ~zero" % now)
        else:
            print("[preflight] schedstats already enabled (=1)")

        # 2) capability gate. Each command is wrapped via _q() so the HOST shell
        # (not the CVM shell) interprets the |, &&, || metacharacters.
        # Runtime prerequisites are always required. The build toolchain
        # (clang/bpftool/libbpf-devel) is only needed when compiling on-host;
        # with a shipped prebuilt binary the host needs none of it.
        checks = {
            "BTF": 'test -r /sys/kernel/btf/vmlinux && echo OK || echo MISSING',
            "task-iter": 'grep -qiE "bpf_iter.*task|task.*iter" /proc/kallsyms && echo OK || echo MISSING',
            "sched_process_exit": '( test -e /sys/kernel/tracing/events/sched/sched_process_exit || '
                                  'test -e /sys/kernel/debug/tracing/events/sched/sched_process_exit ) '
                                  '&& echo OK || echo MISSING',
        }
        if not self._use_prebuilt:
            checks.update({
                "clang": 'command -v clang >/dev/null 2>&1 && echo OK || echo MISSING',
                "bpftool": 'command -v bpftool >/dev/null 2>&1 && echo OK || echo MISSING',
                "libbpf-hdr": '( test -e /usr/include/bpf/libbpf.h || test -e /usr/local/include/bpf/libbpf.h ) '
                              '&& echo OK || echo MISSING',
                "libbpf-lib": 'ldconfig -p 2>/dev/null | grep -q libbpf && echo OK || echo MISSING',
            })
        results = {}
        for name, cmd in checks.items():
            results[name] = _s(self._host.host_cmd(_q(cmd)))
        summary = " ".join("%s=%s" % (k, results[k]) for k in checks)
        print("[preflight] host capabilities (%s): %s" % (
            "prebuilt" if self._use_prebuilt else "build-on-host", summary))

        missing = [k for k, v in results.items() if v != "OK"]
        if missing:
            if self._use_prebuilt:
                hint = ("These are kernel/runtime features the prebuilt binary needs; "
                        "they cannot be installed. Use the bpftrace-based 'schedstat' collector.")
            else:
                hint = ("Use the bpftrace-based 'schedstat' collector instead, ship a prebuilt "
                        "binary in bpf/prebuilt/, or install clang/llvm, bpftool, libbpf-devel.")
            raise RuntimeError(
                "Host missing required capabilities for the libbpf BPF collector: %s. %s"
                % (", ".join(missing), hint))

    def runx(self, num_ticks=0, phase=""):
        pass

    def stop(self):
        if self._loop_pid:
            self._host.host_cmd("kill %s 2>/dev/null" % self._loop_pid)
            time.sleep(1)
            self._host.host_cmd("kill -9 %s 2>/dev/null" % self._loop_pid)
        self._host.host_cmd(_q("pkill -f schedstat_snap 2>/dev/null || true"))

        if self._schedstats_changed and self._schedstats_old is not None:
            self._host.host_cmd(_q("sysctl -w kernel.sched_schedstats=%s >/dev/null 2>&1 || "
                                   "echo %s > %s"
                                   % (self._schedstats_old, self._schedstats_old, SCHEDSTATS_PATH)))
            print("[stop] restored schedstats to %s" % self._schedstats_old)
        print("BPF collector stopped (pid=%s)" % (self._loop_pid or "?"))

    def fetch_and_process(self, phase_timeline, warmup_cfg=None):
        if warmup_cfg is None:
            warmup_cfg = {"threshold_pct": 5, "consecutive": 3, "stable_ticks": 24}

        host_ip = self._host.getHostIp()
        scp_opts = "-o StrictHostKeyChecking=no"
        if self._host._control_active:
            scp_opts += " -o ControlPath=%s" % self._host._control_socket

        local_results = os.path.join(LOCAL_FETCH_DIR, "bpfsnap_results_fetched.txt")
        shell_run("scp %s root@%s:%s %s" % (scp_opts, host_ip, REMOTE_RESULTS_PATH, local_results))
        with open(local_results, "r") as f:
            content = f.read()

        raw_ticks = parse_snap_results(content)
        print("[COLLECTOR=bpfsnap] Fetched %d BPF ticks from host (interval=%ds, full eBPF path)"
              % (len(raw_ticks), self._interval))
        if not raw_ticks:
            print("WARNING: BPF collector produced 0 ticks — check %s on host" % REMOTE_STDERR_PATH)

        def get_phase_for_tick(wc):
            phase = "baseline"
            vm_count = 0
            for t_wc, t_phase, t_vms in phase_timeline:
                if wc >= t_wc:
                    phase = t_phase
                    vm_count = t_vms
                else:
                    break
            return phase, vm_count

        interval = self._interval
        all_tick_results = []
        prev_metrics = {}

        for idx, rt in enumerate(raw_ticks):
            initial_phase, vm_count = get_phase_for_tick(rt["ts_start"])
            tick_result = {
                "tick": idx + 1,
                "phase": initial_phase,
                "wall_clock": rt["ts_start"],
                "wall_clock_end": rt["ts_end"],
                "tick_delta_s": rt["interval_s"],
                "vm_count": vm_count,
                "time_delta": "%ds" % (idx * interval) if idx > 0 else "0s",
                "slices": {},
                "per_service": {},
                "cpustat_xcores": {},
                "mono_ns_start": rt["mono_ns"],
                "mono_ns_end": 0,
                "verify": {},
                "churn": {"total": 0, "born_died": 0, "born_only": 0, "died_only": 0,
                          "by_service": {}, "by_comm": {}, "details": []},
            }

            # Slices: kernel already gives per-interval run/wait deltas.
            for raw_name, disp_name in (("ahv-cvm.slice", "ahv-cvm.slice"),
                                        ("ahv-uvms.slice", "ahv-uvms.slice"),
                                        ("ahv.services", "ahv.services"),
                                        ("other", "other")):
                run, wait, count = rt["slices"].get(raw_name, (0, 0, 0))
                metrics = _build_metrics(run, wait, count, self._num_cpus, interval, 0, count)
                pm = prev_metrics.get(disp_name, {})
                metrics["pct_chg_X"] = round((metrics["X"] - pm["X"]) * 100.0 / pm["X"], 2) if pm.get("X", 0) > 0 else None
                metrics["pct_chg_Y"] = round((metrics["Y"] - pm["Y"]) * 100.0 / pm["Y"], 2) if pm.get("Y", 0) > 0 else None
                metrics["pct_chg_Z"] = round((metrics["Z"] - pm["Z"]) * 100.0 / pm["Z"], 2) if pm.get("Z", 0) > 0 else None
                tick_result["slices"][disp_name] = metrics
                prev_metrics[disp_name] = {"X": metrics["X"], "Y": metrics["Y"], "Z": metrics["Z"]}

            for svc_name, (run, wait, count) in rt["services"].items():
                tick_result["per_service"][svc_name] = _build_metrics(
                    run, wait, count, self._num_cpus, interval, 0, count)

            all_tick_results.append(tick_result)

        self._detect_phases(all_tick_results, phase_timeline, warmup_cfg)

        self._results = all_tick_results
        self._print_summary(all_tick_results)

    def _detect_phases(self, all_tick_results, phase_timeline, warmup_cfg):
        threshold_pct = warmup_cfg.get("threshold_pct", 5)
        consecutive_needed = warmup_cfg.get("consecutive", 3)
        stable_ticks_wanted = warmup_cfg.get("stable_ticks", 24)

        uvm_xcores = [tr["slices"].get("ahv-uvms.slice", {}).get("x_cores", 0.0)
                      for tr in all_tick_results]

        first_collecting_idx = None
        for i, tr in enumerate(all_tick_results):
            if tr["phase"] == "collecting":
                first_collecting_idx = i
                break

        stable_start_idx = None
        if first_collecting_idx is not None:
            consecutive_count = 0
            prev_xc = None
            for i in range(first_collecting_idx, len(uvm_xcores)):
                xc = uvm_xcores[i]
                if prev_xc is not None and xc > 0:
                    pct = abs(xc - prev_xc) / xc * 100.0
                    consecutive_count = consecutive_count + 1 if pct < threshold_pct else 0
                else:
                    consecutive_count = 0
                if consecutive_count >= consecutive_needed:
                    stable_start_idx = i - consecutive_needed + 1
                    break
                prev_xc = xc

        if stable_start_idx is None:
            print("[WARNING] Warmup convergence not detected (threshold=%d%%, consecutive=%d); "
                  "falling back to last %d ticks as stable."
                  % (threshold_pct, consecutive_needed, stable_ticks_wanted))
            stable_start_idx = max(0, len(all_tick_results) - stable_ticks_wanted)

        for i, tr in enumerate(all_tick_results):
            if tr["phase"] in ("baseline", "power_on"):
                continue
            tr["phase"] = "warmup" if i < stable_start_idx else "stable"

        warmup_ticks_count = stable_start_idx - (first_collecting_idx or 0)
        stable_ticks_count = len(all_tick_results) - stable_start_idx
        print("\n[PHASE DETECTION] threshold=%.0f%%, consecutive=%d" % (threshold_pct, consecutive_needed))
        print("  Warmup: ticks %d-%d (%d ticks, ~%ds)" % (
            (first_collecting_idx or 0) + 1, stable_start_idx,
            warmup_ticks_count, warmup_ticks_count * self._interval))
        print("  Stable: ticks %d-%d (%d ticks, ~%ds)" % (
            stable_start_idx + 1, len(all_tick_results),
            stable_ticks_count, stable_ticks_count * self._interval))

    def _print_summary(self, all_tick_results):
        for tr in all_tick_results:
            vm_str = " VMs=%d" % tr["vm_count"] if tr["vm_count"] > 0 else ""
            print("--- Tick %d [%s] %s->%s %s%s ---" % (
                tr["tick"], tr["time_delta"], tr["wall_clock"], tr["wall_clock_end"],
                tr["phase"], vm_str))
            for name in SLICE_NAMES + ["other"]:
                m = tr["slices"].get(name)
                if not m:
                    continue
                demand_s = round(m["Demand"] / 1e9, 2) if m["Demand"] else 0.0
                supply_s = round(m["Supply"] / 1e9, 2) if m["Supply"] else 0.0
                print("  %s: x_cores=%.2f y_cores=%.2f xy_cores=%.2f demand=%.2fs supply=%.2fs tasks=%d" % (
                    name, m["x_cores"], m["y_cores"], m["xy_cores"], demand_s, supply_s,
                    m.get("tasks_count", 0)))

    def exportStats(self):
        return self._results
