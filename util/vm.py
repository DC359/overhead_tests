from libx.lib import *
import time


class Vm:
    def __init__(self, vmname, cvm):
        self._vmname = vmname
        self._cvm = cvm
        self._lastquery = self._cvm.getVmDetail(self._vmname)
        self._cached_ip = None

    def _parse_ips(self):
        raw = self._lastquery.get('VM IP Addresses', '') or ''
        return [ip.strip() for ip in raw.split(',') if ip.strip()]

    @staticmethod
    def _ip_reachable(ip, wait_s=2):
        """True if ICMP echo gets a reply (guest may still reject SSH)."""
        try:
            shell_run("ping -c 1 -W %d %s >/dev/null 2>&1" % (wait_s, ip),
                      timeout=wait_s + 2)
            return True
        except Exception:
            return False

    def getIp(self, timeout_s=300):
        """
        Return a guest IP for SSH/SCP.

        ncli can list multiple addresses (stale or secondary). Prefer a
        previously working IP, then any ping-reachable address, then the
        first listed IP once the wait times out of the "no IP yet" phase.
        """
        if self._cached_ip:
            return self._cached_ip

        start = time.time()
        while True:
            ips = self._parse_ips()
            if ips:
                for ip in ips:
                    if self._ip_reachable(ip):
                        if len(ips) > 1:
                            print("  VM '%s' IPs %s — using reachable %s" % (
                                self._vmname, ips, ip))
                        self._cached_ip = ip
                        return ip
                # IPs present but none ping yet (guest still booting)
                if time.time() - start > timeout_s:
                    # Last resort: first listed (may still fail SSH)
                    print("  WARNING: no ping reply from %s; trying %s" % (
                        ips, ips[0]))
                    self._cached_ip = ips[0]
                    return ips[0]
            elif time.time() - start > timeout_s:
                raise TimeoutError(
                    "Timed out after %ds waiting for IP on VM '%s'" % (
                        timeout_s, self._vmname))
            time.sleep(1)
            self._lastquery = self._cvm.getVmDetail(self._vmname)

    def getHostId(self):
        self._lastquery = self._cvm.getVmDetail(self._vmname)
        return self._lastquery['Hypervisor Host Name']

    def getUuid(self):
        if self._lastquery.get('Uuid', '') == '':
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
        last_print = 0
        # Drop sticky IP so we re-evaluate if the first choice was wrong/stale.
        self._cached_ip = None
        while True:
            elapsed = time.time() - start
            if elapsed > timeout_s:
                raise TimeoutError(
                    "Timed out after %ds waiting for SSH on VM '%s'" % (timeout_s, self._vmname))
            try:
                out = self.vm_cmd("echo hello", timeout=30).strip()
                if out == 'hello':
                    print("  VM '%s' SSH ready on %s (%.0fs)." % (
                        self._vmname, self.getIp(), elapsed))
                    return
            except Exception as e:
                msg = str(e)
                if "Permission denied" in msg or "exit status 5" in msg:
                    raise RuntimeError(
                        "SSH auth failed for VM '%s' (wrong VM password?). "
                        "Re-run setup with the correct password." % self._vmname) from e
                # Unreachable IP: clear cache and try other addresses next loop
                if ("No route to host" in msg or "Connection timed out" in msg
                        or "Network is unreachable" in msg):
                    self._cached_ip = None
                    self._lastquery = self._cvm.getVmDetail(self._vmname)
                if elapsed - last_print >= 30:
                    print("  Still waiting for SSH on '%s' (%.0fs)..." % (
                        self._vmname, elapsed))
                    last_print = elapsed
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
