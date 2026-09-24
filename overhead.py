#!/usr/bin/env python3
"""
Unified overhead testing tool.

Combines VM setup and experiment execution. Host metadata (AHV, QEMU,
kernel, OS, libvirt, …) is collected automatically and written into
terminal output, CSV comment headers, and metadata_*.json.

Usage:
  python3 overhead.py --help
  python3 overhead.py --cvm 10.1.1.1 --host NODE1 --config sample/test_quick.json --skip-setup
  python3 overhead.py --cvm 10.1.1.1 --host NODE1 --workload redis
  python3 overhead.py --cvm 10.1.1.1 --host NODE1 --validate
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import tempfile

VERSION = "1.0"

WORKLOAD_CONFIGS = {
    "redis": "sample/redis.json",
    "fio": "sample/test_fio.json",
    "dirtyHarry": "sample/test.json",
}


def _require_clone_prefix(cfg, config_path="<config>"):
    """Return clone_prefix from config; exit if missing (workload-agnostic)."""
    prefix = (cfg or {}).get("clone_prefix")
    if not prefix:
        print("ERROR: config %s is missing required field 'clone_prefix' "
              "(e.g. \"redis_vm\", \"fio_vm\")." % config_path)
        sys.exit(2)
    return prefix


def _resolve_config(args):
    """Pick config path from --config or --workload; apply CLI overrides."""
    if args.config:
        path = args.config
    else:
        path = WORKLOAD_CONFIGS.get(args.workload)
        if not path:
            print("ERROR: unknown workload %r" % args.workload, file=sys.stderr)
            sys.exit(2)
        if not os.path.isfile(path):
            print("ERROR: default config not found: %s" % path, file=sys.stderr)
            sys.exit(2)

    with open(path) as f:
        config = json.load(f)

    changed = False
    if args.runs is not None:
        config["num_runs"] = args.runs
        changed = True
    if args.interval is not None:
        config["interval"] = args.interval
        changed = True
    if args.duration is not None:
        # Total seconds to collect after VMs are on (not stable_ticks).
        config["collect_duration"] = max(1, int(args.duration))
        changed = True
    if args.clones is not None:
        # setupVms derives clone count from host capacity; this only sets buffer-related
        # hint when present. Prefer editing the JSON for full control.
        config["clone_buffer"] = args.clones
        changed = True

    if not changed:
        return path, None

    # Write a temp config when CLI overrides were applied.
    fd, tmp = tempfile.mkstemp(prefix="overhead_cfg_", suffix=".json")
    with os.fdopen(fd, "w") as out:
        json.dump(config, out, indent=2)
        out.write("\n")
    return tmp, tmp


def validate_connectivity(cvm_ip, host_name):
    """Check CVM acli and SSH hop to the AHV host."""
    from util.cvm import Cvm

    print("\nValidating connectivity...")
    all_ok = True

    print("  Checking CVM acli at %s..." % (cvm_ip or "localhost"), end=" ")
    sys.stdout.flush()
    try:
        cvm = Cvm(cvm_ip)
        out = cvm.cvm_cmd("acli host.list", quiet=True)
        if out is None:
            raise RuntimeError("empty response from acli host.list")
        print("OK")
    except Exception as e:
        print("FAILED: %s" % e)
        all_ok = False
        return False

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
        all_ok = False

    if all_ok:
        print("Connectivity validated.\n")
    else:
        print("\nConnectivity validation FAILED. Fix issues and retry.\n")
    return all_ok


def print_host_metadata(cvm_ip, host_name):
    """Collect and print AHV/QEMU/kernel metadata for the target host."""
    from util.cvm import Cvm
    from util.host_meta import collect_host_metadata, format_metadata_lines

    cvm = Cvm(cvm_ip)
    host = cvm.getHost(host_name)
    print("\nHost metadata:")
    meta = collect_host_metadata(host, cvm)
    lines = format_metadata_lines(meta)
    if not lines:
        print("  (no metadata collected)")
    else:
        for line in lines:
            print("  %s" % line)
    return meta


def run_setup(cvm_ip, host_name, config_path):
    import setupVms
    setupVms.cvm_ip = cvm_ip or ""
    setupVms.host_name = host_name
    setupVms.config_file = config_path
    setupVms.run(from_unified=True)


def run_experiment(cvm_ip, host_name, config_path):
    import overheadTest
    overheadTest.cvm_ip = cvm_ip or ""
    overheadTest.host_name = host_name
    overheadTest.config_file = config_path
    overheadTest.run()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="overhead.py",
        description="Unified AHV overhead testing tool (v%s). "
                    "Collects host metadata (AHV, QEMU, kernel, OS, libvirt) "
                    "into terminal/CSV/JSON output." % VERSION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show this help
  %(prog)s --help

  # Connectivity check only (also prints host metadata)
  %(prog)s --cvm 10.1.1.1 --host NODE1 --validate

  # Quick experiment with existing VMs (skip setup)
  %(prog)s --cvm 10.1.1.1 --host NODE1 \\
      --config sample/test_quick.json --skip-setup

  # Setup + run (default 3 runs via sample/redis.json)
  %(prog)s --cvm 10.1.1.1 --host NODE1 --workload redis

  # Single run only when requested
  %(prog)s --cvm 10.1.1.1 --host NODE1 --config sample/redis.json \\
      --skip-setup --runs 1

  # Wipe matching base/clones and recreate (non-interactive)
  %(prog)s --host NODE1 --config sample/redis.json --clear-existing

  # Collect for 120s after VMs are on (1 run)
  %(prog)s --host NODE1 --config sample/redis.json \\
      --skip-setup --runs 1 --duration 120

Legacy scripts (still supported):
  python3 setupVms.py -i <cvm> -H <host> -f <config.json>
  python3 overheadTest.py -i <cvm> -H <host> -f <config.json>

Environment (optional; prompts if unset where needed):
  VM_PASSWORD   Guest SSH password used during setup
        """,
    )

    parser.add_argument(
        "--cvm", "-i",
        default="",
        help="CVM IP (omit if running on the CVM itself)",
    )
    parser.add_argument(
        "--host", "-H",
        required=True,
        help="AHV host name as shown by 'acli host.list' (or host IP if that is the Name)",
    )

    cfg = parser.add_mutually_exclusive_group()
    cfg.add_argument(
        "--config", "-f",
        help="Path to experiment JSON config (preferred for full control)",
    )
    cfg.add_argument(
        "--workload", "-w",
        choices=sorted(WORKLOAD_CONFIGS.keys()),
        help="Use a built-in sample config for this workload "
             "(redis→sample/redis.json, fio→sample/test_fio.json, "
             "dirtyHarry→sample/test.json)",
    )

    parser.add_argument(
        "--runs", type=int, default=None,
        help="Override num_runs (default is 3 unless the config sets num_runs; "
             "use --runs 1 for a single run)",
    )
    parser.add_argument(
        "--duration", type=int, default=None,
        help="Total collecting time in seconds after VMs are powered on "
             "(sets collect_duration; independent of stable_ticks labeling)",
    )
    parser.add_argument(
        "--interval", type=int, default=None,
        help="Override measurement interval seconds",
    )
    parser.add_argument(
        "--clones", type=int, default=None,
        help="Override clone_buffer hint in config (setup still sizes from host capacity)",
    )

    parser.add_argument(
        "--validate", action="store_true",
        help="Only validate CVM/host connectivity and print metadata; do not run",
    )
    parser.add_argument(
        "--skip-setup", action="store_true",
        help="Skip setupVms (assume clones already exist)",
    )
    parser.add_argument(
        "--clear-existing", action="store_true",
        help="If base/clones already exist, delete them and re-run setup "
             "(non-interactive; otherwise you are prompted)",
    )
    parser.add_argument(
        "--setup-only", action="store_true",
        help="Only run setupVms; do not run the experiment",
    )
    parser.add_argument(
        "--version", action="version",
        version="overhead.py v%s" % VERSION,
    )
    return parser


def _prompt_existing_vms(base_name, base_count, clone_prefix, clone_count,
                         clear_existing=False):
    """
    Existing base/clones would collide with setup.

    Returns:
      'skip'  — caller should skip setup and use existing VMs
      'clear' — caller should delete matching VMs then run setup

    Exits the process on abort / non-interactive with no --clear-existing.
    """
    print("")
    print("!" * 70)
    print("Existing VMs would collide with setup:")
    if base_count:
        print("  base_vm_name '%s': %d VM(s)" % (base_name, base_count))
    if clone_count:
        print("  clone_prefix '%s_*': %d VM(s)" % (clone_prefix, clone_count))
    print("")
    print("  Do NOT re-run setup on top of these (duplicate names / bad IPs).")
    print("  Options:")
    print("    [s] Skip setup — keep existing VMs and run the experiment")
    print("    [c] Clear base + clones matching this config, then run setup")
    print("    [a] Abort")
    print("!" * 70)

    if clear_existing:
        print("  (--clear-existing: clearing and re-running setup)")
        return "clear"

    if not sys.stdin.isatty():
        print("ERROR: non-interactive session. Re-run with one of:")
        print("  --skip-setup          use existing VMs")
        print("  --clear-existing      delete matching VMs then setup")
        sys.exit(2)

    while True:
        try:
            choice = input("Choice [s/c/a]: ").strip().lower()
        except EOFError:
            print("\nAborted.")
            sys.exit(2)
        if choice in ("s", "skip"):
            return "skip"
        if choice in ("c", "clear"):
            try:
                confirm = input(
                    "Type 'yes' to delete '%s' and '%s_*': " % (
                        base_name, clone_prefix)).strip().lower()
            except EOFError:
                print("\nAborted.")
                sys.exit(2)
            if confirm == "yes":
                return "clear"
            print("  Not confirmed; try again or abort.")
            continue
        if choice in ("a", "abort", "q", "quit"):
            print("Aborted.")
            sys.exit(2)
        print("  Enter s, c, or a.")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    print("\n" + "=" * 70)
    print("Overhead Testing Tool v%s" % VERSION)
    print("=" * 70)
    print("  CVM:  %s" % (args.cvm or "(local)"))
    print("  Host: %s" % args.host)

    if not validate_connectivity(args.cvm, args.host):
        print("ERROR: Connectivity validation failed.")
        sys.exit(1)

    # Metadata is collected once inside overheadTest.run() for experiments.
    # Only probe it here in --validate mode (otherwise it duplicates and is slow).
    if args.validate:
        print_host_metadata(args.cvm, args.host)
        print("Validation complete. Ready to run experiments.")
        sys.exit(0)

    if not args.config and not args.workload:
        parser.error("one of --config/-f or --workload/-w is required "
                     "(unless using --validate)")

    if args.skip_setup and args.clear_existing:
        parser.error("--skip-setup and --clear-existing cannot be used together")

    config_path, tmp_path = _resolve_config(args)
    try:
        print("  Config: %s" % (
            args.config or ("%s (%s)" % (args.workload, config_path))))

        with open(config_path) as f:
            cfg = json.load(f)
        clone_prefix = _require_clone_prefix(cfg, config_path)
        base_name = cfg.get("base_vm_name")
        if not base_name:
            print("ERROR: config %s is missing required field 'base_vm_name'."
                  % config_path)
            sys.exit(2)

        from util.cvm import Cvm
        cvm = Cvm(args.cvm)
        n_clones = cvm.countVmsMatching(clone_prefix)
        n_base = cvm.countVmsNamed(base_name)

        do_setup = not args.skip_setup

        if n_clones == 0 and n_base == 0:
            if args.skip_setup:
                print("ERROR: no VMs matching base '%s' or clone_prefix '%s_*' "
                      "found, and --skip-setup was set." % (base_name, clone_prefix))
                print("  Re-run without --skip-setup so setup can create them:")
                print("    python3 overhead.py --host %s --config %s"
                      % (args.host, args.config or config_path))
                sys.exit(1)
            print("No existing base/clones for this config — will run setup.")
        else:
            print("Found existing VMs for this config:")
            if n_base:
                print("  base_vm_name '%s': %d" % (base_name, n_base))
            if n_clones:
                print("  clone_prefix '%s_*': %d" % (clone_prefix, n_clones))

            if args.skip_setup:
                if n_clones == 0:
                    print("ERROR: --skip-setup set but no clones matching '%s_*'."
                          % clone_prefix)
                    sys.exit(1)
                print("  (--skip-setup: using existing VMs, not recreating)")
                do_setup = False
            else:
                action = _prompt_existing_vms(
                    base_name, n_base, clone_prefix, n_clones,
                    clear_existing=args.clear_existing)
                if action == "skip":
                    if n_clones == 0:
                        print("ERROR: cannot skip setup — no clones matching '%s_*'."
                              % clone_prefix)
                        sys.exit(1)
                    do_setup = False
                    print("Skipping setup; using existing VMs.")
                else:
                    print("\nClearing existing base/clones...")
                    if n_base:
                        cvm.deleteVmsNamed(base_name)
                    if n_clones:
                        cvm.deleteVmsMatchingPrefix(clone_prefix)
                    do_setup = True

        if do_setup:
            print("\n--- Setup ---")
            run_setup(args.cvm, args.host, config_path)
        else:
            print("\nSkipping VM setup.")

        if args.setup_only:
            print("\nSetup-only mode; skipping experiment.")
            sys.exit(0)

        print("\n--- Experiment ---")
        run_experiment(args.cvm, args.host, config_path)
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
