#!/usr/bin/env python3
# CLI: setup → validate → collect → present
"""
Orchestrator for AHV overhead experiments (v0.1.0).

Wires: setup → validate → collect → present.

Usage:
  python3 orchestrator.py --help
  python3 orchestrator.py --host NODE1 --validate
  python3 orchestrator.py --host NODE1 --config configs/test_quick.json --skip-setup
"""

from __future__ import print_function

import argparse
import json
import os
import sys
import tempfile

VERSION = "0.1.0"

WORKLOAD_CONFIGS = {
    "redis": "configs/redis.json",
    "fio": "configs/test_fio.json",
    "dirtyHarry": "configs/test.json",
}


def _require_clone_prefix(cfg, config_path="<config>"):
    prefix = (cfg or {}).get("clone_prefix")
    if not prefix:
        print("ERROR: config %s is missing required field 'clone_prefix'."
              % config_path)
        sys.exit(2)
    return prefix


def _resolve_config(args):
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
        config["collect_duration"] = max(1, int(args.duration))
        changed = True
    if args.clones is not None:
        config["clone_buffer"] = args.clones
        changed = True

    if not changed:
        return path, None

    fd, tmp = tempfile.mkstemp(prefix="orchestrator_cfg_", suffix=".json")
    with os.fdopen(fd, "w") as out:
        json.dump(config, out, indent=2)
        out.write("\n")
    return tmp, tmp


def _prompt_existing_vms(base_name, base_count, clone_prefix, clone_count,
                         clear_existing=False):
    print("")
    print("!" * 70)
    print("Existing VMs would collide with setup:")
    if base_count:
        print("  base_vm_name '%s': %d VM(s)" % (base_name, base_count))
    if clone_count:
        print("  clone_prefix '%s_*': %d VM(s)" % (clone_prefix, clone_count))
    print("")
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="orchestrator.py",
        description="AHV overhead experiment orchestrator (v%s). "
                    "Stages: setup → validate → collect → present." % VERSION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --host NODE1 --validate
  %(prog)s --host NODE1 --config configs/test_quick.json --skip-setup --runs 1
  %(prog)s --host NODE1 --workload redis
  %(prog)s --host NODE1 --config configs/redis.json --host-verify

Environment (optional):
  VM_PASSWORD   Guest SSH password used during setup
        """,
    )
    parser.add_argument("--cvm", "-i", default="",
                        help="CVM IP (omit if running on the CVM)")
    parser.add_argument("--host", "-H", required=True,
                        help="AHV host name from 'acli host.list'")

    cfg = parser.add_mutually_exclusive_group()
    cfg.add_argument("--config", "-f", help="Experiment JSON config")
    cfg.add_argument("--workload", "-w",
                     choices=sorted(WORKLOAD_CONFIGS.keys()),
                     help="Built-in config under configs/")

    parser.add_argument("--runs", type=int, default=None)
    parser.add_argument("--duration", type=int, default=None,
                        help="collect_duration seconds after VMs on")
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("--clones", type=int, default=None,
                        help="Override clone_buffer hint")
    parser.add_argument("--validate", action="store_true",
                        help="Connectivity + metadata only")
    parser.add_argument("--skip-setup", action="store_true")
    parser.add_argument("--clear-existing", action="store_true")
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument(
        "--host-verify", action="store_true",
        help="Also start cgtop/mpstat/sar during collect")
    parser.add_argument("--version", action="version",
                        version="orchestrator.py v%s" % VERSION)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    from stages import setup as stage_setup
    from stages import validate as stage_validate
    from stages import collect as stage_collect

    print("\n" + "=" * 70)
    print("Orchestrator v%s" % VERSION)
    print("=" * 70)
    print("  CVM:  %s" % (args.cvm or "(local)"))
    print("  Host: %s" % args.host)
    if args.host_verify:
        print("  Host-verify: ON")

    ok, cvm = stage_validate.run_connectivity(args.cvm, args.host)
    if not ok:
        print("ERROR: Connectivity validation failed.")
        sys.exit(1)

    if args.validate:
        stage_validate.run_metadata(cvm, args.host, print_lines=True)
        host = cvm.getHost(args.host)
        stage_validate.check_collector_binary(host)
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

        # Reuse the Cvm from connectivity — do not reload ncli inventory.
        n_clones = cvm.countVmsMatching(clone_prefix)
        n_base = cvm.countVmsNamed(base_name)

        do_setup = not args.skip_setup

        if n_clones == 0 and n_base == 0:
            if args.skip_setup:
                print("ERROR: no VMs matching base '%s' or clone_prefix '%s_*' "
                      "found, and --skip-setup was set." % (base_name, clone_prefix))
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
            stage_setup.run(args.cvm, args.host, config_path)
        else:
            print("\nSkipping VM setup.")
            # Soft check: binary should already be on host from a prior setup.
            host = cvm.getHost(args.host)
            stage_validate.check_collector_binary(host)

        if args.setup_only:
            print("\nSetup-only mode; skipping experiment.")
            sys.exit(0)

        print("\n--- Collect + Present (cgroup_cpu_snap) ---")
        stage_collect.run(
            args.cvm, args.host, config_path,
            host_verify=args.host_verify, cvm=cvm)
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


if __name__ == "__main__":
    main()
