"""Windows collector — STUB. Works out of the box for the basics (psutil), but needs adapting for full fidelity.

What works now (psutil only, no admin):
  CPU / per-core / memory / disks / NIC rates / volumes / top processes (CPU, RSS, I/O) / connection list
  GPU via nvidia-smi when an NVIDIA driver is installed / default gateway via `route print` / ping / tracert

TODO (for the agent adapting this kit — keep everything read-only and cheap, <=3 % of one core):
  TODO 1  Per-socket byte counters: psutil gives no rx/tx per connection. Options:
          - GetPerTcpConnectionEStats (iphlpapi, needs admin to *enable* EStats collection), or
          - ETW Microsoft-Windows-Kernel-Network provider (admin), or
          - per-process totals from PDH "\\Process(*)\\IO Other Bytes/sec" as an approximation.
          Until then hosts are shown with connection counts only and particles stay idle on host links.
  TODO 2  GPU utilisation for non-NVIDIA GPUs: PDH counters "\\GPU Engine(*)\\Utilization Percentage"
          (typeperf / win32pdh from pywin32), sum per engine type "3D".
  TODO 3  Memory pressure: use "\\Memory\\Available MBytes" and commit charge ("\\Memory\\% Committed Bytes In Use").
  TODO 4  Loadavg does not exist on Windows; psutil.getloadavg() emulates it (first values are 0 for ~5 s).
  TODO 5  Wi-Fi detection: `netsh wlan show interfaces` -> interface Name, map to psutil NIC names.
  TODO 6  Service: run with Task Scheduler ("At log on", "Run whether user is logged on or not") or NSSM; see service/.
"""
import re, subprocess, time
import psutil
from .common import BaseCollector
from ..util import run, MB


class WindowsCollector(BaseCollector):
    os_name = "windows"

    def platform_label(self):
        import platform
        return "Windows %s · %s" % (platform.release(), platform.machine())

    def nic_kind(self, n):
        ln = n.lower()
        if "loopback" in ln: return "lo"
        if "tailscale" in ln: return "tail"
        if "wi-fi" in ln or "wireless" in ln or "wlan" in ln: return "wifi"
        if "vethernet" in ln or "hyper-v" in ln or "virtualbox" in ln or "vmware" in ln or "wsl" in ln: return "virtual"
        if "ethernet" in ln or ln.startswith("eth"): return "wired"
        return "other"

    def gpu_sample(self):
        return self.nvidia_smi()          # TODO 2: PDH GPU Engine counters for AMD / Intel

    def default_gateway(self):
        out = run(["route", "print", "-4", "0.0.0.0"], timeout=4)
        for line in out.splitlines():
            sp = line.split()
            if len(sp) >= 5 and sp[0] == "0.0.0.0" and sp[1] == "0.0.0.0" and re.match(r"^\d+\.\d+\.\d+\.\d+$", sp[2]):
                return sp[2]
        return ""

    def traceroute(self, target, hops):
        out = run(["tracert", "-d", "-h", str(hops), "-w", "500", target], timeout=4 + hops * 3)
        ips = []
        for line in out.splitlines():
            m = re.match(r"^\s*\d+\s+.*?(\d+\.\d+\.\d+\.\d+)\s*$", line)
            if m and m.group(1) not in ips: ips.append(m.group(1))
        return ips[:hops]

    def ping(self, ip, timeout=2.0):
        out = run(["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip], timeout=timeout + 2)
        m = re.search(r"[=<]\s*(\d+)\s*ms", out)
        return float(m.group(1)) if m else None
