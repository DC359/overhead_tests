#!/usr/bin/env python3

VERSION = "0.3"

from util.ObjFactory import *
from util.cvm import *
import sys, getopt, json

cvm_ip = ''
config_file = ''
host_name = ''


def run():
    print("setupVms v%s" % VERSION)
    with open(config_file) as f:
        config = json.load(f)

    vm_size = config.get("vm_size_gb", 5)
    base_name = config.get("base_vm_name", "dirty_harry_base")
    clone_prefix = config.get("clone_prefix", "dirty_harry")
    clone_buffer = config.get("clone_buffer", 5)

    cvm = Cvm(cvm_ip)

    # Step 1: Ensure disk image exists
    cvm.ensureImage()

    # Step 2: Ensure network exists
    cvm.ensureNetwork()

    # Step 3: Create base VM (memory + 2 cores per vcpu)
    print("Creating base VM: %s (%dGB, num_cores_per_vcpu=2)" % (base_name, vm_size))
    base_vm = cvm.vmCreate(base_name, vm_size)

    # Step 4: Attach disk and NIC
    print("Attaching disk image to base VM...")
    cvm.vmDiskCreate(base_name)
    print("Attaching NIC to base VM...")
    cvm.vmNicCreate(base_name)

    # Step 5: Power on and copy SSH key
    print("Powering on base VM...")
    base_vm.vmOn()

    print("Copying SSH key to base VM (so clones inherit passwordless access)...")
    vm_ip = base_vm.getIp()
    import subprocess
    subprocess.run(
        ["sshpass", "-p", "nutanix/4u", "ssh-copy-id", "-o", "StrictHostKeyChecking=no", "root@%s" % vm_ip],
        check=True
    )

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

    # --- IPERF_ADDITION START: resolve host UUID for affinity pinning ---
    workload_type = config["workload"]["type"]
    host_uuid = None
    if workload_type == "iperf":
        host_obj = cvm.getHost(host_name)
        host_uuid = host_obj.getHostUuid()
        if host_uuid:
            print("Will pin clones to host %s (uuid=%s)" % (host_name, host_uuid))
        else:
            print("[WARNING] Could not resolve UUID for host %s — clones will NOT be pinned." % host_name)
    # --- IPERF_ADDITION END ---

    # Step 9: Clone
    print("Cloning %d VMs..." % max_vms)
    success = 0
    for i in range(1, max_vms + 1):
        clone_name = "%s_%d" % (clone_prefix, i)
        try:
            cvm.vmClone(base_name, clone_name)
            # --- IPERF_ADDITION START: pin clone to target host ---
            if workload_type == "iperf" and host_uuid:
                cvm.vmAffinitySet(clone_name, host_uuid)
            # --- IPERF_ADDITION END ---
            success += 1
            if success % 10 == 0:
                print("  Cloned %d/%d" % (success, max_vms))
        except Exception as e:
            print("  Clone %s failed: %s" % (clone_name, e))

    # Step 10: Verify workload works on a clone
    svc_names = {"dirtyHarry": "dirty-harry", "fio": "fio-workload"}
    test_clone_name = "%s_1" % clone_prefix
    test_clone = cvm._vmDic.get(test_clone_name)
    if test_clone:
        import time
        cvm.vmOn(test_clone)
        test_clone.waitForReady()
        time.sleep(10)

        # --- IPERF_ADDITION START ---
        if workload_type == "iperf":
            print("\nVerifying iperf3 binary on a clone...")
            version = test_clone.vm_cmd("iperf3 --version 2>&1 | head -1").strip()
            if version and "iperf" in version.lower():
                print("  Clone %s: iperf3 binary OK (%s)" % (test_clone_name, version))
            else:
                print("  Clone %s: iperf3 binary NOT FOUND" % test_clone_name)
                cvm.vmOff(test_clone)
                print("\nSetup FAILED: iperf3 not available on clones. Fix and re-run.")
                sys.exit(1)
        # --- IPERF_ADDITION END ---
        else:
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
    print("\nRun overheadTest.py to start the experiment.")


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

    if config_file == '' or host_name == '':
        print("Config file and host name are required.")
        print(helpmsg)
        sys.exit(2)

    if cvm_ip == '':
        print("No CVM IP provided, assuming running on CVM.")

    run()


if __name__ == "__main__":
    main(sys.argv[1:])
