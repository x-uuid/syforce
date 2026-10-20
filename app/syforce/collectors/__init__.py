"""Platform collectors. Pick one with get_collector(); each implements the BaseCollector interface (common.py)."""
import sys


def get_collector(cfg):
    if sys.platform.startswith("linux"):
        from .linux import LinuxCollector as C
    elif sys.platform == "darwin":
        from .macos import MacCollector as C
    elif sys.platform.startswith("win"):
        from .windows import WindowsCollector as C
    else:
        from .common import BaseCollector as C
    return C(cfg)
