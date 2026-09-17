"""
Retry helper for flaky network operations (SCP, etc.).
"""

import time
import functools


def retry(max_attempts=3, delay=2, exceptions=(Exception,)):
    """Decorator: retry a function on transient failures."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_error = e
                    if attempt < max_attempts:
                        print("  Retry %d/%d after error: %s" % (attempt, max_attempts, e))
                        time.sleep(delay)
            raise last_error
        return wrapper
    return decorator


def retry_call(func, args=(), kwargs=None, max_attempts=3, delay=2, exceptions=(Exception,)):
    """Call a function with retry logic (non-decorator form)."""
    if kwargs is None:
        kwargs = {}
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func(*args, **kwargs)
        except exceptions as e:
            last_error = e
            if attempt < max_attempts:
                print("  Retry %d/%d after error: %s" % (attempt, max_attempts, e))
                time.sleep(delay)
    raise last_error
