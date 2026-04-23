from libx.lib import *
import time


class Vm:
    def __init__(self, vmname, cvm):
        self._vmname = vmname
        self._cvm = cvm
        self._lastquery = self._cvm.getVmDetail(self._vmname)

    def getIp(self):
        while True:
            ip = self._lastquery['VM IP Addresses'].split(',')[0]
            if not (ip == ''):
                break
            time.sleep(1)
            self._lastquery = self._cvm.getVmDetail(self._vmname)
        return self._lastquery['VM IP Addresses'].split(',')[0]

    def getHostId(self):
        self._lastquery = self._cvm.getVmDetail(self._vmname)
        return self._lastquery['Hypervisor Host Name']

    def getUuid(self):
        if self._lastquery['Uuid'] == '':
            self._lastquery = self._cvm.getVmDetail(self._vmname)
        return self._lastquery['Uuid']

    def vm_cmd(self, cmd):
        try:
            out = run_remote_cmd(self.getIp(), 'root', cmd, use_password=True)
            if isinstance(out, bytes):
                out = out.decode()
            return out
        except Exception as e:
            print(e)
            return ''

    def vm_cmd_non_blocking(self, cmd):
        try:
            return run_remote_cmd_non_blocking(self.getIp(), 'root', cmd, use_password=True)
        except Exception as e:
            print(e)

    def getVmName(self):
        return self._vmname

    def waitForReady(self):
        while True:
            out = self.vm_cmd("echo hello").strip()
            if out == 'hello':
                break
            time.sleep(2)

    def vmOff(self):
        try:
            self._cvm.vmOff(self)
        except Exception as e:
            print(e)

    def vmOn(self):
        try:
            self._cvm.vmOn(self)
            self.waitForReady()
        except Exception as e:
            print(e)

    def vmUpdate(self, memory, cpu):
        try:
            self._cvm.vmUpdate(self, memory, cpu)
        except Exception as e:
            print(e)
