#!/usr/bin/env python3
# CVM/AHV connectivity and host metadata
"""Validate stage — VERSION 0.1.0 (connectivity + optional metadata)."""

VERSION = "0.1.0"


def _s(out):
    if out is None:
        return ""
    return out.strip() if isinstance(out, str) else out.decode().strip()


def run_connectivity(cvm_ip, host_name):
    """Talk to the *existing* CVM + SSH to AHV.

    Returns (ok: bool, cvm_or_None). Building Cvm() loads ncli host/vm inventory
    once — that is the slow part, not 'creating a CVM'.
    """
    print("[stages.validate] v%s — connectivity" % VERSION)
    from infra.cvm import Cvm
    import sys

    print("  Loading cluster inventory via existing CVM (%s) "
          "[ncli host/vm list — can take a while]..."
          % (cvm_ip or "localhost"), end=" ")
    sys.stdout.flush()
    try:
        cvm = Cvm(cvm_ip)
        print("OK (%d host(s), %d VM(s))" % (
            len(cvm.getHosts()), len(cvm._vmDic)))
    except Exception as e:
        print("FAILED: %s" % e)
        return False, None

    print("  Checking CVM acli...", end=" ")
    sys.stdout.flush()
    try:
        out = cvm.cvm_cmd("acli host.list", quiet=True)
        if out is None:
            raise RuntimeError("empty response from acli host.list")
        print("OK")
    except Exception as e:
        print("FAILED: %s" % e)
        return False, None

    print("  Checking SSH to AHV host %s..." % host_name, end=" ")
    sys.stdout.flush()
    try:
        host = cvm.getHost(host_name)
        out = host.host_cmd("echo ok", quiet=True)
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if not out or "ok" not in out:
            raise RuntimeError("unexpected reply: %r" % out)
        print("OK (%s)" % host.getHostIp())
    except Exception as e:
        print("FAILED: %s" % e)
        return False, None

    print("Connectivity validated.\n")
    return True, cvm


def run_metadata(cvm, host_name, print_lines=True):
    """Collect host metadata using an existing Cvm handle (no second inventory load)."""
    from infra.host_meta import collect_host_metadata, format_metadata_lines

    print("[stages.validate] v%s — host metadata" % VERSION)
    host = cvm.getHost(host_name)
    meta = collect_host_metadata(host, cvm)
    if print_lines:
        print("\nHost metadata:")
        lines = format_metadata_lines(meta)
        if not lines:
            print("  (no metadata collected)")
        else:
            for line in lines:
                print("  %s" % line)
    return meta


def check_collector_binary(host, remote_path="/root/cgroup_cpu_snap"):
    """Soft check: binary present and executable on host."""
    out = _s(host.host_cmd(
        "'test -x %s && echo OK || echo MISSING'" % remote_path, quiet=True))
    if out != "OK":
        print("WARNING: collector not executable at %s on host (%s)" % (
            remote_path, out or "empty"))
        return False
    print("  Collector binary OK at %s" % remote_path)
    return True
