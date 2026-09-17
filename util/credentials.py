"""
Credential helpers for overhead-tests.

Passwords are:
- Taken from environment variables when set
- Otherwise prompted once per process (hidden input)
- Cached in memory for the rest of the run
- Never stored in source or on disk
"""

import os
import getpass

_cache = {}


def get_vm_password():
    """
    Guest VM SSH password.

    Env: VM_PASSWORD
    Prompted only when password auth is needed (setup / guest checks).
    """
    if "vm" not in _cache:
        password = os.environ.get("VM_PASSWORD")
        if not password:
            password = getpass.getpass("Enter VM password: ")
        _cache["vm"] = password
    return _cache["vm"]


def clear_cache():
    """Clear cached credentials (useful for tests)."""
    _cache.clear()
