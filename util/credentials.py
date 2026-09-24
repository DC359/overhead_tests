"""
Credential helpers for overhead-tests.

VM password is collected once at setup time (or via VM_PASSWORD env).
Experiment runs should use SSH keys after setup and must not prompt mid-run.
"""

import os
import sys
import getpass

_cache = {}


def prompt_vm_password_once():
    """
    Call at the start of setupVms only.

    Uses VM_PASSWORD if set; otherwise prompts once (hidden) and caches it.
    Typing is not echoed — that is normal; press Enter when done.

    Does not validate the password here (guest is not up yet). Wrong
    passwords fail later when setup SSHs into the base VM.
    """
    if "vm" in _cache:
        return _cache["vm"]
    password = os.environ.get("VM_PASSWORD")
    if password:
        print("Using VM_PASSWORD from environment.")
    else:
        # getpass hides keystrokes (no * or echo). Flush so the prompt shows
        # immediately even if stdout is block-buffered.
        sys.stdout.flush()
        password = getpass.getpass("Enter VM password (input hidden): ")
        print("Password entered.")
    sys.stdout.flush()
    _cache["vm"] = password
    return password


def get_vm_password():
    """
    Return cached/env VM password for SSH/SCP during setup.

    Does not prompt. If missing, raises — so experiment runs never block
    mid-run waiting for a password.
    """
    if "vm" in _cache:
        return _cache["vm"]
    password = os.environ.get("VM_PASSWORD")
    if password:
        _cache["vm"] = password
        return password
    raise RuntimeError(
        "VM password not set. Run setupVms.py (it asks once at the start) "
        "or export VM_PASSWORD before setup."
    )


def clear_cache():
    """Clear cached credentials (useful for tests)."""
    _cache.clear()
