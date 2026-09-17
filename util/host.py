from libx.lib import *
import os
import atexit


class Host:
    def __init__(self, hostdetails, cvm):
        self._hostdetails = hostdetails
        self._cvm = cvm
        self._control_socket = "/tmp/ssh_ctrl_%s" % self.getHostIp().replace(".", "_")
        self._control_active = False

    def getHostId(self):
        return self._hostdetails['Name']

    def getHostIp(self):
        return self._hostdetails['Hypervisor Address']

    def getHostUuid(self):
        """Host UUID for affinity / metadata (prefer ncli Id, then acli)."""
        raw_id = self._hostdetails.get("Id", "") or ""
        if "::" in raw_id:
            return raw_id.split("::")[1]
        # Avoid returning the hypervisor IP as a fake UUID
        host_ip = self.getHostIp()
        if raw_id and raw_id != host_ip and not raw_id.replace(".", "").isdigit():
            return raw_id
        out = self._cvm.cvm_cmd("acli host.list", quiet=True)
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if out:
            import re
            for line in out.split("\n"):
                if host_ip not in line:
                    continue
                # Prefer a UUID-shaped token on the matching row
                for tok in line.split():
                    if re.match(
                            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", tok):
                        return tok
        return raw_id

    def _ssh_ctrl_opts(self):
        return "-o ControlPath=%s" % self._control_socket

    def open_control(self):
        """Open a persistent SSH ControlMaster connection to the host."""
        if self._control_active:
            return
        ctrl_cmd = ("ssh -o StrictHostKeyChecking=no -o ControlMaster=yes "
                    "-o ControlPath=%s -o ControlPersist=yes "
                    "-fN root@%s" % (self._control_socket, self.getHostIp()))
        try:
            self._cvm.cvm_cmd(ctrl_cmd)
            self._control_active = True
            atexit.register(self.close_control)
            print("SSH ControlMaster opened to %s" % self.getHostIp())
        except Exception as e:
            print("ControlMaster failed, falling back to regular SSH: %s" % e)

    def close_control(self):
        """Close the persistent SSH connection."""
        if not self._control_active:
            return
        try:
            close_cmd = ("ssh -o ControlPath=%s -O exit root@%s"
                         % (self._control_socket, self.getHostIp()))
            self._cvm.cvm_cmd(close_cmd)
        except Exception:
            pass
        self._control_active = False

    def host_cmd(self, cmd, quiet=False):
        try:
            if self._control_active:
                nested_cmd = "ssh -o ControlPath=%s root@%s %s" % (
                    self._control_socket, self.getHostIp(), cmd)
            else:
                nested_cmd = "ssh root@%s %s" % (self.getHostIp(), cmd)
            return self._cvm.cvm_cmd(nested_cmd, quiet=quiet)
        except Exception as e:
            if not quiet:
                print(e)
