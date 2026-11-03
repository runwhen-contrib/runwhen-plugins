"""The two clock functions the waits use, in one place so tests can replace them."""

import time


def monotonic():
    return time.monotonic()


def sleep(seconds):
    time.sleep(seconds)
