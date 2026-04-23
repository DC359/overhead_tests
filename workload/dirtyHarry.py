from libx.objtmpl import WorkloadTmpl
from libx.lib import scp_add_file
import time
import tempfile
import os

DIRTY_HARRY_URL = "http://uranus.corp.nutanix.com/~tejus.gk/harry"


class dirtyHarry(WorkloadTmpl):

    DEFAULT_CONFIG = {"n": 2, "m": 4}

    def setup(self, vm, config=None):
        self._vm = vm
        self._config = config if config else self.DEFAULT_CONFIG

        size = self._vm.vm_cmd("stat -c %s /root/harry 2>/dev/null || echo 0").strip()
        if not size.isdigit() or int(size) < 1000:
            print("harry binary not present or empty, downloading...")
            self._vm.vm_cmd("rm -f /root/harry")
            self._vm.vm_cmd("wget -q %s -O /root/harry" % DIRTY_HARRY_URL)
            self._vm.vm_cmd("chmod +x /root/harry")
            size = self._vm.vm_cmd("stat -c %s /root/harry 2>/dev/null || echo 0").strip()
            if not size.isdigit() or int(size) < 1000:
                raise RuntimeError("Failed to download harry binary (size=%s)" % size)

        n = self._config['n']
        m = self._config['m']

        svc_content = (
            "[Unit]\n"
            "Description=Dirty Harry Memory Workload\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "ExecStart=/root/harry -n %d -m %d\n"
            "Restart=always\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        ) % (n, m)

        local_path = "/tmp/dirty-harry.service"
        with open(local_path, "w") as f:
            f.write(svc_content)

        vm_ip = self._vm.getIp()
        scp_add_file(local_path, vm_ip, "root", "etc/systemd/system/dirty-harry.service", use_password=True)
        os.remove(local_path)

        self._vm.vm_cmd("systemctl daemon-reload")
        self._vm.vm_cmd("systemctl enable dirty-harry")

    def runx(self, config=None):
        conf = config if config else self._config
        self._vm.vm_cmd("systemctl start dirty-harry")

        setup_time = int(1.3 * conf['m'])
        wait_time = max(120, setup_time)
        print("Waiting for dirty harry initialisation for %d secs" % wait_time)
        time.sleep(wait_time)

    def close(self):
        self._vm.vm_cmd("systemctl stop dirty-harry")
