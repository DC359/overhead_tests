from libx.lib import *
import time


class Vm:
    def __init__(self, vmname, cvm):
        self._vmname = vmname
        self._cvm = cvm
        self._lastquery = self._cvm.getVmDetail(self._vmname)

    def getIp(self, timeout_s=300):
        start = time.time()
        while True:
            ip = self._lastquery.get('VM IP Addresses', '').split(',')[0].strip()
            if ip:
                return ip
            if time.time() - start > timeout_s:
                raise TimeoutError(
                    "Timed out after %ds waiting for IP on VM '%s'" % (timeout_s, self._vmname))
            time.sleep(1)
            self._lastquery = self._cvm.getVmDetail(self._vmname)

    def getHostId(self):
        self._lastquery = self._cvm.getVmDetail(self._vmname)
        return self._lastquery['Hypervisor Host Name']

    def getUuid(self):
        if self._lastquery['Uuid'] == '':
            self._lastquery = self._cvm.getVmDetail(self._vmname)
        return self._lastquery['Uuid']

    def vm_cmd(self, cmd, timeout=30):
        out = run_remote_cmd(self.getIp(), 'root', cmd, use_password=True, timeout=timeout)
        if isinstance(out, bytes):
            out = out.decode()
        return out

    def vm_cmd_non_blocking(self, cmd):
        try:
            return run_remote_cmd_non_blocking(self.getIp(), 'root', cmd, use_password=True)
        except Exception as e:
            print(e)

    def getVmName(self):
        return self._vmname

    def waitForReady(self, timeout_s=300):
        """Wait until guest SSH works. Fail fast on bad password; time out otherwise."""
        start = time.time()
        while True:
            if time.time() - start > timeout_s:
                raise TimeoutError(
                    "Timed out after %ds waiting for SSH on VM '%s'" % (timeout_s, self._vmname))
            try:
                out = self.vm_cmd("echo hello", timeout=30).strip()
                if out == 'hello':
                    return
            except Exception as e:
                msg = str(e)
                if "Permission denied" in msg or "exit status 5" in msg:
                    raise RuntimeError(
                        "SSH auth failed for VM '%s' (wrong VM password?). "
                        "Re-run setup with the correct password." % self._vmname) from e
                # IP/SSH not ready yet — keep waiting until timeout
            time.sleep(2)

    def vmOff(self):
        try:
            self._cvm.vmOff(self)
        except Exception as e:
            print(e)

    def vmOn(self):
        self._cvm.vmOn(self)
        self.waitForReady()

    def vmUpdate(self, memory, cpu):
        try:
            self._cvm.vmUpdate(self, memory, cpu)
        except Exception as e:
            print(e)
