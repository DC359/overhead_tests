from libx.objtmpl import WorkloadTmpl
from libx.lib import scp_add_file
import time
import os


class fioWorkload(WorkloadTmpl):

    DEFAULT_CONFIG = {
        "rw": "randread",
        "bs": "4k",
        "ioengine": "io_uring",
        "iodepth": 128,
        "numjobs": 8,
        "size": "512M",
        "direct": 1,
    }

    def setup(self, vm, config=None):
        self._vm = vm
        self._config = config if config else self.DEFAULT_CONFIG

        out = self._vm.vm_cmd("which fio 2>/dev/null || echo NOT_FOUND").strip()
        if "NOT_FOUND" in out:
            print("fio not found, installing...")
            self._vm.vm_cmd("apt update && apt install -y fio")
            out = self._vm.vm_cmd("which fio 2>/dev/null || echo NOT_FOUND").strip()
            if "NOT_FOUND" in out:
                raise RuntimeError("Failed to install fio")

        version = self._vm.vm_cmd("fio --version").strip()
        print("fio installed: %s" % version)

        fio_cmd = "fio --name=test --rw=%s --bs=%s --ioengine=%s --iodepth=%d --numjobs=%d --size=%s --direct=%d --time_based --runtime=3600" % (
            self._config["rw"],
            self._config["bs"],
            self._config["ioengine"],
            self._config["iodepth"],
            self._config["numjobs"],
            self._config["size"],
            self._config["direct"],
        )

        svc_content = (
            "[Unit]\n"
            "Description=FIO Workload\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "ExecStart=%s\n"
            "Restart=always\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        ) % fio_cmd

        local_path = "/tmp/fio-workload.service"
        with open(local_path, "w") as f:
            f.write(svc_content)

        vm_ip = self._vm.getIp()
        scp_add_file(local_path, vm_ip, "root", "etc/systemd/system/fio-workload.service", use_password=True)
        os.remove(local_path)

        self._vm.vm_cmd("systemctl daemon-reload")
        self._vm.vm_cmd("systemctl enable fio-workload")

    def runx(self, config=None):
        self._vm.vm_cmd("systemctl start fio-workload")
        print("Waiting for fio workload to start...")
        time.sleep(30)

    def close(self):
        self._vm.vm_cmd("systemctl stop fio-workload")
