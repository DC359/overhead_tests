from libx.lib import *
from util.vm import *
from util.host import *
import json
import sys
import math
import re


class Cvm:
    def __init__(self, cvmip):
        self._cvmip = cvmip
        self._hostDic = {}
        self._vmDic = {}
        self.initCvmObjects()

    def initCvmObjects(self):
        hosts = self.getHostDetails()
        for host in hosts:
            self._hostDic[host['Name']] = Host(host, self)

        vms = self.getVmDetails()
        for vm in vms:
            if vm['Name'].find('CVM') == -1:
                self._vmDic[vm['Name']] = Vm(vm['Name'], self)

    def getVm(self, name):
        if not name in self._vmDic:
            print("requested vm does not exist %s" % name)
            sys.exit(1)
        return self._vmDic[name]

    def getHost(self, name):
        if not name in self._hostDic:
            print("requested host does not exist %s" % name)
            sys.exit(1)
        return self._hostDic[name]

    def getHosts(self):
        return self._hostDic.values()

    def cvm_cmd(self, cmd, quiet=False):
        out = ''
        try:
            if self._cvmip == '':
                out = shell_run(cmd)
            else:
                out = run_remote_cmd(self._cvmip, 'nutanix', cmd)
            if isinstance(out, bytes):
                out = out.decode()
            return out
        except Exception as e:
            # `quiet` suppresses benign failures from best-effort commands
            # (e.g. cleanup pkills / kill -9 of an already-exited pid), which
            # otherwise spam the log with harmless SSH exit-255 / exit-1 lines.
            if not quiet:
                print(e)

    def getVmDetail(self, vmname):
        cmd = "ncli virtualmachine ls name=%s" % vmname
        vm = self.cvm_cmd(cmd)
        results = self.parseDetails(vm, 'Consistency Group')
        if not results:
            return {}
        if len(results) > 1:
            # Duplicate names (e.g. failed re-create). Prefer the real VM
            # (has disks / IPs) over an empty shell.
            def _score(v):
                try:
                    disks = int(str(v.get('VDisk Count', '0') or '0').replace(',', ''))
                except ValueError:
                    disks = 0
                ips = 1 if (v.get('VM IP Addresses') or '').strip() else 0
                return (disks, ips)

            results = sorted(results, key=_score, reverse=True)
            print("WARNING: %d VMs named '%s'; using uuid %s (delete duplicates)." % (
                len(results), vmname, results[0].get('Uuid', '?')))
        return results[0]

    def getVmDetails(self):
        cmd = "ncli virtualmachine ls"
        vms = self.cvm_cmd(cmd)
        vms = self.parseDetails(vms, 'Consistency Group')
        return vms

    def getHostDetails(self):
        cmd = "ncli host ls"
        hosts = self.cvm_cmd(cmd)
        hosts = self.parseDetails(hosts, 'Block Serial (Model)')
        return hosts

    def parseDetails(self, details, sep):
        res = []
        dic = {}
        lines = details.split("\n")
        for line in lines:
            words = line.split(':')
            if len(words) < 2:
                continue
            left = words[0].strip()
            right = words[1].strip()
            dic[left] = right
            if left == sep:
                res.append(dic)
                dic = {}
        return res

    def vmOff(self, vm):
        try:
            cmd = "acli vm.off %s" % vm.getUuid()
            self.cvm_cmd(cmd)
        except Exception as e:
            print(e)

    def vmOn(self, vm):
        try:
            cmd = "acli vm.on %s" % vm.getUuid()
            self.cvm_cmd(cmd)
        except Exception as e:
            raise RuntimeError("Failed to power on VM '%s': %s" % (vm.getVmName(), e)) from e

    def vmUpdate(self, vm, memory, cpu):
        try:
            cmd = "acli vm.update %s num_vcpus=%s memory=%sG" % (vm.getUuid(), cpu, memory)
            self.cvm_cmd(cmd)
        except Exception as e:
            print(e)

    def vmCreate(self, name, memory):
        # cvm_cmd swallows acli failures — check for existing name first so we
        # never create a second empty VM and keep going.
        existing = [v for v in (self.getVmDetails() or []) if v.get('Name') == name]
        if existing:
            uuids = ', '.join(v.get('Uuid', '?') for v in existing)
            raise RuntimeError(
                "VM '%s' already exists (%d): %s. "
                "Delete it (acli vm.delete <uuid> confirm=true) or use --skip-setup."
                % (name, len(existing), uuids))

        cmd = "acli vm.create %s memory=%sG num_cores_per_vcpu=2" % (name, memory)
        try:
            # Raise on non-zero so a failed create cannot be ignored.
            if self._cvmip == '':
                out = shell_run(cmd)
            else:
                out = run_remote_cmd(self._cvmip, 'nutanix', cmd)
            if isinstance(out, bytes):
                out = out.decode()
        except Exception as e:
            raise RuntimeError("Failed to create VM '%s': %s" % (name, e)) from e

        self._vmDic[name] = Vm(name, self)
        return self._vmDic[name]

    def ensureImage(self, image_name="img"):
        out = self.cvm_cmd("acli image.list")
        out = out if isinstance(out, str) else out.decode()
        if image_name in out:
            print("Image '%s' already exists, skipping creation." % image_name)
            return
        url = "http://endor.dyn.nutanix.com/acro_images/automation/ahv_guest_os/DSK/debian-12.11-x86_64_vdisk.qcow2"
        cmd = "acli image.create %s source_url=%s container=SelfServiceContainer image_type=kDiskImage" % (image_name, url)
        print("Creating image '%s' (this may take a few minutes)..." % image_name)
        self.cvm_cmd(cmd)

    def ensureNetwork(self, net_name="netw"):
        out = self.cvm_cmd("acli net.list")
        out = out if isinstance(out, str) else out.decode()
        if net_name in out:
            print("Network '%s' already exists, skipping creation." % net_name)
            return
        cmd = "acli net.create %s vlan=0" % net_name
        print("Creating network '%s'..." % net_name)
        self.cvm_cmd(cmd)

    def vmDiskCreate(self, vm_name, image_name="img"):
        try:
            cmd = "acli vm.disk_create %s clone_from_image=%s" % (vm_name, image_name)
            self.cvm_cmd(cmd)
        except Exception as e:
            print(e)

    def vmNicCreate(self, vm_name, net_name="netw"):
        try:
            cmd = "acli vm.nic_create %s network=%s" % (vm_name, net_name)
            self.cvm_cmd(cmd)
        except Exception as e:
            print(e)

    def vmClone(self, source_vm_name, clone_name):
        try:
            cmd = "acli vm.clone %s clone_from_vm=%s" % (clone_name, source_vm_name)
            self.cvm_cmd(cmd)
            import time
            time.sleep(2)
            self._vmDic[clone_name] = Vm(clone_name, self)
            return self._vmDic[clone_name]
        except Exception as e:
            raise RuntimeError("Failed to clone VM '%s' -> '%s': %s" % (
                source_vm_name, clone_name, e)) from e

    def getMaxVms(self, host_name, vm_size_gb, buffer=5):
        try:
            host = self.getHost(host_name)
            host_ip = host.getHostIp()
            cmd = "acli host.get %s" % host_ip
            out = self.cvm_cmd(cmd)
            lines = out.split("\n") if isinstance(out, str) else out.decode().split("\n")

            total_bytes = 0
            used_bytes = 0
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("memory_size_bytes") and total_bytes == 0:
                    total_bytes = int(re.findall(r'\d+', stripped)[0])
                elif stripped.startswith("mem_usage_bytes") and used_bytes == 0:
                    used_bytes = int(re.findall(r'\d+', stripped)[0])

            free_bytes = total_bytes - used_bytes
            free_gb = free_bytes / (1024.0 ** 3)
            total_gb = total_bytes / (1024.0 ** 3)
            used_gb = used_bytes / (1024.0 ** 3)
            vm_cost_gb = vm_size_gb + 0.075
            max_vms = int(math.floor(free_gb / vm_cost_gb))
            print("Host memory: total=%.1fGB used=%.1fGB free=%.1fGB | VM cost=%.2fGB | max_vms=%d (+%d buffer = %d)" %
                  (total_gb, used_gb, free_gb, vm_cost_gb, max_vms, buffer, max_vms + buffer))
            return max_vms + buffer
        except Exception as e:
            print(e)
            return 0

    def countVmsMatching(self, prefix):
        """Count clone VMs for this config prefix.

        Matches names exactly equal to prefix, or starting with 'prefix_'
        (e.g. redis_vm_1). Does NOT match a differently named base that only
        shares a string prefix (e.g. clone_prefix 'dirty_harry' must not
        count 'dirty_harry_base').
        """
        try:
            vms = self.getVmDetails()
        except Exception as e:
            print(e)
            return 0
        count = 0
        for vm in vms:
            name = vm.get("Name", "")
            if name == prefix or name.startswith(prefix + "_"):
                count += 1
        return count

    def listVmsNamed(self, name):
        """Return ncli detail dicts for VMs with this exact name."""
        try:
            vms = self.getVmDetails()
        except Exception as e:
            print(e)
            return []
        return [vm for vm in (vms or []) if vm.get("Name") == name]

    def countVmsNamed(self, name):
        """Count VMs with this exact name (detects duplicate base VMs)."""
        return len(self.listVmsNamed(name))

    def deleteVmsNamed(self, name):
        """Power off and delete every VM with this exact name (by UUID)."""
        matched = self.listVmsNamed(name)
        if not matched:
            print("  No VMs named '%s' to delete." % name)
            return 0
        for vm in matched:
            uuid = vm.get("Uuid") or ""
            if not uuid:
                continue
            print("  Deleting %s (%s)..." % (name, uuid))
            self.cvm_cmd("acli vm.off %s" % uuid, quiet=True)
            out = self.cvm_cmd("acli vm.delete %s confirm=true" % uuid)
            if out is None:
                print("    WARNING: delete may have failed for %s" % uuid)
            # Drop from in-memory map if present
            if name in self._vmDic and self._vmDic[name].getUuid() == uuid:
                del self._vmDic[name]
        return len(matched)

    def deleteVmsMatchingPrefix(self, prefix):
        """Power off and delete clone VMs for this prefix (prefix / prefix_*)."""
        n = self.countVmsMatching(prefix)
        if n == 0:
            print("  No VMs matching '%s' / '%s_*' to delete." % (prefix, prefix))
            return 0
        print("  Deleting %d clone VM(s) matching '%s' / '%s_*'..." % (n, prefix, prefix))
        # Use prefix_* so we do not also delete base_vm_name like dirty_harry_base
        # when clone_prefix is dirty_harry.
        self.cvm_cmd("acli vm.off %s_*" % prefix, quiet=True)
        self.cvm_cmd("acli vm.delete %s_* confirm=true" % prefix)
        # Rare: a VM named exactly the prefix
        self.cvm_cmd("acli vm.off %s" % prefix, quiet=True)
        self.cvm_cmd("acli vm.delete %s confirm=true" % prefix, quiet=True)
        for name in list(self._vmDic.keys()):
            if name == prefix or name.startswith(prefix + "_"):
                del self._vmDic[name]
        return n

    def countPoweredOnMatching(self, prefix):
        """Count powered-on clone VMs for this config prefix.
        Uses acli (reliable for power state) instead of ncli."""
        return len(self.listPoweredOnMatching(prefix))

    def listPoweredOnMatching(self, prefix):
        """Return names of powered-on clone VMs for this config prefix.
        Matches exact prefix or 'prefix_*'; excludes off/buffer VMs."""
        try:
            cmd = "acli vm.list power_state=on"
            out = self.cvm_cmd(cmd)
            if not out:
                return []
            lines = out.split("\n") if isinstance(out, str) else out.decode().split("\n")
            names = []
            for line in lines:
                name = line.strip().split()[0] if line.strip() else ""
                if name == prefix or name.startswith(prefix + "_"):
                    names.append(name)
            return names
        except Exception as e:
            print(e)
            return []

    def vmOnAll(self, pattern):
        try:
            cmd = "acli vm.on %s" % pattern
            return self.cvm_cmd(cmd)
        except Exception as e:
            print(e)

    def countPoweredOnVms(self):
        try:
            cmd = "acli vm.list power_state=on"
            out = self.cvm_cmd(cmd)
            lines = out.split("\n") if isinstance(out, str) else out.decode().split("\n")
            count = sum(1 for line in lines if line.strip())
            return count
        except Exception as e:
            print(e)
            return 0

    def vmOffAll(self, pattern):
        try:
            cmd = "acli vm.off %s" % pattern
            return self.cvm_cmd(cmd)
        except Exception as e:
            print(e)

    def vmDeleteAll(self, pattern):
        try:
            cmd = "acli vm.delete %s" % pattern
            return self.cvm_cmd(cmd)
        except Exception as e:
            print(e)
