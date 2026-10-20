"""Linux collector: /proc process sampler (all users), `ss -tinp` per-socket bytes, NVML GPU, PSI, big.LITTLE topology.

Caveats
- `ss` only reports the owning pid for sockets of the same user; root-owned sockets show as ":port" / "?"
  unless the service runs with sudo (not recommended) or CAP_NET_ADMIN.
- /proc/<pid>/io is only readable for your own processes; disk I/O per process for others is skipped.
- Unprivileged ICMP sockets depend on net.ipv4.ping_group_range; otherwise /bin/ping is used.
"""
import os, re, socket, time
import psutil
from .common import BaseCollector
from ..util import run, log, MB, split_hp

try:
    import pynvml as NV
except Exception:
    NV = None

CLK_TCK = os.sysconf("SC_CLK_TCK"); PAGE = os.sysconf("SC_PAGE_SIZE")


def nice_name(comm, cmd):
    """short display name: 'python3 app.py', 'uvicorn main' ..."""
    if comm.startswith("python") or comm in ("uvicorn", "gunicorn"):
        for a in cmd.split()[1:]:
            if a.startswith("-"): continue
            if ":" in a and not a.startswith("/"): return comm + " " + a.split(":")[0][:30]
            b = os.path.basename(a)
            if b and not b.startswith("python") and b != comm: return comm + " " + b[:30]
    return comm


class LinuxCollector(BaseCollector):
    os_name = "linux"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cpu_labels, self.cpu_kinds = self._topology()
        self._name_cache = {}; self._cg_cache = {}
        self._sprev = {}; self._sprev_t = None; self._sfirst = True
        self.gpu_h = None; self.gpu_name = "GPU"; self._gtick = 0; self.gpu_procs = []
        if NV is not None and str(cfg["gpu"].get("enabled")) != "false":
            try:
                NV.nvmlInit(); self.gpu_h = NV.nvmlDeviceGetHandleByIndex(0)
                n = NV.nvmlDeviceGetName(self.gpu_h); self.gpu_name = n.decode() if isinstance(n, bytes) else n
            except Exception as e:
                self.gpu_h = None; log("NVML unavailable (%r) -> nvidia-smi fallback" % e)

    def platform_label(self):
        model = ""
        for f in ("/sys/devices/virtual/dmi/id/product_name", "/proc/device-tree/model"):
            try: model = open(f).read().strip("\x00\n ")[:40]; break
            except Exception: pass
        base = "Linux · %s" % os.uname().machine
        return base + (" · " + model if model else "")

    # ---------- CPU topology: big.LITTLE via cpuinfo_max_freq / cpu_capacity ----------
    def _topology(self):
        caps = []
        for i in range(self.ncpu):
            v = None
            for f in ("/sys/devices/system/cpu/cpu%d/cpu_capacity" % i, "/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq" % i):
                try: v = int(open(f).read()); break
                except Exception: pass
            caps.append(v or 0)
        hi = max(caps) if caps else 0
        if not hi or min(caps) >= hi * 0.9:
            return ["P"] * self.ncpu, {"P": "CPU"}
        return ["P" if c >= hi * 0.9 else "E" for c in caps], {"P": "Performance", "E": "Efficiency"}

    def mem_extra(self, vm):
        psi = None
        try:
            m = re.search(r"avg10=([\d.]+)", open("/proc/pressure/memory").readline()); psi = float(m.group(1)) if m else None
        except Exception: pass
        p = 4 if (psi or 0) > 40 else 2 if (psi or 0) > 10 else 1
        if vm.percent > 95: p = max(p, 4)
        return {"psi": psi, "pressure": p}

    # ---------- processes via /proc (cheap, all users for CPU/RSS) ----------
    def proc_cmd(self, pid, comm=""):
        k = (pid, comm); v = self._name_cache.get(k)
        if v is None:
            v = ""
            try:
                raw = open("/proc/%d/cmdline" % pid, "rb").read(600).split(b"\0")
                v = " ".join(a.decode("utf-8", "replace") for a in raw if a)[:200]
            except Exception: pass
            if len(self._name_cache) > 6000: self._name_cache.clear()
            self._name_cache[k] = v
        return v

    def pid_container(self, pid):
        v = self._cg_cache.get(pid)
        if v is None:
            v = ""
            try:
                m = re.search(r"docker[-/]([0-9a-f]{64})", open("/proc/%d/cgroup" % pid).read())
                if m: v = m.group(1)
            except Exception: pass
            if len(self._cg_cache) > 5000: self._cg_cache.clear()
            self._cg_cache[pid] = v
        return v

    def sample_procs(self):
        t = time.time(); dt = (t - self._pprev_t) if self._pprev_t else None
        cur = {}; rows = []; io_denied = 0; by_ctr = {}; memrows = []
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        for pid in pids:
            try:
                with open("/proc/%d/stat" % pid, "rb") as f: st = f.read().decode("utf-8", "replace")
            except Exception: continue
            r = st.rfind(")"); comm = st[st.find("(") + 1:r]; f = st[r + 2:].split()
            try: cpu_t = (int(f[11]) + int(f[12])) / CLK_TCK; rss = int(f[21]) * PAGE
            except Exception: continue
            rd = wr = None
            try:
                with open("/proc/%d/io" % pid, "rb") as fio: io = fio.read().split(b"\n")
                rd = int(io[4].split()[1]); wr = int(io[5].split()[1])
            except Exception: io_denied += 1
            cur[pid] = (cpu_t, rd, wr, comm)
            if rss > 64 * MB: memrows.append((rss, pid, comm))
            pv = self._pprev.get(pid)
            if pv and dt and pv[3] == comm:
                c = max(0.0, cpu_t - pv[0]) / dt * 100
                r_ = (max(0, rd - pv[1]) / dt) if (rd is not None and pv[1] is not None) else 0
                w_ = (max(0, wr - pv[2]) / dt) if (wr is not None and pv[2] is not None) else 0
                ctr = self.pid_container(pid) if c > 0.3 or rss > 256 * MB else ""
                if ctr:
                    b = by_ctr.setdefault(ctr, {"cpu": 0.0, "rss": 0, "n": 0}); b["cpu"] += c; b["rss"] += rss; b["n"] += 1
                if c > 0.5 or r_ + w_ > 0: rows.append((pid, comm, c, r_, w_, rss, ctr))
        self._pprev, self._pprev_t = cur, t
        def item(pid, comm, ctr, **kw):
            cmd = self.proc_cmd(pid, comm)
            d = {"pid": pid, "name": nice_name(comm, cmd), "comm": comm, "cmd": cmd[:120], "ctr": ctr[:12] if ctr else ""}
            d.update(kw); return d
        cpu = sorted([item(p, cm, ctr, cpu=round(c, 1), mem=rss) for p, cm, c, r_, w_, rss, ctr in rows if c > 0.5], key=lambda x: -x["cpu"])
        disk = sorted([item(p, cm, ctr, r=r_, w=w_) for p, cm, c, r_, w_, rss, ctr in rows if r_ + w_ > 0], key=lambda x: -(x["r"] + x["w"]))
        memrows.sort(reverse=True)
        mem = [item(p, cm, self.pid_container(p), mem=rss) for rss, p, cm in memrows[:10]]
        with self.proc_lock:
            self.top_cpu = cpu[:12]; self.top_disk = disk[:8]; self.by_ctr = by_ctr; self.top_mem = mem
            self.count = len(pids); self.io_denied = io_denied

    # ---------- sockets via `ss -tinp` (bytes per TCP socket for all users; pid only for own) ----------
    def sample_net(self):
        out = run(["ss", "-tinpH", "state", "established"], timeout=5)
        lst = run(["ss", "-ltnpH"], timeout=5)
        if not out and not lst:
            return super().sample_net()          # ss missing (minimal containers) -> psutil fallback
        nudp = len(run(["ss", "-unaH"], timeout=5).splitlines())
        t = time.time(); dt = max(0.5, (t - self._sprev_t) if self._sprev_t else 3)
        cur = {}; procs = {}; flows = []; conn = {"tcp": 0, "udp": nudp, "listen": 0, "est": 0}; port_owner = {}
        my_port = str(self.cfg.get("port"))
        lines = out.splitlines(); i = 0
        while i < len(lines):
            head = lines[i]; info = lines[i + 1] if i + 1 < len(lines) and lines[i + 1][:1] in ("\t", " ") else ""
            i += 2 if info else 1
            tok = head.split()
            if len(tok) < 4: continue
            la, ra = tok[2], tok[3]; rest = " ".join(tok[4:])
            conn["tcp"] += 1; conn["est"] += 1
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', rest)
            pid = int(m.group(2)) if m else 0; nm = m.group(1) if m else ""
            mr = re.search(r"bytes_received:(\d+)", info); ma = re.search(r"bytes_acked:(\d+)", info) or re.search(r"bytes_sent:(\d+)", info)
            rxb = int(mr.group(1)) if mr else 0; txb = int(ma.group(1)) if ma else 0
            lh, lp = split_hp(la); rh, rp = split_hp(ra)
            key = (la, ra); cur[key] = (rxb, txb)
            pv = self._sprev.get(key)
            if pv is None: pv = (rxb, txb) if self._sfirst else (0, 0)
            rx = max(0, rxb - pv[0]) / dt; tx = max(0, txb - pv[1]) / dt
            if pid:
                pr = procs.setdefault(pid, {"pid": pid, "name": nm, "rx": 0.0, "tx": 0.0, "n": 0})
            else:
                k2 = "other:" + lp if int(lp or 0) < 32768 else "other"
                pr = procs.setdefault(k2, {"pid": 0, "name": (":%s" % lp) if k2 != "other" else "?", "rx": 0.0, "tx": 0.0, "n": 0, "anon": True})
            pr["rx"] += rx; pr["tx"] += tx; pr["n"] += 1
            if rp == my_port: port_owner[lp] = (pid, nm)
            flows.append({"pid": pid, "name": nm, "local": lh, "lport": lp, "remote": rh, "rport": rp, "iface": self.iface_of(lh), "rx": rx, "tx": tx})
        listen = {}
        for line in lst.splitlines():
            tok = line.split()
            if len(tok) < 4: continue
            h, p = split_hp(tok[3])
            try: p = int(p)
            except ValueError: continue
            conn["listen"] += 1
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            e = listen.setdefault(p, {"port": p, "hosts": [], "proc": m.group(1) if m else "", "pid": int(m.group(2)) if m else 0})
            e["hosts"].append(h)
        with self.net_lock:
            self.net = {"procs": sorted(procs.values(), key=lambda x: -(x["rx"] + x["tx"])), "flows": flows, "conn": conn,
                        "listen": listen, "port_owner": port_owner, "updated": t}
        self._sprev, self._sprev_t, self._sfirst = cur, t, False

    # ---------- GPU: NVML in-process, nvidia-smi fallback ----------
    def gpu_sample(self):
        if self.gpu_h is None:
            return self.nvidia_smi()
        h = self.gpu_h
        def g(fn, *a):
            try: return fn(h, *a)
            except Exception: return None
        u = g(NV.nvmlDeviceGetUtilizationRates); temp = g(NV.nvmlDeviceGetTemperature, 0); pw = g(NV.nvmlDeviceGetPowerUsage)
        sm = g(NV.nvmlDeviceGetClockInfo, 1) or g(NV.nvmlDeviceGetClockInfo, 0); ps = g(NV.nvmlDeviceGetPerformanceState)
        mem = g(NV.nvmlDeviceGetMemoryInfo)
        self._gtick += 1
        if self._gtick % 3 == 1:
            procs = []
            for p in g(NV.nvmlDeviceGetComputeRunningProcesses) or []:
                try: nm = open("/proc/%d/comm" % p.pid).read().strip()
                except Exception: nm = str(p.pid)
                procs.append({"pid": p.pid, "name": nm, "mem": getattr(p, "usedGpuMemory", None), "ctr": self.pid_container(p.pid)[:12]})
            self.gpu_procs = procs
        used = sum(p["mem"] or 0 for p in self.gpu_procs)
        return {"present": True, "name": self.gpu_name, "util": u.gpu if u else None, "mem_util": u.memory if u else None,
                "temp": temp, "power": round(pw / 1000, 1) if pw is not None else None, "clock_sm": sm, "pstate": ps,
                "mem_used": mem.used if mem else used, "mem_total": mem.total if mem else None, "mem_unified": mem is None,
                "procs": self.gpu_procs, "src": "NVML"}

    # ---------- disks ----------
    def real_disk(self, n):
        return not n.startswith(("loop", "ram", "dm-", "zram", "sr", "md")) and not re.match(r"^(nvme\d+n\d+p\d+|sd[a-z]+\d+|vd[a-z]+\d+|mmcblk\d+p\d+)$", n)

    def volumes(self):
        return [v for v in super().volumes() if not v["mount"].startswith(("/snap", "/boot", "/var/snap", "/run", "/sys", "/proc", "/dev"))]

    # ---------- routing ----------
    def main_nic(self):
        try:
            for line in open("/proc/net/route").read().splitlines()[1:]:
                sp = line.split()
                if sp[1] == "00000000": return sp[0]
        except Exception: pass
        return super().main_nic()

    def default_gateway(self):
        try:
            for line in open("/proc/net/route").read().splitlines()[1:]:
                sp = line.split()
                if sp[1] == "00000000" and sp[2] != "00000000":
                    return socket.inet_ntoa(bytes.fromhex(sp[2])[::-1])
        except Exception: pass
        return ""

    def traceroute(self, target, hops):
        out = run(["traceroute", "-n", "-q", "1", "-w", "1", "-m", str(hops), target], timeout=4 + hops * 2)
        if not out:
            out = run(["tracepath", "-n", "-m", str(hops), target], timeout=4 + hops * 3)
        ips = []
        for line in out.splitlines():
            m = re.match(r"^\s*(\d+):?\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m and m.group(2) not in ips: ips.append(m.group(2))
        return ips[:hops]
