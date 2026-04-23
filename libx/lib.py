import subprocess

VM_PASSWORD = "nutanix/4u"
SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

def shell_run(cmd):
    return subprocess.check_output(cmd, shell=True)

def shell_run_non_blocking(cmd):
    return subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def run_remote_cmd(host_ip, user, cmd, use_password=False):
    if use_password:
        full_cmd = "sshpass -p '%s' ssh %s %s@%s '%s'" % (VM_PASSWORD, SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    else:
        full_cmd = "ssh %s %s@%s '%s'" % (SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    return shell_run(full_cmd)

def run_remote_cmd_non_blocking(host_ip, user, cmd, use_password=False):
    if use_password:
        full_cmd = "sshpass -p '%s' ssh %s %s@%s '%s'" % (VM_PASSWORD, SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    else:
        full_cmd = "ssh %s %s@%s '%s'" % (SSH_OPTS, user, host_ip, cmd.replace("'", "'\\''"))
    return shell_run_non_blocking(full_cmd)

def scp_add_file(srcpath, destHost, user, destpath, use_password=False):
    if use_password:
        full_cmd = "sshpass -p '%s' scp %s %s %s@%s:/%s" % (VM_PASSWORD, SSH_OPTS, srcpath, user, destHost, destpath)
    else:
        full_cmd = "scp %s %s %s@%s:/%s" % (SSH_OPTS, srcpath, user, destHost, destpath)
    return shell_run(full_cmd)
