import re
import subprocess
from util.credentials import get_vm_password

SSH_OPTS = (
    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
    "-o LogLevel=ERROR -o ConnectTimeout=10"
)


def _redact_cmd(cmd):
    """Hide sshpass passwords in error messages."""
    if "sshpass -p" in cmd:
        return re.sub(r"sshpass -p '[^']*'", "sshpass -p '***'", cmd)
    return cmd


def shell_run(cmd, timeout=None):
    try:
        if timeout is None:
            return subprocess.check_output(cmd, shell=True)
        return subprocess.check_output(cmd, shell=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError("Command timed out after %ss: %s" % (
            timeout, _redact_cmd(cmd)[:120])) from e
    except subprocess.CalledProcessError as e:
        raise subprocess.CalledProcessError(
            e.returncode, _redact_cmd(e.cmd), e.output, e.stderr) from None


def shell_run_non_blocking(cmd):
    return subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_remote_cmd(host_ip, user, cmd, use_password=False, timeout=30):
    if use_password:
        password = get_vm_password()
        full_cmd = "sshpass -p '%s' ssh %s %s@%s '%s'" % (
            password, SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    else:
        full_cmd = "ssh %s %s@%s '%s'" % (SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    return shell_run(full_cmd, timeout=timeout)


def run_remote_cmd_non_blocking(host_ip, user, cmd, use_password=False):
    if use_password:
        password = get_vm_password()
        full_cmd = "sshpass -p '%s' ssh %s %s@%s '%s'" % (
            password, SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    else:
        full_cmd = "ssh %s %s@%s '%s'" % (SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    return shell_run_non_blocking(full_cmd)


def scp_add_file(srcpath, destHost, user, destpath, use_password=False, timeout=60):
    if use_password:
        password = get_vm_password()
        full_cmd = "sshpass -p '%s' scp %s %s %s@%s:/%s" % (
            password, SSH_OPTS, srcpath, user, destHost, destpath)
    else:
        full_cmd = "scp %s %s %s@%s:/%s" % (SSH_OPTS, srcpath, user, destHost, destpath)
    return shell_run(full_cmd, timeout=timeout)


def local_ssh_pubkey():
    """Return contents of the first available local ~/.ssh/*.pub key."""
    import os
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        path = os.path.expanduser("~/.ssh/%s" % name)
        if os.path.isfile(path):
            with open(path) as f:
                key = f.read().strip()
            if key:
                return key, path
    raise RuntimeError(
        "No SSH public key found in ~/.ssh/ "
        "(expected id_ed25519.pub, id_rsa.pub, or id_ecdsa.pub). "
        "Generate one with: ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519")


def install_ssh_pubkey(host_ip, user="root", timeout=60):
    """
    Append the local public key to remote authorized_keys via password SSH.

    Avoids ssh-copy-id, which writes a temp file under ~/.ssh on the CVM and
    then tries to delete it — CVM file-protection blocks that delete and prints
    scary 'system files detected' warnings even though the key was installed.
    """
    pubkey, key_path = local_ssh_pubkey()
    # Single-quote for the remote shell (same escaping as run_remote_cmd).
    safe = pubkey.replace("'", "'\\''")
    remote = (
        "mkdir -p .ssh && chmod 700 .ssh && "
        "touch .ssh/authorized_keys && chmod 600 .ssh/authorized_keys && "
        "grep -qxF '%s' .ssh/authorized_keys || echo '%s' >> .ssh/authorized_keys"
        % (safe, safe)
    )
    run_remote_cmd(host_ip, user, remote, use_password=True, timeout=timeout)
    return key_path
