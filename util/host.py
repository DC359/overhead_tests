from libx.lib import *


class Host:
    def __init__(self, hostdetails, cvm):
        self._hostdetails = hostdetails
        self._cvm = cvm

    def getHostId(self):
        return self._hostdetails['Name']

    def getHostIp(self):
        return self._hostdetails['Hypervisor Address']

    # --- IPERF_ADDITION START ---
    def getHostUuid(self):
        """Get the host UUID needed for vm.affinity_set.
        Looks up via 'acli host.list' since ncli host ls uses a different ID format."""
        out = self._cvm.cvm_cmd("acli host.list")
        if isinstance(out, bytes):
            out = out.decode()
        host_ip = self.getHostIp()
        for line in out.split("\n"):
            if host_ip in line:
                return line.split()[0].strip()
        raw_id = self._hostdetails.get('Id', '')
        if '::' in raw_id:
            return raw_id.split('::')[1]
        return raw_id
    # --- IPERF_ADDITION END ---

    def host_cmd(self, cmd):
        try:
            nested_cmd = "ssh root@%s %s" % (self.getHostIp(), cmd)
            return self._cvm.cvm_cmd(nested_cmd)
        except Exception as e:
            print(e)
