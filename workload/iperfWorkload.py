from libx.objtmpl import WorkloadTmpl

IPERF_DEB_BASE_URL = "http://uranus.corp.nutanix.com/~dhruv.choudhary/iperf"
IPERF_DEBS = [
    "libsctp1_1.0.19+dfsg-2_amd64.deb",
    "libiperf0_3.12-1+deb12u2_amd64.deb",
    "iperf3_3.12-1+deb12u2_amd64.deb",
]

INSTALL_SCRIPT = """
set -e
if command -v iperf3 >/dev/null 2>&1; then
    echo "IPERF_ALREADY_INSTALLED"
    iperf3 --version 2>&1 | head -1
    exit 0
fi
mkdir -p /tmp/iperf_install
cd /tmp/iperf_install
%(downloads)s
for deb in %(deb_names)s; do
    ar x "$deb" data.tar.xz
    tar xf data.tar.xz
    mv -f data.tar.xz data.tar.xz.done 2>/dev/null || true
done
cp -f usr/bin/iperf3 /usr/local/bin/iperf3
cp -f usr/lib/x86_64-linux-gnu/libiperf* /usr/lib/x86_64-linux-gnu/ 2>/dev/null || true
cp -f usr/lib/x86_64-linux-gnu/libsctp* /usr/lib/x86_64-linux-gnu/ 2>/dev/null || true
cp -f lib/x86_64-linux-gnu/libsctp* /usr/lib/x86_64-linux-gnu/ 2>/dev/null || true
/sbin/ldconfig
systemctl stop iperf3 2>/dev/null || true
systemctl disable iperf3 2>/dev/null || true
/usr/local/bin/iperf3 --version 2>&1 | head -1
echo "IPERF_INSTALL_OK"
"""


def _build_install_script():
    downloads = "\n".join(
        'wget -q %s/%s -O %s' % (IPERF_DEB_BASE_URL, deb, deb)
        for deb in IPERF_DEBS
    )
    deb_names = " ".join(IPERF_DEBS)
    return INSTALL_SCRIPT % {"downloads": downloads, "deb_names": deb_names}


class iperfWorkload(WorkloadTmpl):
    """Installs iperf3 on a base VM. No systemd service is created —
    iperf server/client roles are assigned at experiment runtime by iperfTest.py."""

    def setup(self, vm, config=None):
        self._vm = vm

        print("Installing iperf3...")
        out = self._vm.vm_cmd(_build_install_script()).strip()
        if "IPERF_ALREADY_INSTALLED" in out:
            print("iperf3 already installed.")
        elif "IPERF_INSTALL_OK" in out:
            print("iperf3 installed successfully.")
        else:
            raise RuntimeError("iperf3 installation failed. Output:\n%s" % out)

        if "iperf" in out.lower():
            for line in out.split("\n"):
                if "iperf" in line.lower() and "version" not in line.lower():
                    continue
                if "iperf" in line.lower():
                    print("iperf3 version: %s" % line.strip())
                    break

    def runx(self, config=None):
        pass

    def close(self):
        pass
