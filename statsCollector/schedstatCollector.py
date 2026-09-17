from libx.objtmpl import StatsCollectorTmpl
from libx.lib import shell_run
import os
import time

SLICE_PATHS = [
    "/sys/fs/cgroup/ahv.slice/ahv-cvm.slice",
    "/sys/fs/cgroup/ahv.slice/ahv-uvms.slice",
    "/sys/fs/cgroup/system.slice",
]
SLICE_NAMES = ["ahv-cvm.slice", "ahv-uvms.slice", "ahv.services"]
SERVICE_PARENT = "/sys/fs/cgroup/system.slice"

# Local fetch directory — must have enough space for large schedstat results files.
# /tmp on CVM is small (~488MB) so we use $HOME by default. Also used as gcc tempdir
# and to host the locally compiled binary, since /tmp on CVM is too small.
LOCAL_FETCH_DIR = os.environ.get("OVERHEAD_FETCH_DIR", os.path.expanduser("~"))

REMOTE_C_BINARY = "/run/user/0/schedstat_collect"
LOCAL_C_BINARY = os.path.join(LOCAL_FETCH_DIR, "schedstat_collect")
REMOTE_RESULTS_PATH = "/tmp/schedstat_results.txt"
REMOTE_PID_PATH = "/tmp/schedstat_loop.pid"

COLLECT_INTERVAL_MS = 200

LOCAL_C_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schedstat_collect.c")

BPFTRACE_URL = "http://woody.eng.nutanix.com/bpf/el8/bpftrace"
REMOTE_BPFTRACE = "/run/user/0/bpftrace"
REMOTE_BPF_LOG = "/tmp/bpf_ephemeral.txt"
REMOTE_BPF_PID = "/tmp/bpf_ephemeral.pid"
REMOTE_BPF_STDERR = "/tmp/bpf_stderr.txt"

BPF_SCRIPT = r"""
tracepoint:sched:sched_process_exit {
    $kn  = curtask->cgroups->dfl_cgrp->kn;
    $p1  = $kn->__parent;
    $n1  = str($p1->name);

    $depth = 0;
    $svc   = $kn;

    if ($n1 == "system.slice") {
        $depth = 1;
        $svc   = $kn;
    } else {
        $p2 = $p1->__parent;
        $n2 = str($p2->name);
        if ($n2 == "system.slice") {
            $depth = 2;
            $svc   = $p1;
        } else {
            $p3 = $p2->__parent;
            $n3 = str($p3->name);
            if ($n3 == "system.slice") {
                $depth = 3;
                $svc   = $p2;
            } else {
                $p4 = $p3->__parent;
                $n4 = str($p4->name);
                if ($n4 == "system.slice") {
                    $depth = 4;
                    $svc   = $p3;
                } else {
                    $p5 = $p4->__parent;
                    $n5 = str($p5->name);
                    if ($n5 == "system.slice") {
                        $depth = 5;
                        $svc   = $p4;
                    }
                }
            }
        }
    }

    if ($depth > 0) {
        printf("%llu %d %d %s %s %d %llu %llu\n",
            nsecs, pid, curtask->tgid, comm,
            str($svc->name),
            $depth,
            curtask->se.sum_exec_runtime,
            curtask->sched_info.run_delay);
    } else {
        @non_sys_exits++;
    }
}

END {
    printf("NON_SYS_SLICE_EXITS %lld\n", @non_sys_exits);
    clear(@non_sys_exits);
}
"""

# The C program is SCP'd and compiled on the host during setup().


def parse_collection_output(output):
    slices = {}
    services = {}
    cpustat = {}
    svc_cpustat = {}
    timing_str = ""

    lines = output.split("\n") if isinstance(output, str) else output.decode().split("\n")

    for line in lines:
        stripped = line.strip()
        parts = stripped.split()

        if len(parts) == 3 and parts[0] == "CPUSTAT":
            try:
                cpustat[parts[1]] = int(parts[2])
            except ValueError:
                pass
            continue

        if len(parts) == 3 and parts[0] == "SVC_CPUSTAT":
            try:
                svc_cpustat[parts[1]] = int(parts[2])
            except ValueError:
                pass
            continue

        if stripped.startswith("TIMING "):
            timing_str = stripped
            continue

        if len(parts) != 5:
            continue

        group_type, group_name, tid, run, wait = parts
        try:
            tid = int(tid)
            run = int(run)
            wait = int(wait)
        except ValueError:
            continue

        if group_type == "SLICE":
            if group_name not in slices:
                slices[group_name] = {}
            slices[group_name][tid] = (run, wait)
        elif group_type == "SERVICE":
            if group_name not in services:
                services[group_name] = {}
            services[group_name][tid] = (run, wait)

    return slices, services, cpustat, svc_cpustat, timing_str


def parse_results_file(content):
    """Parse the multi-tick results file.
    Returns list of (tick_num, ts_start, ts_end, delta_s, elapsed_sleep_str, raw_output, mono_ns) tuples.
    """
    ticks = []
    lines = content.split("\n") if isinstance(content, str) else content.decode().split("\n")
    current_tick = None
    current_ts_start = ""
    current_ts_end = ""
    current_delta = 0
    current_es_str = ""
    current_mono_ns = 0
    current_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TICK "):
            parts = stripped.split()
            if len(parts) >= 5:
                current_tick = int(parts[1])
                current_ts_start = parts[2]
                current_ts_end = parts[3]
                current_delta = int(parts[4])
                current_mono_ns = 0
                extra_parts = []
                for p in parts[5:]:
                    if p.startswith("MONO=") and p.endswith("ns"):
                        try:
                            current_mono_ns = int(p[5:-2])
                        except ValueError:
                            pass
                    else:
                        extra_parts.append(p)
                current_es_str = " ".join(extra_parts)
                current_lines = []
        elif stripped == "END_TICK" and current_tick is not None:
            ticks.append((current_tick, current_ts_start, current_ts_end, current_delta,
                          current_es_str, "\n".join(current_lines), current_mono_ns))
            current_tick = None
        elif current_tick is not None:
            current_lines.append(stripped)

    return ticks


def parse_bpf_log(path):
    """Parse bpf_ephemeral.txt produced by bpftrace.
    Event lines (8 fields): <mono_ns> <pid> <tgid> <comm> <service> <depth> <run_ns> <wait_ns>
    Compat (7 fields):      <mono_ns> <pid> <tgid> <comm> <service> <run_ns> <wait_ns>
    UNACCOUNTED lines (7):  UNACCOUNTED <mono_ns> <pid> <tgid> <comm> <cgroup> <run_ns> <wait_ns>
    Summary line:           SLICE_EXIT_SUMMARY ahv_cvm=N ahv_uvms=N unaccounted=N
    Returns (entries_sorted, slice_exit_summary_dict, unaccounted_entries).
    """
    entries = []
    unaccounted = []
    exit_summary = {"ahv_cvm": 0, "ahv_uvms": 0, "unaccounted": 0}
    if not os.path.exists(path):
        return entries, exit_summary, unaccounted
    with open(path, "r") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("SLICE_EXIT_SUMMARY"):
                for token in stripped.split():
                    if "=" in token:
                        k, v = token.split("=", 1)
                        try:
                            exit_summary[k] = int(v)
                        except ValueError:
                            pass
                continue
            if stripped.startswith("NON_SYS_SLICE_EXITS"):
                parts = stripped.split()
                if len(parts) >= 2:
                    try:
                        exit_summary["unaccounted"] = int(parts[1])
                    except ValueError:
                        pass
                continue
            if stripped.startswith("UNACCOUNTED"):
                parts = stripped.split()
                if len(parts) >= 8:
                    try:
                        unaccounted.append({
                            "mono_ns": int(parts[1]),
                            "pid": int(parts[2]),
                            "tgid": int(parts[3]),
                            "comm": parts[4],
                            "cgroup": parts[5],
                            "run_ns": int(parts[6]),
                            "wait_ns": int(parts[7]),
                        })
                    except (ValueError, IndexError):
                        pass
                continue
            parts = stripped.split()
            if len(parts) >= 8:
                try:
                    entries.append({
                        "mono_ns": int(parts[0]),
                        "pid": int(parts[1]),
                        "tgid": int(parts[2]),
                        "comm": parts[3],
                        "service": parts[4],
                        "depth": int(parts[5]),
                        "run_ns": int(parts[6]),
                        "wait_ns": int(parts[7]),
                    })
                except (ValueError, IndexError):
                    continue
            elif len(parts) >= 7:
                try:
                    entries.append({
                        "mono_ns": int(parts[0]),
                        "pid": int(parts[1]),
                        "tgid": int(parts[2]),
                        "comm": parts[3],
                        "service": parts[4],
                        "depth": 1,
                        "run_ns": int(parts[5]),
                        "wait_ns": int(parts[6]),
                    })
                except (ValueError, IndexError):
                    continue
    entries.sort(key=lambda e: e["mono_ns"])
    return entries, exit_summary, unaccounted


def _build_metrics(X, Y, N, num_cpus, interval, counted_tids):
    """Shared by schedstat and bpfsnap collectors (kept as helper; bpfsnap imports it)."""
    INTERVAL_NS = interval * 1000000000
    CPU_BUDGET_NS = interval * num_cpus * 1000000000
    T = interval * N
    T_ns = T * 1000000000
    Z = T_ns - X - Y if T_ns > (X + Y) else 0

    Supply = X
    if (X + Z) > 0:
        ratio_scaled = (X * 1000000) // (X + Z)
        fractional = (Y // 1000000) * ratio_scaled
        if fractional < 0:
            fractional = 0
        Demand = X + fractional
    else:
        fractional = 0
        Demand = X

    DemandSupplyRatio = (Demand * 100) // Supply if Supply > 0 else 0

    pct_running_x = round(X * 100.0 / T_ns, 2) if T_ns > 0 else 0.0
    pct_readyq_y = round(Y * 100.0 / T_ns, 2) if T_ns > 0 else 0.0
    pct_contention = round(Y * 100.0 / (X + Y), 2) if (X + Y) > 0 else 0.0
    pct_cpu_util = round(X * 100.0 / CPU_BUDGET_NS, 2) if CPU_BUDGET_NS > 0 else 0.0
    x_cores = round(X * 1.0 / INTERVAL_NS, 2) if INTERVAL_NS > 0 else 0.0
    y_cores = round(Y * 1.0 / INTERVAL_NS, 2) if INTERVAL_NS > 0 else 0.0
    xy_cores = round(x_cores + y_cores, 2)
    fractional_pct_supply = round(fractional * 100.0 / Supply, 2) if Supply > 0 else 0.0

    return {
        "X": X, "Y": Y, "Z": Z, "T": T, "tasks_count": N,
        "counted_tids": counted_tids,
        "Supply": Supply, "Demand": Demand,
        "DemandSupplyRatio": DemandSupplyRatio,
        "pct_running_x": pct_running_x,
        "pct_readyq_y": pct_readyq_y,
        "pct_contention": pct_contention,
        "pct_cpu_util": pct_cpu_util,
        "x_cores": x_cores, "y_cores": y_cores,
        "xy_cores": xy_cores,
        "fractional": fractional,
        "fractional_pct_supply": fractional_pct_supply,
    }


def compute_metrics(current_tids, prev_tids, num_cpus, interval):
    X = 0
    Y = 0
    new_prev = {}
    have_baseline = len(prev_tids) > 0

    for tid, (run, wait) in current_tids.items():
        new_prev[tid] = (run, wait)
        if tid in prev_tids:
            prev_run, prev_wait = prev_tids[tid]
            X += run - prev_run
            Y += wait - prev_wait
        elif have_baseline:
            X += run
            Y += wait

    N = len(current_tids)
    metrics = _build_metrics(X, Y, N, num_cpus, interval, N)
    return metrics, new_prev


class schedstatCollector(StatsCollectorTmpl):

    def setup(self, host, interval=30, label=""):
        self._host = host
        self._interval = interval
        self._label = label
        self._results = []
        self._num_cpus = 0
        self._bpf_pid = None

        print("=" * 72)
        print("[COLLECTOR=schedstat] LEGACY /proc-poll (%dms) + streaming bpftrace" % COLLECT_INTERVAL_MS)
        print("  (not the full eBPF collector — select 'bpfsnap' for that)")
        print("=" * 72)

        host.open_control()

        out = self._host.host_cmd("nproc")
        self._num_cpus = int(out.strip()) if isinstance(out, str) else int(out.decode().strip())

        host_ip = self._host.getHostIp()
        scp_opts = "-o StrictHostKeyChecking=no"
        if self._host._control_active:
            scp_opts += " -o ControlPath=%s" % self._host._control_socket

        print("Compiling C collector locally (TMPDIR=%s)..." % LOCAL_FETCH_DIR)
        shell_run("TMPDIR=%s gcc -O2 -o %s %s" % (LOCAL_FETCH_DIR, LOCAL_C_BINARY, LOCAL_C_SOURCE))
        if not os.path.isfile(LOCAL_C_BINARY):
            raise RuntimeError("C collector compilation failed locally")
        print("C collector compiled successfully, uploading to host...")

        self._host.host_cmd("pkill -f schedstat_collect 2>/dev/null || true; "
                            "pkill -f bpftrace 2>/dev/null || true; "
                            "pkill -f systemd-cgtop 2>/dev/null || true; "
                            "pkill -f mpstat 2>/dev/null || true; "
                            "pkill -f 'sar -q' 2>/dev/null || true; "
                            "rm -f %s %s %s %s %s /tmp/cgtop_log.txt /tmp/mpstat_log.txt /tmp/sar_log.txt "
                            "/tmp/cgtop_bg.pid /tmp/mpstat_bg.pid /tmp/sar_bg.pid"
                            % (REMOTE_RESULTS_PATH, REMOTE_PID_PATH,
                               REMOTE_BPF_LOG, REMOTE_BPF_PID, REMOTE_BPF_STDERR))
        time.sleep(0.5)
        stale = self._host.host_cmd("pgrep -c systemd-cgtop 2>/dev/null || echo 0")
        stale = stale.strip() if isinstance(stale, str) else stale.decode().strip()
        if stale != "0":
            print("WARNING: %s stale systemd-cgtop process(es) — force-killing" % stale)
            self._host.host_cmd("pkill -9 -f systemd-cgtop 2>/dev/null || true")

        stale_bpf = self._host.host_cmd("pgrep -c bpftrace 2>/dev/null || echo 0")
        stale_bpf = stale_bpf.strip() if isinstance(stale_bpf, str) else stale_bpf.decode().strip()
        if stale_bpf != "0":
            print("WARNING: %s stale bpftrace process(es) — force-killing" % stale_bpf)
            self._host.host_cmd("pkill -9 -f bpftrace 2>/dev/null || true")

        shell_run("scp %s %s root@%s:%s" % (scp_opts, LOCAL_C_BINARY, host_ip, REMOTE_C_BINARY))

        self._host.host_cmd("chmod +x %s" % REMOTE_C_BINARY)

        ldd_out = self._host.host_cmd("ldd %s 2>&1 || echo LDD_FAILED" % REMOTE_C_BINARY)
        if isinstance(ldd_out, bytes):
            ldd_out = ldd_out.decode()
        ldd_out = ldd_out.strip() if ldd_out else ""
        if "not found" in ldd_out or "LDD_FAILED" in ldd_out:
            print("WARNING: ldd output (may be OK for static binary):\n  %s" % ldd_out)
        else:
            print("Binary library check OK on host")

        bpf_check = self._host.host_cmd("test -x %s && echo OK || echo MISSING" % REMOTE_BPFTRACE)
        bpf_check = bpf_check.strip() if isinstance(bpf_check, str) else bpf_check.decode().strip()
        if bpf_check != "OK":
            print("Downloading bpftrace to host...")
            self._host.host_cmd("curl -s -o %s %s && chmod +x %s"
                                % (REMOTE_BPFTRACE, BPFTRACE_URL, REMOTE_BPFTRACE))
            bpf_check2 = self._host.host_cmd("test -x %s && echo OK || echo MISSING" % REMOTE_BPFTRACE)
            bpf_check2 = bpf_check2.strip() if isinstance(bpf_check2, str) else bpf_check2.decode().strip()
            if bpf_check2 != "OK":
                print("WARNING: bpftrace download failed — ephemeral thread tracing disabled")
        else:
            print("bpftrace already present on host")

        self._host.host_cmd(
            "nohup %s %d %s %s %s %s %s %s %s %s %s </dev/null >/dev/null 2>&1 &"
            % (REMOTE_C_BINARY, COLLECT_INTERVAL_MS, REMOTE_RESULTS_PATH, REMOTE_PID_PATH,
               SLICE_PATHS[0], SLICE_NAMES[0],
               SLICE_PATHS[1], SLICE_NAMES[1],
               SLICE_PATHS[2], SLICE_NAMES[2],
               SERVICE_PARENT)
        )
        time.sleep(0.5)
        self._host.host_cmd(
            "'nohup bash -c \"echo \\$\\$ > /tmp/cgtop_bg.pid; date > /tmp/cgtop_log.txt; exec systemd-cgtop -b -d %d -n 0 >> /tmp/cgtop_log.txt\" </dev/null >/dev/null 2>&1 &'" % interval
        )
        time.sleep(0.5)
        self._host.host_cmd(
            "'nohup bash -c \"echo \\$\\$ > /tmp/mpstat_bg.pid; exec mpstat %d >> /tmp/mpstat_log.txt\" </dev/null >/dev/null 2>&1 &'" % interval
        )
        time.sleep(0.5)
        self._host.host_cmd(
            "'nohup bash -c \"echo \\$\\$ > /tmp/sar_bg.pid; exec sar -q %d >> /tmp/sar_log.txt\" </dev/null >/dev/null 2>&1 &'" % interval
        )
        time.sleep(0.5)

        local_bpf_script = "/tmp/bpf_ephemeral.bt"
        with open(local_bpf_script, "w") as f:
            f.write(BPF_SCRIPT)
        shell_run("scp %s %s root@%s:/tmp/bpf_ephemeral.bt" % (scp_opts, local_bpf_script, host_ip))
        self._host.host_cmd(
            "'nohup bash -c \"echo \\$\\$ > %s; exec %s /tmp/bpf_ephemeral.bt >> %s 2>%s\" </dev/null >/dev/null 2>&1 &'"
            % (REMOTE_BPF_PID, REMOTE_BPFTRACE, REMOTE_BPF_LOG, REMOTE_BPF_STDERR)
        )

        for attempt in range(5):
            time.sleep(2)
            try:
                pid_out = self._host.host_cmd("cat %s 2>/dev/null" % REMOTE_PID_PATH)
                if pid_out:
                    self._loop_pid = pid_out.strip() if isinstance(pid_out, str) else pid_out.decode().strip()
                    if self._loop_pid:
                        break
            except Exception:
                pass
            if attempt == 4:
                try:
                    diag = self._host.host_cmd("ls -la %s 2>&1; pgrep -a schedstat 2>&1" % REMOTE_C_BINARY)
                    if isinstance(diag, bytes):
                        diag = diag.decode()
                    print("Diagnostic: %s" % (diag.strip() if diag else "no output"))
                except Exception:
                    pass
                raise RuntimeError("C collector failed to start — %s not created on host" % REMOTE_PID_PATH)

        bpf_pid = None
        for attempt in range(5):
            time.sleep(2)
            try:
                bp_out = self._host.host_cmd("cat %s 2>/dev/null" % REMOTE_BPF_PID)
                if bp_out:
                    bpf_pid = bp_out.strip() if isinstance(bp_out, str) else bp_out.decode().strip()
                    if bpf_pid:
                        break
            except Exception:
                pass
        if not bpf_pid:
            print("WARNING: bpftrace may not have started (pid file not found)")
        else:
            self._bpf_pid = bpf_pid
            time.sleep(3)
            alive_check = self._host.host_cmd("kill -0 %s 2>/dev/null && echo ALIVE || echo DEAD" % bpf_pid)
            alive_check = alive_check.strip() if isinstance(alive_check, str) else alive_check.decode().strip()
            if alive_check != "ALIVE":
                print("WARNING: bpftrace (pid=%s) exited shortly after start — likely BPF compilation error" % bpf_pid)
                try:
                    stderr_out = self._host.host_cmd("cat %s 2>/dev/null" % REMOTE_BPF_STDERR)
                    stderr_out = stderr_out.strip() if isinstance(stderr_out, str) else stderr_out.decode().strip()
                    if stderr_out:
                        print("bpftrace stderr:\n%s" % stderr_out[:1000])
                except Exception:
                    pass

        print("C collector started (pid=%s, collect=%dms, display=%ds), "
              "bpftrace started (pid=%s), cgtop+mpstat+sar also started"
              % (self._loop_pid, COLLECT_INTERVAL_MS, interval,
                 bpf_pid or "?"))

    def runx(self, num_ticks=0, phase=""):
        pass

    def stop(self):
        """Stop the C collector, bpftrace, cgtop, mpstat, and sar on the host."""
        self._host.host_cmd("kill %s 2>/dev/null" % self._loop_pid)
        time.sleep(1)
        self._host.host_cmd("kill -9 %s 2>/dev/null" % self._loop_pid)
        bp = getattr(self, "_bpf_pid", None)
        if bp:
            self._host.host_cmd("kill %s 2>/dev/null" % bp)
        self._host.host_cmd("pkill -f bpftrace 2>/dev/null || true")
        self._host.host_cmd("'kill $(cat /tmp/cgtop_bg.pid) 2>/dev/null; "
                            "kill $(cat /tmp/mpstat_bg.pid) 2>/dev/null; "
                            "kill $(cat /tmp/sar_bg.pid) 2>/dev/null'")
        self._host.host_cmd("pkill -f systemd-cgtop 2>/dev/null || true; "
                            "pkill -f mpstat 2>/dev/null || true; "
                            "pkill -f 'sar -q' 2>/dev/null || true")
        print("C collector stopped (pid=%s), bpftrace stopped (pid=%s), "
              "cgtop+mpstat+sar stopped" % (self._loop_pid, bp or "?"))

    def fetch_and_process(self, phase_timeline, warmup_cfg=None):
        """SCP the results file from host, parse all ticks, compute metrics,
        then retroactively detect warmup->stable transition using second derivative.

        Sub-ticks collected at COLLECT_INTERVAL_MS are aggregated into display
        ticks of self._interval seconds. For each display window, the first and
        last sub-tick snapshots are used for delta computation.

        warmup_cfg: {
            "threshold_pct": 5,    # |Δx_cores / x_cores| threshold for "settled"
            "consecutive": 3,      # how many consecutive ticks must be below threshold
            "stable_ticks": 24,    # how many ticks to label as "stable" after warmup
        }
        """
        if warmup_cfg is None:
            warmup_cfg = {"threshold_pct": 5, "consecutive": 3, "stable_ticks": 24}

        host_ip = self._host.getHostIp()
        local_results = os.path.join(LOCAL_FETCH_DIR, "schedstat_results_fetched.txt")
        scp_opts = "-o StrictHostKeyChecking=no"
        if self._host._control_active:
            scp_opts += " -o ControlPath=%s" % self._host._control_socket
        shell_run("scp %s root@%s:%s %s" % (scp_opts, host_ip, REMOTE_RESULTS_PATH, local_results))

        with open(local_results, "r") as f:
            content = f.read()

        raw_ticks = parse_results_file(content)
        print("Fetched %d sub-ticks from host (collect=%dms)" % (len(raw_ticks), COLLECT_INTERVAL_MS))

        local_bpf = os.path.join(LOCAL_FETCH_DIR, "bpf_ephemeral_fetched.txt")
        try:
            shell_run("scp %s root@%s:%s %s" % (scp_opts, host_ip, REMOTE_BPF_LOG, local_bpf))
        except Exception:
            print("WARNING: could not fetch bpf_ephemeral.txt from host")
            with open(local_bpf, "w") as f:
                pass
        bpf_entries, bpf_exit_summary, bpf_unaccounted = parse_bpf_log(local_bpf)
        bpf_depth_counts = {}
        for e in bpf_entries:
            d = e.get("depth", 1)
            bpf_depth_counts[d] = bpf_depth_counts.get(d, 0) + 1
        depth_detail = ", ".join("depth_%d=%d" % (d, c) for d, c in sorted(bpf_depth_counts.items()))
        nested_total = sum(c for d, c in bpf_depth_counts.items() if d > 1)
        non_sys_total = (bpf_exit_summary.get("ahv_cvm", 0)
                         + bpf_exit_summary.get("ahv_uvms", 0)
                         + bpf_exit_summary.get("unaccounted", 0))
        print("BPF ephemeral log: %d system.slice exit events (%s) — %d nested (depth>1), %d non-system.slice"
              % (len(bpf_entries), depth_detail or "none", nested_total, non_sys_total))

        local_bpf_stderr = os.path.join(LOCAL_FETCH_DIR, "bpf_stderr_fetched.txt")
        try:
            shell_run("scp %s root@%s:%s %s" % (scp_opts, host_ip, REMOTE_BPF_STDERR, local_bpf_stderr))
            with open(local_bpf_stderr, "r") as f:
                stderr_content = f.read().strip()
            if stderr_content:
                print("BPF stderr output:\n%s" % stderr_content[:500])
        except Exception:
            pass

        if len(bpf_entries) == 0:
            print("WARNING: BPF produced 0 events — bpftrace may have failed to compile or start")

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

        VERIFY_SERVICES = ["libvirtd.service", "ovs-vswitchd.service",
                           "ahv-host-agent.service", "ahv-storaged.service",
                           "NetworkManager.service", "vhostmd.service"]

        # --- Aggregate sub-ticks into display windows ---
        subticks_per_display = max(1, (self._interval * 1000) // COLLECT_INTERVAL_MS)
        print("Aggregating %d sub-ticks per display tick (display=%ds)" % (subticks_per_display, self._interval))

        display_windows = []
        for i in range(0, len(raw_ticks), subticks_per_display):
            window = raw_ticks[i:i + subticks_per_display]
            if window:
                display_windows.append(window)

        print("Aggregated into %d display ticks" % len(display_windows))

        # --- PASS 1: compute metrics at sub-tick (200ms) granularity ---
        #
        # For each display window we iterate *every* sub-tick so that:
        #   - schedstat deltas are accumulated at 200ms resolution
        #   - a high-water-mark (HWM) dict records the latest run/wait we've
        #     seen for every TID in any sub-tick of this display window (plus
        #     the carry-over from the previous display tick)
        #   - BPF dedup uses the HWM dict, eliminating double-counting even
        #     when a TID is born and dies within the same display tick.

        prev_subtick_slices = {}
        prev_subtick_services = {}
        prev_cpustat = {}
        prev_svc_cpustat = {}
        prev_metrics = {}
        all_tick_results = []
        all_timing = []
        all_es = []
        verify_data = {svc: [] for svc in VERIFY_SERVICES}

        for disp_idx, window in enumerate(display_windows):
            first_sub = window[0]
            last_sub = window[-1]

            first_tick_num, first_ts_start, _, _, _, _, first_mono_ns = first_sub
            last_tick_num, _, last_ts_end, last_delta, last_es_str, last_raw, last_mono_ns = last_sub

            total_elapsed = 0
            for _, _, _, _, es, _, _ in window:
                if es:
                    for part in es.split():
                        if part.startswith("ELAPSED="):
                            try:
                                total_elapsed += int(part.split("=")[1].replace("ms", ""))
                            except ValueError:
                                pass

            timing_str = "sub-ticks=%d total_collection=%dms" % (len(window), total_elapsed)
            es_str = last_es_str
            display_tick_num = disp_idx + 1

            # --- Sub-tick level accumulation for slices and services ---
            # X/Y accumulators per slice across all sub-ticks in this window
            slice_X = {n: 0 for n in SLICE_NAMES}
            slice_Y = {n: 0 for n in SLICE_NAMES}
            svc_X = {}
            svc_Y = {}

            # HWM: for BPF dedup — records the latest (run, wait) for every TID
            # seen in ahv.services across all sub-ticks of this display window.
            # Seeded from the previous display tick's final snapshot.
            hwm_svc_tids = dict(prev_subtick_slices.get("ahv.services", {}))
            have_baseline = len(prev_subtick_slices) > 0

            last_slices_parsed = {}
            last_services_parsed = {}
            last_cpustat_parsed = {}
            last_svc_cpustat_parsed = {}

            for sub_idx, sub in enumerate(window):
                _, _, _, _, _, sub_raw, _ = sub
                sub_slices, sub_services, sub_cpustat, sub_svc_cpustat, _ = parse_collection_output(sub_raw)

                if sub_idx == 0 and disp_idx == 0:
                    prev_subtick_slices = {n: dict(sub_slices.get(n, {})) for n in SLICE_NAMES}
                    prev_subtick_services = {sn: dict(sv) for sn, sv in sub_services.items()}
                    for tid, (r, w) in sub_slices.get("ahv.services", {}).items():
                        hwm_svc_tids[tid] = (r, w)
                    last_slices_parsed = sub_slices
                    last_services_parsed = sub_services
                    last_cpustat_parsed = sub_cpustat
                    last_svc_cpustat_parsed = sub_svc_cpustat
                    continue

                for n in SLICE_NAMES:
                    current_tids = sub_slices.get(n, {})
                    prev_tids = prev_subtick_slices.get(n, {})
                    for tid, (run, wait) in current_tids.items():
                        if tid in prev_tids:
                            prev_run, prev_wait = prev_tids[tid]
                            slice_X[n] += run - prev_run
                            slice_Y[n] += wait - prev_wait
                        elif have_baseline or sub_idx > 0:
                            slice_X[n] += run
                            slice_Y[n] += wait
                        if n == "ahv.services":
                            hwm_svc_tids[tid] = (run, wait)

                all_svc_this_sub = set(sub_services.keys())
                for svc_name in all_svc_this_sub:
                    current_tids = sub_services.get(svc_name, {})
                    prev_tids_svc = prev_subtick_services.get(svc_name, {})
                    if svc_name not in svc_X:
                        svc_X[svc_name] = 0
                        svc_Y[svc_name] = 0
                    for tid, (run, wait) in current_tids.items():
                        if tid in prev_tids_svc:
                            prev_run, prev_wait = prev_tids_svc[tid]
                            svc_X[svc_name] += run - prev_run
                            svc_Y[svc_name] += wait - prev_wait
                        elif have_baseline or sub_idx > 0:
                            svc_X[svc_name] += run
                            svc_Y[svc_name] += wait

                prev_subtick_slices = {n: dict(sub_slices.get(n, {})) for n in SLICE_NAMES}
                prev_subtick_services = {sn: dict(sv) for sn, sv in sub_services.items()}
                last_slices_parsed = sub_slices
                last_services_parsed = sub_services
                last_cpustat_parsed = sub_cpustat
                last_svc_cpustat_parsed = sub_svc_cpustat

            interval_ns = self._interval * 1000000000
            cpustat_xcores = {}
            interval_usec = self._interval * 1000000
            for name in SLICE_NAMES:
                curr = last_cpustat_parsed.get(name, 0)
                prev_val = prev_cpustat.get(name, 0)
                delta = curr - prev_val
                cpustat_xcores[name] = round(delta * 1.0 / interval_usec, 2) if interval_usec > 0 else 0.0
            prev_cpustat = last_cpustat_parsed

            initial_phase, vm_count = get_phase_for_tick(first_ts_start)

            tick_result = {
                "tick": display_tick_num,
                "phase": initial_phase,
                "wall_clock": first_ts_start,
                "wall_clock_end": last_ts_end,
                "tick_delta_s": last_delta,
                "vm_count": vm_count,
                "time_delta": "%ds" % ((display_tick_num - 1) * self._interval) if display_tick_num > 1 else "0s",
                "slices": {},
                "per_service": {},
                "cpustat_xcores": cpustat_xcores,
                "sub_tick_count": len(window),
                "mono_ns_start": first_mono_ns,
                "mono_ns_end": last_mono_ns,
            }

            for name in SLICE_NAMES:
                N = len(last_slices_parsed.get(name, {}))
                metrics = _build_metrics(slice_X[name], slice_Y[name], N, self._num_cpus, self._interval, N)

                prev_m = prev_metrics.get(name, {})
                metrics["pct_chg_X"] = round((metrics["X"] - prev_m["X"]) * 100.0 / prev_m["X"], 2) if prev_m.get("X", 0) > 0 else None
                metrics["pct_chg_Y"] = round((metrics["Y"] - prev_m["Y"]) * 100.0 / prev_m["Y"], 2) if prev_m.get("Y", 0) > 0 else None
                metrics["pct_chg_Z"] = round((metrics["Z"] - prev_m["Z"]) * 100.0 / prev_m["Z"], 2) if prev_m.get("Z", 0) > 0 else None

                tick_result["slices"][name] = metrics
                prev_metrics[name] = {"X": metrics["X"], "Y": metrics["Y"], "Z": metrics["Z"]}

            for svc_name in svc_X:
                N = len(last_services_parsed.get(svc_name, {}))
                metrics = _build_metrics(svc_X[svc_name], svc_Y[svc_name], N, self._num_cpus, self._interval, N)
                tick_result["per_service"][svc_name] = metrics

            # --- BPF ephemeral aggregation with sub-tick HWM dedup ---
            # hwm_svc_tids now contains the latest (run, wait) for every TID
            # seen in any sub-tick of this display window OR the previous one.
            mono_start = first_mono_ns
            mono_end = last_mono_ns
            if disp_idx + 1 < len(display_windows):
                next_first = display_windows[disp_idx + 1][0]
                mono_end = next_first[6]

            eph_by_svc = {}
            eph_dedup_hwm = 0
            eph_nested_count = 0
            tick_depth_counts = {}
            if mono_start == 0 and mono_end == 0:
                pass
            else:
                for e in bpf_entries:
                    if mono_start > 0 and e["mono_ns"] < mono_start:
                        continue
                    if mono_end > 0 and e["mono_ns"] >= mono_end:
                        break

                    tid = e["pid"]
                    d = e.get("depth", 1)
                    tick_depth_counts[d] = tick_depth_counts.get(d, 0) + 1

                    if d > 1:
                        eph_nested_count += 1

                    if tid in hwm_svc_tids:
                        hwm_run, hwm_wait = hwm_svc_tids[tid]
                        run_contribution = max(0, e["run_ns"] - hwm_run)
                        wait_contribution = max(0, e["wait_ns"] - hwm_wait)
                        eph_dedup_hwm += 1
                    else:
                        run_contribution = e["run_ns"]
                        wait_contribution = e["wait_ns"]

                    svc = e["service"]
                    if svc not in eph_by_svc:
                        eph_by_svc[svc] = {"run_ns": 0, "wait_ns": 0, "count": 0}
                    eph_by_svc[svc]["run_ns"] += run_contribution
                    eph_by_svc[svc]["wait_ns"] += wait_contribution
                    eph_by_svc[svc]["count"] += 1

            eph_total_run = 0
            eph_total_wait = 0
            eph_total_count = 0
            for svc, ed in eph_by_svc.items():
                ex = round(ed["run_ns"] / interval_ns, 4) if interval_ns > 0 else 0.0
                ey = round(ed["wait_ns"] / interval_ns, 4) if interval_ns > 0 else 0.0
                if svc in tick_result["per_service"]:
                    tick_result["per_service"][svc]["ephemeral_x_cores"] = ex
                    tick_result["per_service"][svc]["ephemeral_y_cores"] = ey
                    tick_result["per_service"][svc]["ephemeral_count"] = ed["count"]
                    tick_result["per_service"][svc]["total_x_cores"] = round(
                        tick_result["per_service"][svc].get("x_cores", 0) + ex, 4)
                    tick_result["per_service"][svc]["total_y_cores"] = round(
                        tick_result["per_service"][svc].get("y_cores", 0) + ey, 4)
                eph_total_run += ed["run_ns"]
                eph_total_wait += ed["wait_ns"]
                eph_total_count += ed["count"]

            svc_slice = "ahv.services"
            if svc_slice in tick_result["slices"]:
                tick_result["slices"][svc_slice]["ephemeral_x_cores"] = round(
                    eph_total_run / interval_ns, 4) if interval_ns > 0 else 0.0
                tick_result["slices"][svc_slice]["ephemeral_y_cores"] = round(
                    eph_total_wait / interval_ns, 4) if interval_ns > 0 else 0.0
                tick_result["slices"][svc_slice]["ephemeral_count"] = eph_total_count
                tick_result["slices"][svc_slice]["total_x_cores"] = round(
                    tick_result["slices"][svc_slice].get("x_cores", 0)
                    + (eph_total_run / interval_ns if interval_ns > 0 else 0), 4)
            tick_result["bpf_dedup"] = {"hwm_dedup": eph_dedup_hwm,
                                        "counted": eph_total_count, "nested": eph_nested_count,
                                        "depth_counts": tick_depth_counts}

            # --- Sanity checks: compare total_X against cpu.stat ---
            total_X_ns = slice_X.get("ahv.services", 0) + eph_total_run
            cpustat_svc_ns = cpustat_xcores.get("ahv.services", 0) * interval_ns
            if cpustat_svc_ns > 0 and total_X_ns > cpustat_svc_ns * 1.10:
                overshoot_pct = (total_X_ns - cpustat_svc_ns) * 100.0 / cpustat_svc_ns
                tick_result["sanity_warning"] = (
                    "total_X exceeds cpu.stat by %.1f%% (total_X=%.4f, cpustat=%.4f x_cores)"
                    % (overshoot_pct,
                       total_X_ns / interval_ns if interval_ns > 0 else 0,
                       cpustat_xcores.get("ahv.services", 0)))

            tick_verify = {}
            for vsvc in VERIFY_SERVICES:
                curr_usec = last_svc_cpustat_parsed.get(vsvc, 0)
                prev_usec = prev_svc_cpustat.get(vsvc, 0)
                cpustat_delta_ns = (curr_usec - prev_usec) * 1000
                svc_metrics = tick_result["per_service"].get(vsvc, {})
                schedstat_x = svc_metrics.get("X", 0)
                verify_data[vsvc].append((display_tick_num, cpustat_delta_ns, schedstat_x))
                tick_verify[vsvc] = {"cpustat_ns": cpustat_delta_ns, "schedstat_ns": schedstat_x}
            tick_result["verify"] = tick_verify
            prev_svc_cpustat = last_svc_cpustat_parsed

            all_tick_results.append(tick_result)
            all_timing.append(timing_str)
            all_es.append(es_str)

        # --- PASS 2: detect warmup->stable using second derivative on ahv-uvms.slice ---
        threshold_pct = warmup_cfg.get("threshold_pct", 5)
        consecutive_needed = warmup_cfg.get("consecutive", 3)
        stable_ticks_wanted = warmup_cfg.get("stable_ticks", 24)

        uvm_xcores = []
        for tr in all_tick_results:
            uvm_xcores.append(tr["slices"].get("ahv-uvms.slice", {}).get("x_cores", 0.0))

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
                    delta = abs(xc - prev_xc)
                    pct = (delta / xc) * 100.0
                    if pct < threshold_pct:
                        consecutive_count += 1
                    else:
                        consecutive_count = 0
                else:
                    consecutive_count = 0

                if consecutive_count >= consecutive_needed:
                    stable_start_idx = i - consecutive_needed + 1
                    break
                prev_xc = xc

        if stable_start_idx is None:
            print("[WARNING] Warmup convergence not detected (threshold=%d%%, consecutive=%d)." % (
                threshold_pct, consecutive_needed))
            print("          Falling back: last %d ticks as stable." % stable_ticks_wanted)
            stable_start_idx = max(0, len(all_tick_results) - stable_ticks_wanted)

        # --- PASS 3: retag phases ---
        for i, tr in enumerate(all_tick_results):
            if tr["phase"] == "baseline" or tr["phase"] == "power_on":
                pass
            elif i < stable_start_idx:
                tr["phase"] = "warmup"
            else:
                tr["phase"] = "stable"

        warmup_ticks_count = stable_start_idx - (first_collecting_idx or 0)
        stable_ticks_count = len(all_tick_results) - stable_start_idx
        print("\n[PHASE DETECTION] threshold=%.0f%%, consecutive=%d" % (threshold_pct, consecutive_needed))
        print("  Warmup: ticks %d-%d (%d ticks, ~%ds)" % (
            (first_collecting_idx or 0) + 1, stable_start_idx,
            warmup_ticks_count, warmup_ticks_count * self._interval))
        print("  Stable: ticks %d-%d (%d ticks, ~%ds)" % (
            stable_start_idx + 1, len(all_tick_results),
            stable_ticks_count, stable_ticks_count * self._interval))

        # --- PASS 4: bucket BPF exit events into tick windows for churn data ---
        for i, tr in enumerate(all_tick_results):
            mono_s = tr.get("mono_ns_start", 0)
            mono_e = tr.get("mono_ns_end", 0)
            if i + 1 < len(all_tick_results):
                mono_e = all_tick_results[i + 1].get("mono_ns_start", mono_e)

            exit_details = []
            by_service = {}
            by_comm = {}
            for e in bpf_entries:
                if mono_s > 0 and e["mono_ns"] < mono_s:
                    continue
                if mono_e > 0 and e["mono_ns"] >= mono_e:
                    break
                exit_details.append({
                    "pid": e["pid"], "comm": e["comm"],
                    "cgroup": e["service"], "depth": e.get("depth", 1),
                    "churn_type": "exit",
                    "run_ns": e["run_ns"], "wait_ns": e["wait_ns"],
                })
                by_service[e["service"]] = by_service.get(e["service"], 0) + 1
                by_comm[e["comm"]] = by_comm.get(e["comm"], 0) + 1

            tr["churn"] = {
                "born_died": len(exit_details),
                "born_only": 0,
                "died_only": 0,
                "total": len(exit_details),
                "by_service": by_service,
                "by_comm": by_comm,
                "bd_by_service": by_service,
                "bd_by_comm": by_comm,
                "details": exit_details,
                "per_svc_churn": {},
            }

        # --- Output all ticks ---
        self._bpf_summary = {
            "total_events": len(bpf_entries),
            "depth_counts": dict(bpf_depth_counts),
            "non_sys_exits": non_sys_total,
            "exit_summary": bpf_exit_summary,
        }
        self._results = all_tick_results
        for i, tr in enumerate(all_tick_results):
            vm_str = " VMs=%d" % tr["vm_count"] if tr["vm_count"] > 0 else ""
            delta_str = " delta=%ds" % tr["tick_delta_s"] if tr["tick_delta_s"] > 0 else ""
            print("--- Tick %d [%s] %s->%s%s %s%s ---" % (
                tr["tick"], tr["time_delta"], tr["wall_clock"], tr["wall_clock_end"],
                delta_str, tr["phase"], vm_str))
            for name in SLICE_NAMES:
                m = tr["slices"][name]
                demand_s = round(m["Demand"] / 1e9, 2) if m["Demand"] else 0.0
                supply_s = round(m["Supply"] / 1e9, 2) if m["Supply"] else 0.0
                print("  %s: x_cores=%.2f y_cores=%.2f xy_cores=%.2f demand=%.2fs supply=%.2fs" % (
                    name, m["x_cores"], m["y_cores"], m["xy_cores"], demand_s, supply_s))
            extra = []
            if all_timing[i]:
                extra.append(all_timing[i])
            if all_es[i]:
                extra.append(all_es[i])
            if extra:
                print("  [%s]" % " | ".join(extra))
            churn = tr.get("churn", {})
            if churn.get("total", 0) > 0:
                svc_parts = sorted(churn["by_service"].items(), key=lambda x: -x[1])
                svc_str = ", ".join("%s(%d)" % (s, c) for s, c in svc_parts[:5])
                comm_parts = sorted(churn["by_comm"].items(), key=lambda x: -x[1])
                comm_str = ", ".join("%s(x%d)" % (n, c) for n, c in comm_parts[:5])
                print("  [BPF EXITS] %d threads exited | services: %s" % (churn["total"], svc_str))
                print("              names: %s" % comm_str)

            bpf_dd = tr.get("bpf_dedup", {})
            eph_svc_parts = []
            for svc_name, sm in sorted(tr.get("per_service", {}).items()):
                ec = sm.get("ephemeral_count", 0)
                if ec > 0:
                    eph_svc_parts.append("%s: +%.3f x_cores (%d exits)" % (
                        svc_name, sm.get("ephemeral_x_cores", 0), ec))
            if eph_svc_parts:
                print("  [BPF EPH] %s" % " | ".join(eph_svc_parts[:5]))
            svc_m = tr["slices"].get("ahv.services", {})
            total_xc = svc_m.get("total_x_cores")
            if total_xc is not None:
                print("  [ahv.services total_x_cores=%.4f (schedstat=%.4f + bpf_eph=%.4f) dedup: hwm=%d counted=%d nested=%d]" % (
                    total_xc, svc_m.get("x_cores", 0), svc_m.get("ephemeral_x_cores", 0),
                    bpf_dd.get("hwm_dedup", 0), bpf_dd.get("counted", 0),
                    bpf_dd.get("nested", 0)))
            sanity_w = tr.get("sanity_warning")
            if sanity_w:
                print("  [SANITY WARNING] %s" % sanity_w)

        # --- VERIFICATION: per-service cpu.stat vs schedstat vs schedstat+bpf ---
        print("\n" + "=" * 140)
        print("VERIFICATION: cpu.stat vs schedstat vs schedstat+bpf (per-service)")
        print("=" * 140)
        print("%-28s %4s %14s %14s %14s %8s %8s %5s" % (
            "Service", "Tick", "cpu.stat(ns)", "schedstat(ns)", "total(ns)", "Diff%", "TotalD%", "Eph#"))
        print("-" * 140)
        for vsvc in VERIFY_SERVICES:
            for tick_num, cpustat_ns, schedstat_ns in verify_data[vsvc]:
                if cpustat_ns == 0 and schedstat_ns == 0:
                    continue
                tr = all_tick_results[tick_num - 1] if tick_num <= len(all_tick_results) else {}
                svc_m = tr.get("per_service", {}).get(vsvc, {})
                eph_run = svc_m.get("ephemeral_x_cores", 0) * self._interval * 1000000000
                total_ns = schedstat_ns + int(eph_run)
                eph_count = svc_m.get("ephemeral_count", 0)

                diff_pct = (schedstat_ns - cpustat_ns) * 100.0 / cpustat_ns if cpustat_ns > 0 else 0.0
                total_diff_pct = (total_ns - cpustat_ns) * 100.0 / cpustat_ns if cpustat_ns > 0 else 0.0
                print("%-28s %4d %14d %14d %14d %7.1f%% %7.1f%% %5d" % (
                    vsvc, tick_num, cpustat_ns, schedstat_ns, total_ns,
                    diff_pct, total_diff_pct, eph_count))
        print("=" * 140)
        print("Diff%  = (schedstat - cpu.stat) / cpu.stat  — without BPF ephemeral")
        print("TotalD% = (schedstat+bpf - cpu.stat) / cpu.stat — with BPF (should be closer to 0)")
        print("")

    def exportStats(self):
        return self._results
