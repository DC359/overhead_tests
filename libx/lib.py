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
