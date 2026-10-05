"""
Collect AHV host / hypervisor metadata for run headers and CSV comments.
Best-effort: missing fields become empty strings, never fail the experiment.
"""


def _one_line(host, cmd):
    out = host.host_cmd(cmd, quiet=True)
    if out is None:
        return ""
    if isinstance(out, bytes):
        out = out.decode(errors="replace")
    out = out.strip()
    if not out:
        return ""
    return out.split("\n")[0].strip()


def _first_ok(host, cmds):
    for cmd in cmds:
        val = _one_line(host, cmd)
        if val and "not found" not in val.lower() and "no such" not in val.lower():
            return val
    return ""


def collect_host_metadata(host, cvm=None):
    """
    Return a dict of metadata strings for the AHV host.

    Queried via CVM -> host SSH (same path as the collector).
    """
    meta = {
        "host_name": host.getHostId(),
        "host_ip": host.getHostIp(),
        "num_cpus": _one_line(host, "nproc"),
        "kernel": _one_line(host, "uname -r"),
        "os": _first_ok(host, [
            "grep ^PRETTY_NAME= /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '\"'",
            "cat /etc/redhat-release 2>/dev/null",
        ]),
        "qemu_version": _first_ok(host, [
            "/usr/libexec/qemu-kvm --version 2>/dev/null | head -1",
            "qemu-kvm --version 2>/dev/null | head -1",
            "virsh version 2>/dev/null | head -1",
        ]),
        "ahv_version": _first_ok(host, [
            "cat /etc/nutanix/release_version 2>/dev/null",
            "cat /etc/ahv-release 2>/dev/null",
            "cat /etc/nutanix-release 2>/dev/null",
            "rpm -q --qf '%{NAME}-%{VERSION}-%{RELEASE}' ahv 2>/dev/null",
            "rpm -q --qf '%{NAME}-%{VERSION}-%{RELEASE}' nutanix-ahv 2>/dev/null",
        ]),
        "libvirt_version": _first_ok(host, [
            "virsh version --daemon 2>/dev/null | head -1",
            "rpm -q --qf '%{VERSION}-%{RELEASE}' libvirt 2>/dev/null",
        ]),
        "hostname_fqdn": _one_line(host, "hostname -f 2>/dev/null || hostname"),
    }

    # Optional: host UUID from acli if cvm handle provided
    if cvm is not None:
        try:
            meta["host_uuid"] = host.getHostUuid() or ""
        except Exception:
            meta["host_uuid"] = ""
    else:
        meta["host_uuid"] = ""

    return meta


def format_metadata_lines(meta):
    """Human-readable lines for terminal header."""
    order = [
        ("Host", "host_name"),
        ("Host IP", "host_ip"),
        ("Host UUID", "host_uuid"),
        ("Hostname", "hostname_fqdn"),
        ("CPUs", "num_cpus"),
        ("Kernel", "kernel"),
        ("OS", "os"),
        ("AHV / Nutanix", "ahv_version"),
        ("QEMU", "qemu_version"),
        ("Libvirt", "libvirt_version"),
    ]
    lines = []
    for label, key in order:
        val = (meta or {}).get(key, "")
        if val:
            lines.append("%s: %s" % (label, val))
    return lines


def metadata_csv_comments(meta):
    """List of '# Key: value' rows for CSV headers."""
    rows = []
    for key in sorted((meta or {}).keys()):
        val = meta.get(key, "")
        if val:
            rows.append("# %s: %s" % (key, val))
    return rows
