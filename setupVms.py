#!/usr/bin/env python3

VERSION = "0.3"

from util.ObjFactory import *
from util.cvm import *
import sys, getopt, json

cvm_ip = ''
config_file = ''
host_name = ''


def run(from_unified=False):
    print("setupVms v%s" % VERSION)
    sys.stdout.flush()

    with open(config_file) as f:
        config = json.load(f)

    vm_size = config.get("vm_size_gb", 5)
    base_name = config.get("base_vm_name")
    clone_prefix = config.get("clone_prefix")
    if not base_name or not clone_prefix:
        print("ERROR: config must set both 'base_vm_name' and 'clone_prefix'.")
        sys.exit(2)
    workload_type = config["workload"]["type"]
    clone_buffer = config.get("clone_buffer", 5)

    # Cvm() runs ncli host/vm list — can take a while with many VMs; print so
    # it does not look hung right after the password prompt.
    print("Loading cluster inventory (ncli host/vm list)...")
    sys.stdout.flush()
    cvm = Cvm(cvm_ip)
    print("Inventory loaded (%d host(s), %d VM(s))." % (
        len(cvm.getHosts()), len(cvm._vmDic)))
    sys.stdout.flush()

    # Refuse to stack a second setup on top of existing base/clones unless
    # the user clears them (overhead.py already prompts when from_unified).
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
        print("  Clear them first, or use the unified CLI:")
        print("    python3 overhead.py --host %s --config %s --skip-setup"
              % (host_name, config_file))
        print("    python3 overhead.py --host %s --config %s --clear-existing"
              % (host_name, config_file))
        if not from_unified:
            print("")
            print("  Or delete manually, then re-run setupVms:")
            print("    acli vm.delete %s confirm=true" % base_name)
            print("    acli vm.delete %s_* confirm=true" % clone_prefix)
        sys.exit(2)

    from util.credentials import prompt_vm_password_once
    prompt_vm_password_once()

    # Step 1: Ensure disk image exists
    print("Checking disk image...")
    sys.stdout.flush()
    cvm.ensureImage()

    # Step 2: Ensure network exists
    print("Checking network...")
    sys.stdout.flush()
    cvm.ensureNetwork()

    # Step 3: Create base VM (memory + 2 cores per vcpu)
    print("Creating base VM: %s (%dGB, num_cores_per_vcpu=2)" % (base_name, vm_size))
    sys.stdout.flush()
    base_vm = cvm.vmCreate(base_name, vm_size)

    # Step 4: Attach disk and NIC
    print("Attaching disk image to base VM...")
    cvm.vmDiskCreate(base_name)
    print("Attaching NIC to base VM...")
    cvm.vmNicCreate(base_name)

    # Step 5: Power on and copy SSH key
    print("Powering on base VM (waiting for guest IP + SSH; up to ~5 min)...")
    sys.stdout.flush()
    base_vm.vmOn()

    print("Copying SSH key to base VM (so clones inherit passwordless access)...")
    sys.stdout.flush()
    vm_ip = base_vm.getIp()
    from libx.lib import install_ssh_pubkey
    key_path = install_ssh_pubkey(vm_ip, user="root")
    print("  Installed %s on root@%s" % (key_path, vm_ip))

    # Step 6: Install workload
    print("Installing workload on base VM...")
    workload = ObjFactory.getWorkloadObj(config["workload"]["type"])
    workload.setup(base_vm, config["workload"]["config"])
    print("Workload installed and enabled as systemd service.")

    # Step 7: Sync filesystem and power off base VM
    print("Syncing filesystem before power off...")
    base_vm.vm_cmd("sync")
    import time as _time
    _time.sleep(5)
    base_vm.vmOff()
    print("Base VM powered off.")

    # Step 8: Calculate clone count
    max_vms = cvm.getMaxVms(host_name, vm_size, clone_buffer)
    print("Calculated clone count: %d (including buffer of %d)" % (max_vms, clone_buffer))

    # Step 9: Clone
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

    # Step 10: Verify workload works on a clone
    svc_names = {"dirtyHarry": "dirty-harry", "fio": "fio-workload", "redis": "redis-workload"}
    test_clone_name = "%s_1" % clone_prefix
    test_clone = cvm._vmDic.get(test_clone_name)
    if test_clone:
        import time
        cvm.vmOn(test_clone)
        test_clone.waitForReady()
        time.sleep(10)

        svc_name = svc_names.get(workload_type, workload_type)
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
        else:
            print("  SUCCESS: %s is active and running on clone." % svc_name)

        cvm.vmOff(test_clone)
        time.sleep(5)
    else:
        print("  WARNING: Could not find clone %s to verify." % test_clone_name)

    print("\nSetup complete.")
    print("  Base VM: %s" % base_name)
    print("  Clones created: %d" % success)
    print("  Clone naming: %s_1 through %s_%d" % (clone_prefix, clone_prefix, success))
    print("  All VMs are powered off.")
    if not from_unified:
        print("\nRun overheadTest.py (or overhead.py) to start the experiment.")


def main(argv):
    global cvm_ip, config_file, host_name

    helpmsg = 'setupVms.py -i <cvmip> -f <configjson> -H <hostname>'
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
