from libx.objtmpl import WorkloadTmpl
from libx.lib import scp_add_file
import time
import os

# Prebuilt redis binaries hosted on the internal mirror (same pattern as
# dirtyHarry / iperf — the base VMs have no usable apt repo, only this mirror).
# Build them once on a Debian 12 box with:  make MALLOC=libc -j
# (MALLOC=libc + bundled lua/hiredis => no external lib deps, just glibc), then
# upload src/redis-server and src/redis-benchmark to this URL.
REDIS_BIN_BASE_URL = "http://uranus.corp.nutanix.com/~dhruv.choudhary/redis"
REDIS_BINS = ["redis-server", "redis-benchmark"]


class redisWorkload(WorkloadTmpl):
    """Realistic in-memory KV workload, fully self-contained on a single VM.

    redis-server and a continuous redis-benchmark load loop both run inside the
    same VM against 127.0.0.1 — no client VM, no cross-VM networking. The high
    rate of small request/response ops is what surfaces as host-side overhead
    (virtio-net, syscalls, VM-exits)."""

    DEFAULT_CONFIG = {
        "clients": 50,
        "requests": 1000000,
        "data_size": 64,
        "pipeline": 1,
        "threads": 4,
        "keyspace": 100000,
        "tests": "set,get",
    }

    def setup(self, vm, config=None):
        self._vm = vm
        self._config = dict(self.DEFAULT_CONFIG, **(config or {}))
        base_url = self._config.get("bin_base_url", REDIS_BIN_BASE_URL)

        for binname in REDIS_BINS:
            dst = "/usr/local/bin/%s" % binname
            size = self._vm.vm_cmd("stat -c %%s %s 2>/dev/null || echo 0" % dst).strip()
            if not size.isdigit() or int(size) < 1000:
                print("%s not present, downloading..." % binname)
                self._vm.vm_cmd("rm -f %s" % dst)
                self._vm.vm_cmd("wget -q %s/%s -O %s" % (base_url, binname, dst))
                self._vm.vm_cmd("chmod +x %s" % dst)
                size = self._vm.vm_cmd("stat -c %%s %s 2>/dev/null || echo 0" % dst).strip()
                if not size.isdigit() or int(size) < 1000:
                    raise RuntimeError("Failed to download %s (size=%s)" % (binname, size))

        version = self._vm.vm_cmd("/usr/local/bin/redis-server --version").strip()
        print("redis installed: %s" % version)

        bench_cmd = (
            "/usr/local/bin/redis-benchmark -h 127.0.0.1 -q -t %s -c %d -n %d -d %d -P %d --threads %d -r %d" % (
                self._config["tests"],
                self._config["clients"],
                self._config["requests"],
                self._config["data_size"],
                self._config["pipeline"],
                self._config["threads"],
                self._config["keyspace"],
            )
        )

        # One self-contained service: run redis-server as a background child
        # (NOT pkill/daemonize — pkill -f would match this bash cmdline and kill
        # itself), then loop the benchmark forever in the foreground so the unit
        # stays active. On stop/restart systemd kills the whole cgroup, so the
        # backgrounded redis-server is cleaned up too.
        start_server = (
            "/usr/local/bin/redis-server --bind 127.0.0.1 --port 6379 "
            "--protected-mode no --save \"\" --appendonly no --daemonize no &"
        )
        exec_start = (
            "/bin/bash -c '%s sleep 3; while true; do %s > /dev/null 2>&1; sleep 1; done'" % (
                start_server, bench_cmd))

        svc_content = (
            "[Unit]\n"
            "Description=Redis Benchmark Workload\n"
            "After=network.target\n"
            "StartLimitIntervalSec=0\n"
            "\n"
            "[Service]\n"
            "ExecStart=%s\n"
            "Restart=always\n"
            "RestartSec=2\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        ) % exec_start

        local_path = "/tmp/redis-workload.service"
        with open(local_path, "w") as f:
            f.write(svc_content)

        vm_ip = self._vm.getIp()
        scp_add_file(local_path, vm_ip, "root", "etc/systemd/system/redis-workload.service", use_password=True)
        os.remove(local_path)

        self._vm.vm_cmd("systemctl daemon-reload")
        self._vm.vm_cmd("systemctl enable redis-workload")

    def runx(self, config=None):
        self._vm.vm_cmd("systemctl start redis-workload")
        print("Waiting for redis workload to start...")
        time.sleep(30)

    def close(self):
        self._vm.vm_cmd("systemctl stop redis-workload")
