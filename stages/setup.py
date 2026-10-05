#!/usr/bin/env python3
# Create base VM, install workload, clone, deploy collector
"""Setup stage — VERSION 0.1.0

Create base VM, install workload, clone fleet, deploy cgroup_cpu_snap to AHV.
"""

VERSION = "0.1.0"

import json
import os
import subprocess
import sys
import time

from infra.cvm import Cvm
from infra.credentials import prompt_vm_password_once
from infra.factory import ObjFactory
from infra.retry import retry_call
from infra.ssh import install_ssh_pubkey, shell_run

LOCAL_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "cgroup_cpu_snap")
REMOTE_BIN = "/root/cgroup_cpu_snap"

SVC_NAMES = {
    "dirtyHarry": "dirty-harry",
    "fio": "fio-workload",
    "redis": "redis-workload",
}


def deploy_collector(cvm_ip, host_name):
    """Copy bin/cgroup_cpu_snap to /root/cgroup_cpu_snap on the AHV host."""
    if not os.path.isfile(LOCAL_BIN):
        raise RuntimeError("Local collector missing: %s" % LOCAL_BIN)

    print("[stages.setup] deploying collector to host...")
    cvm = Cvm(cvm_ip)
    host = cvm.getHost(host_name)
    host.open_control()
    host_ip = host.getHostIp()
    scp_opts = "-o StrictHostKeyChecking=no"
    if host._control_active:
        scp_opts += " -o ControlPath=%s" % host._control_socket

    retry_call(
        lambda: shell_run(
            "scp %s %s root@%s:%s" % (scp_opts, LOCAL_BIN, host_ip, REMOTE_BIN),
            timeout=60),
        max_attempts=2, delay=2,
        exceptions=(subprocess.CalledProcessError, TimeoutError, OSError))
    host.host_cmd("'chmod +x %s'" % REMOTE_BIN)
    out = host.host_cmd(
        "'test -x %s && echo OK || echo MISSING'" % REMOTE_BIN, quiet=True)
    if isinstance(out, bytes):
        out = out.decode(errors="replace")
    out = (out or "").strip()
    if out != "OK":
        raise RuntimeError("Collector not executable on host at %s" % REMOTE_BIN)
    print("[stages.setup] collector OK at %s" % REMOTE_BIN)


def _create_fleet(cvm_ip, host_name, config_path):
    print("[stages.setup] v%s" % VERSION)
    sys.stdout.flush()

    with open(config_path) as f:
        config = json.load(f)

    vm_size = config.get("vm_size_gb", 5)
    base_name = config.get("base_vm_name")
    clone_prefix = config.get("clone_prefix")
    if not base_name or not clone_prefix:
        print("ERROR: config must set both 'base_vm_name' and 'clone_prefix'.")
        sys.exit(2)
    workload_type = config["workload"]["type"]
    clone_buffer = config.get("clone_buffer", 5)

    print("Loading cluster inventory (ncli host/vm list)...")
    sys.stdout.flush()
    cvm = Cvm(cvm_ip)
    print("Inventory loaded (%d host(s), %d VM(s))." % (
        len(cvm.getHosts()), len(cvm._vmDic)))
    sys.stdout.flush()

    n_base = cvm.countVmsNamed(base_name)
    n_clones = cvm.countVmsMatching(clone_prefix)
    if n_base or n_clones:
        print("")
        print("ERROR: existing VMs would collide with setup:")
        if n_base:
            print("  base_vm_name '%s': %d" % (base_name, n_base))
        if n_clones:
            print("  clone_prefix '%s_*': %d" % (clone_prefix, n_clones))
        print("")
        print("  Clear them first, or:")
        print("    python3 orchestrator.py --host %s --config %s --skip-setup"
              % (host_name, config_path))
        print("    python3 orchestrator.py --host %s --config %s --clear-existing"
              % (host_name, config_path))
        sys.exit(2)

    prompt_vm_password_once()

    print("Checking disk image...")
    sys.stdout.flush()
    cvm.ensureImage()

    print("Checking network...")
    sys.stdout.flush()
    cvm.ensureNetwork()

    print("Creating base VM: %s (%dGB, num_cores_per_vcpu=2)" % (base_name, vm_size))
    sys.stdout.flush()
    base_vm = cvm.vmCreate(base_name, vm_size)

    print("Attaching disk image to base VM...")
    cvm.vmDiskCreate(base_name)
    print("Attaching NIC to base VM...")
    cvm.vmNicCreate(base_name)

    print("Powering on base VM (waiting for guest IP + SSH; up to ~5 min)...")
    sys.stdout.flush()
    base_vm.vmOn()

    print("Copying SSH key to base VM (so clones inherit passwordless access)...")
    sys.stdout.flush()
    vm_ip = base_vm.getIp()
    key_path = install_ssh_pubkey(vm_ip, user="root")
    print("  Installed %s on root@%s" % (key_path, vm_ip))

    print("Installing workload on base VM...")
    workload = ObjFactory.getWorkloadObj(config["workload"]["type"])
    workload.setup(base_vm, config["workload"]["config"])
    print("Workload installed and enabled as systemd service.")

    print("Syncing filesystem before power off...")
    base_vm.vm_cmd("sync")
    time.sleep(5)
    base_vm.vmOff()
    print("Base VM powered off.")

    max_vms = cvm.getMaxVms(host_name, vm_size, clone_buffer)
    if max_vms < 1:
        print("ERROR: clone count is %d — not enough free memory." % max_vms)
        sys.exit(1)
    print("Calculated clone count: %d (including buffer of %d)" % (max_vms, clone_buffer))

    print("Cloning %d VMs..." % max_vms)
    success = 0
    for i in range(1, max_vms + 1):
        clone_name = "%s_%d" % (clone_prefix, i)
        try:
            cvm.vmClone(base_name, clone_name)
            success += 1
            if success % 10 == 0:
                print("  Cloned %d/%d" % (success, max_vms))
        except Exception as e:
            print("  Clone %s failed: %s" % (clone_name, e))

    if success < 1:
        print("ERROR: 0 clones created.")
        sys.exit(1)

    test_clone_name = "%s_1" % clone_prefix
    test_clone = cvm._vmDic.get(test_clone_name)
    if not test_clone:
        print("ERROR: Could not find clone %s to verify." % test_clone_name)
        sys.exit(1)

    cvm.vmOn(test_clone)
    test_clone.waitForReady()
    time.sleep(10)

    svc_name = SVC_NAMES.get(workload_type, workload_type)
    print("\nVerifying workload (%s) on a clone..." % svc_name)
    status = test_clone.vm_cmd("systemctl is-active %s" % svc_name).strip()
    if status != "active":
        test_clone.vm_cmd("systemctl start %s" % svc_name)
        time.sleep(5)
        status = test_clone.vm_cmd("systemctl is-active %s" % svc_name).strip()
    print("  Clone %s: %s=%s" % (test_clone_name, svc_name, status))
    if status != "active":
        journal = test_clone.vm_cmd("journalctl -u %s --no-pager -n 10" % svc_name)
        print("  FAILED! %s is not running. Journal:\n%s" % (svc_name, journal))
        cvm.vmOff(test_clone)
        print("\nSetup FAILED: workload does not work on clones. Fix and re-run.")
        sys.exit(1)
    print("  SUCCESS: %s is active and running on clone." % svc_name)

    cvm.vmOff(test_clone)
    time.sleep(5)

    print("\nSetup complete.")
    print("  Base VM: %s" % base_name)
    print("  Clones created: %d" % success)
    print("  Clone naming: %s_1 through %s_%d" % (clone_prefix, clone_prefix, success))
    print("  All VMs are powered off.")


def run(cvm_ip, host_name, config_path):
    _create_fleet(cvm_ip, host_name, config_path)
    deploy_collector(cvm_ip, host_name)
