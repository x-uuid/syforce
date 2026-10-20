"""BaseCollector: portable psutil implementation of every collector hook.

Platform modules (linux.py / macos.py / windows.py) subclass this and override only what they can do better.
Every hook is READ-ONLY and must stay cheap: the whole backend budget is ~3 % of one core.

Hook contract (all return plain JSON-able dicts/lists; see AGENT_PROMPT.md "data contract"):
  cpu_labels / cpu_kinds          per-logical-CPU class label ("P"/"E"...) and label -> display name
  mem_extra(vm)                   {"psi": float|None, "pressure": 1|2|4}
  sample_procs()                  fills self.top_cpu / top_disk / top_mem / count / io_denied / by_ctr
  sample_net()                    fills self.net (procs, flows, conn, listen, port_owner, updated)
  gpu_sample()                    {"present": bool, "name", "util", "temp", "power", "clock_sm", "mem_used", "mem_total", "procs": []}
  nic_kind(name)                  "wired" | "wifi" | "tail" | "virtual" | "lo" | "other"
  main_nic()                      interface carrying the default route
  default_gateway()               IPv4 of the default gateway or ""
  traceroute(target, hops)        list of hop IPs (first N), [] if tool missing
  ping(ip)                        RTT ms or None
"""
import os, platform, re, socket, struct, select, threading, time
import psutil
from ..util import run, log, MB, CLIENTS, split_hp


class BaseCollector:
    os_name = "generic"

    def __init__(self, cfg):
        self.cfg = cfg
        self.ncpu = psutil.cpu_count() or 1
        self.cpu_labels = ["P"] * self.ncpu
        self.cpu_kinds = {"P": "CPU"}
        self.top_cpu = []; self.top_disk = []; self.top_mem = []; self.count = 0; self.io_denied = 0; self.by_ctr = {}
        self.proc_lock = threading.Lock()
        self.net_lock = threading.Lock()
        self.net = {"procs": [], "flows": [], "conn": {"tcp": 0, "udp": 0, "listen": 0, "est": 0},
                    "listen": {}, "port_owner": {}, "updated": 0}
        self.tail_ips = set()            # set by the tailscale plugin; used to tag flows
        self._pprev = {}; self._pprev_t = None; self._pcache = {}
        self._gw = None; self._gw_t = 0; self._main = None; self._main_t = 0
        self._smi = None

    # ---------- lifecycle ----------
    def start(self):
        threading.Thread(target=self._net_loop, daemon=True).start()

    def _net_loop(self):
        while True:
            try: self.sample_net()
            except Exception as e: log("net sampler error %r" % e)
            time.sleep(3 if CLIENTS[0] > 0 else 6)

    def platform_label(self):
        return "%s %s · %s" % (platform.system(), platform.release(), platform.machine())

    # ---------- cpu / memory ----------
    def cpu_freqs(self):
        try: return [round(f.current) for f in psutil.cpu_freq(percpu=True)]
        except Exception: return None

    def loadavg(self):
        try: return list(psutil.getloadavg())
        except Exception: return [0.0, 0.0, 0.0]

    def mem_extra(self, vm):
        return {"psi": None, "pressure": 4 if vm.percent > 95 else 2 if vm.percent > 85 else 1}

    # ---------- processes (psutil, portable but slower than /proc or proc_pid_rusage) ----------
    def proc_cmd(self, pid, name=""):
        try: return " ".join(psutil.Process(pid).cmdline())[:200]
        except Exception: return ""

    def pid_container(self, pid):
        return ""

    def sample_procs(self):
        t = time.time(); dt = (t - self._pprev_t) if self._pprev_t else None
        cur = {}; rows = []; mem = []; denied = 0; n = 0
        for p in psutil.process_iter(["pid", "name"]):
            n += 1
            try:
                ct = p.cpu_times(); cpu_t = ct.user + ct.system
                rss = p.memory_info().rss
            except Exception:
                denied += 1; continue
            rd = wr = None
            try:
                io = p.io_counters(); rd, wr = io.read_bytes, io.write_bytes
            except Exception: pass
            pid = p.info["pid"]; nm = p.info["name"] or str(pid)
            cur[pid] = (cpu_t, rd, wr, nm)
            if rss > 64 * MB: mem.append((rss, pid, nm))
            pv = self._pprev.get(pid)
            if pv and dt and pv[3] == nm:
                c = max(0.0, cpu_t - pv[0]) / dt * 100
                r = max(0, rd - pv[1]) / dt if rd is not None and pv[1] is not None else 0
                w = max(0, wr - pv[2]) / dt if wr is not None and pv[2] is not None else 0
                if c > 0.5 or r + w > 0: rows.append((pid, nm, c, r, w, rss))
        self._pprev, self._pprev_t = cur, t
        cpu = sorted([{"pid": p, "name": nm, "cmd": "", "cpu": round(c, 1), "mem": rss} for p, nm, c, r, w, rss in rows if c > 0.5], key=lambda x: -x["cpu"])
        disk = sorted([{"pid": p, "name": nm, "r": r, "w": w} for p, nm, c, r, w, rss in rows if r + w > 0], key=lambda x: -(x["r"] + x["w"]))
        mem.sort(reverse=True)
        with self.proc_lock:
            self.top_cpu = cpu[:12]; self.top_disk = disk[:8]; self.count = n; self.io_denied = denied
            self.top_mem = [{"pid": p, "name": nm, "mem": rss} for rss, p, nm in mem[:10]]

    # ---------- network sockets (psutil: counts + peers, NO per-socket byte counters) ----------
    def sample_net(self):
        try: conns = psutil.net_connections(kind="inet")
        except Exception: conns = []
        names = {}
        def pname(pid):
            if not pid: return ""
            if pid not in names:
                try: names[pid] = psutil.Process(pid).name()
                except Exception: names[pid] = str(pid)
            return names[pid]
        conn = {"tcp": 0, "udp": 0, "listen": 0, "est": 0}; flows = []; listen = {}; port_owner = {}; procs = {}
        my_port = str(self.cfg.get("port"))
        for c in conns:
            tcp = c.type == socket.SOCK_STREAM
            conn["tcp" if tcp else "udp"] += 1
            if tcp and c.status == "LISTEN":
                conn["listen"] += 1
                e = listen.setdefault(c.laddr.port, {"port": c.laddr.port, "hosts": [], "proc": pname(c.pid), "pid": c.pid or 0})
                e["hosts"].append(c.laddr.ip); continue
            if not c.raddr: continue
            if tcp and c.status == "ESTABLISHED": conn["est"] += 1
            nm = pname(c.pid)
            if str(c.raddr.port) == my_port: port_owner[str(c.laddr.port)] = (c.pid or 0, nm)
            flows.append({"pid": c.pid or 0, "name": nm, "local": c.laddr.ip, "lport": str(c.laddr.port),
                          "remote": c.raddr.ip, "rport": str(c.raddr.port), "iface": self.iface_of(c.laddr.ip), "rx": 0.0, "tx": 0.0})
            if c.pid:
                pr = procs.setdefault(c.pid, {"pid": c.pid, "name": nm, "rx": 0.0, "tx": 0.0, "n": 0}); pr["n"] += 1
        with self.net_lock:
            self.net = {"procs": sorted(procs.values(), key=lambda x: -x["n"]), "flows": flows, "conn": conn,
                        "listen": listen, "port_owner": port_owner, "updated": time.time()}

    def iface_of(self, local_ip):
        if local_ip in self.tail_ips: return "tailscale"
        if local_ip.startswith("127.") or local_ip == "::1": return "lo"
        if local_ip.startswith("172.17."): return "docker"
        return "lan" if local_ip else ""

    # ---------- GPU (generic: nvidia-smi if present) ----------
    def gpu_sample(self):
        return self.nvidia_smi()

    def nvidia_smi(self):
        if self._smi is False: return {"present": False}
        out = run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,clocks.sm,memory.used,memory.total,pstate",
                   "--format=csv,noheader,nounits"], timeout=4)
        sp = [x.strip() for x in out.split("\n")[0].split(",")] if out else []
        if len(sp) < 5:
            if self._smi is None: self._smi = False
            return {"present": False}
        self._smi = True
        f = lambda v: float(v) if re.match(r"^[\d.]+$", v or "") else None
        mu = f(sp[5]) if len(sp) > 5 else None; mt = f(sp[6]) if len(sp) > 6 else None
        return {"present": True, "name": sp[0], "util": f(sp[1]), "temp": f(sp[2]), "power": f(sp[3]), "clock_sm": f(sp[4]),
                "mem_used": mu * MB if mu is not None else None, "mem_total": mt * MB if mt is not None else None,
                "pstate": (sp[7][1:] if len(sp) > 7 and sp[7].startswith("P") else None), "procs": [], "src": "nvidia-smi"}

    # ---------- disks ----------
    def real_disk(self, name):
        return True

    def volumes(self):
        vols = []
        for p in psutil.disk_partitions(all=False):
            if p.fstype in ("squashfs", "overlay", "tmpfs", "devtmpfs", "autofs"): continue
            try: u = psutil.disk_usage(p.mountpoint)
            except Exception: continue
            vols.append({"mount": p.mountpoint, "device": p.device, "fs": p.fstype, "total": u.total, "used": u.used, "free": u.free, "pct": u.percent})
        return vols

    # ---------- interfaces / routing ----------
    def nic_kind(self, n):
        ln = n.lower()
        if ln in ("lo", "lo0") or ln.startswith("loopback"): return "lo"
        if ln.startswith("tailscale") or ln == "tailscale0": return "tail"
        if ln.startswith(("wl", "wi-fi", "wifi", "wlan")) or "wireless" in ln: return "wifi"
        if ln.startswith(("veth", "docker", "br-", "virbr", "vmnet", "vbox", "utun", "awdl", "llw", "anpi", "gif", "stf", "bridge", "ap")): return "virtual"
        if ln.startswith(("en", "eth", "ethernet")): return "wired"
        return "other"

    def main_nic(self):
        """interface whose IPv4 is used for the default route (UDP connect trick: no packets sent)."""
        t = time.time()
        if self._main and t - self._main_t < 60: return self._main
        self._main_t = t; ip = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("192.0.2.1", 9)); ip = s.getsockname()[0]; s.close()
        except Exception: pass
        for n, addrs in psutil.net_if_addrs().items():
            if any(a.family == socket.AF_INET and a.address == ip for a in addrs):
                self._main = n; return n
        self._main = self._main or ""
        return self._main

    def default_gateway(self):
        return ""

    def traceroute(self, target, hops):
        return []

    # ---------- ping (unprivileged ICMP socket, fallback to the ping binary) ----------
    def ping(self, ip, timeout=2.0):
        ms = icmp_dgram_ping(ip, timeout)
        if ms is not False: return ms
        return self.ping_cmd(ip, timeout)

    def ping_cmd(self, ip, timeout=2.0):
        out = run(["ping", "-n", "-c", "1", "-W", str(int(timeout)), ip], timeout=timeout + 2)
        m = re.search(r"time[=<]([\d.]+) ?ms", out)
        return float(m.group(1)) if m else None


_ICMP_OK = [None]
def icmp_dgram_ping(ip, timeout=2.0):
    """Returns ms, None (timeout) or False (unprivileged ICMP sockets not allowed here)."""
    if _ICMP_OK[0] is False or ":" in ip: return False
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (OSError, AttributeError):
        _ICMP_OK[0] = False; return False
    try:
        ident = os.getpid() & 0xffff; seq = int(time.time() * 10) & 0xffff; data = b"syforce-ping"
        hdr = struct.pack("!BBHHH", 8, 0, 0, ident, seq); pkt = hdr + data; cs = 0
        if len(pkt) % 2: pkt += b"\0"
        for i in range(0, len(pkt), 2): cs += (pkt[i] << 8) + pkt[i + 1]
        cs = (cs >> 16) + (cs & 0xffff); cs = ~(cs + (cs >> 16)) & 0xffff
        t0 = time.time(); sk.sendto(struct.pack("!BBHHH", 8, 0, cs, ident, seq) + data, (ip, 0))
        while True:
            left = timeout - (time.time() - t0)
            if left <= 0: return None
            r, _, _ = select.select([sk], [], [], left)
            if not r: return None
            buf, addr = sk.recvfrom(1024)
            if addr[0] != ip: continue
            off = (buf[0] & 0x0f) * 4 if len(buf) >= 20 and (buf[0] >> 4) == 4 else 0   # macOS includes the IP header
            if len(buf) >= off + 8 and buf[off] == 0:
                _ICMP_OK[0] = True
                return round((time.time() - t0) * 1000, 2)
    except PermissionError:
        _ICMP_OK[0] = False; return False
    except OSError:
        return None
    finally:
        sk.close()
