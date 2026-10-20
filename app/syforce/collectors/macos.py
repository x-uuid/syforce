"""macOS collector (Apple Silicon / Intel): proc_pid_rusage, `netstat -anvb`, IOKit/ioreg GPU, sysctl memory pressure.

Caveats
- No per-process GPU usage without sudo (powermetrics); only device-wide utilisation from IOAccelerator.
- proc_pid_rusage is denied for other users' processes -> `ps` fallback every 30 s (approximate).
- `netstat -anvb` gives per-socket rx/tx bytes for ALL users without sudo (~10 ms), much cheaper than nettop.
- Unprivileged ICMP sockets are allowed on macOS, so ping needs no setuid binary.
"""
import ctypes, ctypes.util, heapq, os, platform, re, time
import psutil
from .common import BaseCollector
from ..util import run, log, MB

libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
class _TB(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]
_tb = _TB(); libc.mach_timebase_info(ctypes.byref(_tb))
TICK_NS = _tb.numer / _tb.denom
class _RU(ctypes.Structure):   # rusage_info_v2 (fields addressed by index)
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [("f%d" % i, ctypes.c_uint64) for i in range(64)]
libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
libc.proc_name.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]


def sysctl_int(name):
    v = ctypes.c_int64(0); sz = ctypes.c_size_t(8)
    if libc.sysctlbyname(name.encode(), ctypes.byref(v), ctypes.byref(sz), None, 0) != 0: return None
    return v.value if sz.value == 8 else ctypes.c_int32(v.value & 0xffffffff).value


class MacCollector(BaseCollector):
    os_name = "macos"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._names = {}; self._apps = {}; self._ps = {}; self._ps_t = 0
        self._nprev = {}; self._nprev_t = None; self._nfirst = True
        self.cpu_labels, self.cpu_kinds = self._topology()
        self._gpu = None; self._mp = 1; self._mp_t = 0

    def platform_label(self):
        chip = run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        return "macOS %s%s" % (platform.mac_ver()[0], " · " + chip if chip else "")

    # ---------- CPU topology from IOPlatformDevice cluster-type ----------
    def _topology(self):
        out = run(["ioreg", "-rd1", "-c", "IOPlatformDevice"])
        types = {}; cur = {}
        for line in out.splitlines():
            m = re.search(r'"logical-cpu-id" = (\d+)', line)
            if m: cur["id"] = int(m.group(1))
            m = re.search(r'"cluster-type" = <"(\w)">', line)
            if m: cur["t"] = m.group(1)
            if "id" in cur and "t" in cur: types[cur["id"]] = cur["t"]; cur = {}
        lv0 = run(["sysctl", "-n", "hw.perflevel0.name"]).strip() or "Performance"
        lv1 = run(["sysctl", "-n", "hw.perflevel1.name"]).strip() or "Efficiency"
        lab = []
        for i in range(self.ncpu):
            t = types.get(i, "P")
            lab.append("E" if t == "E" else "P")
        kinds = {"P": lv0, "E": lv1} if "E" in lab else {"P": "CPU"}
        return lab, kinds

    def mem_extra(self, vm):
        t = time.time()
        if t - self._mp_t > 5:
            self._mp_t = t; self._mp = sysctl_int("kern.memorystatus_vm_pressure_level") or 1
        return {"psi": None, "pressure": self._mp}

    def loadavg(self):
        return list(os.getloadavg())

    # ---------- names ----------
    def proc_name(self, pid):
        n = self._names.get(pid)
        if n is None:
            b = ctypes.create_string_buffer(256); libc.proc_name(pid, b, 256)
            n = b.value.decode("utf-8", "replace") or str(pid)
            if len(n) >= 15:
                try: n = psutil.Process(pid).name()
                except Exception: pass
            if len(self._names) > 5000: self._names.clear()
            self._names[pid] = n
        return n

    def app_of(self, pid, path=None):
        """outermost .app bundle (e.g. the browser for its helper processes)"""
        v = self._apps.get(pid)
        if v is None:
            if path is None:
                b = ctypes.create_string_buffer(4096)
                path = b.value.decode("utf-8", "replace") if libc.proc_pidpath(pid, b, 4096) > 0 else ""
            v = next((p[:-4] for p in path.split("/") if p.endswith(".app")), "")
            if len(self._apps) > 5000: self._apps.clear()
            self._apps[pid] = v
        return v

    # ---------- processes via proc_pid_rusage ----------
    def sample_procs(self):
        t = time.time(); dt = (t - self._pprev_t) if self._pprev_t else None
        ru = _RU(); cur = {}; rows = []; denied = []; fp = {}
        pids = psutil.pids()
        for pid in pids:
            if libc.proc_pid_rusage(pid, 2, ctypes.byref(ru)) != 0:
                denied.append(pid); continue
            cpu_t = (ru.f0 + ru.f1) * TICK_NS; rd, wr = ru.f16, ru.f17
            cur[pid] = (cpu_t, rd, wr); fp[pid] = ru.f7
            pv = self._pprev.get(pid)
            if pv and dt:
                c = max(0, cpu_t - pv[0]) / 1e9 / dt * 100; r = max(0, rd - pv[1]) / dt; w = max(0, wr - pv[2]) / dt
                if c > 0.5 or r + w > 0: rows.append((pid, c, r, w, ru.f7))
        self._pprev, self._pprev_t = cur, t
        if t - self._ps_t > 30:      # other users' processes: ps (setuid) every 30 s
            self._ps_t = t; m = {}
            for line in run(["ps", "-Ao", "pid=,pcpu=,rss=,comm="]).splitlines():
                sp = line.split(None, 3)
                if len(sp) < 4: continue
                try: m[int(sp[0])] = (float(sp[1]), int(sp[2]) * 1024, os.path.basename(sp[3])); self.app_of(int(sp[0]), sp[3])
                except ValueError: pass
            self._ps = m
        dset = set(denied)
        cpu = [{"pid": p, "name": self.proc_name(p), "app": self.app_of(p), "cpu": round(c, 1), "mem": mem} for p, c, r, w, mem in rows if c > 0.5]
        for p, (c, rss, nm) in self._ps.items():
            if p in dset and c > 0.5: cpu.append({"pid": p, "name": nm, "app": self.app_of(p), "cpu": c, "mem": rss, "approx": True})
            if p in dset: fp[p] = rss
        cpu.sort(key=lambda x: -x["cpu"])
        disk = sorted([{"pid": p, "name": self.proc_name(p), "app": self.app_of(p), "r": r, "w": w} for p, c, r, w, mem in rows if r + w > 0], key=lambda x: -(x["r"] + x["w"]))
        top_mem = [{"pid": p, "name": self.proc_name(p) if p not in dset else self._ps[p][2], "app": self.app_of(p), "mem": m}
                   for p, m in heapq.nlargest(10, fp.items(), key=lambda x: x[1])]
        with self.proc_lock:
            self.top_cpu = cpu[:12]; self.top_disk = disk[:8]; self.top_mem = top_mem; self.count = len(pids); self.io_denied = len(denied)

    def proc_cmd(self, pid, name=""):
        return ""

    # ---------- sockets via `netstat -anvb` ----------
    @staticmethod
    def _hp(a):
        h, _, p = a.rpartition(".")
        return h.split("%")[0], p

    def sample_net(self):
        out = run(["netstat", "-anvb", "-f", "inet"]) + run(["netstat", "-anvb", "-f", "inet6"])
        t = time.time(); dt = max(0.5, (t - self._nprev_t) if self._nprev_t else 2)
        cur = {}; procs = {}; flows = []; conn = {"tcp": 0, "udp": 0, "listen": 0, "est": 0}; port_owner = {}; listen = {}
        my_port = str(self.cfg.get("port"))
        for line in out.splitlines():
            if not (line.startswith("tcp") or line.startswith("udp")): continue
            tok = line.split(); proto = tok[0]
            lead = 10 if proto.startswith("tcp") else 9          # column where "process:pid" starts
            if len(tok) < lead + 9: continue
            state = tok[5] if proto.startswith("tcp") else ""
            conn["tcp" if proto.startswith("tcp") else "udp"] += 1
            pname = " ".join(tok[lead:-8]); nm, _, ps = pname.rpartition(":")
            try: pid = int(ps)
            except ValueError: pid = 0
            nm = nm.strip() or (self.proc_name(pid) if pid else "")
            lh, lp = self._hp(tok[3]); rh, rp = self._hp(tok[4])
            if state == "LISTEN":
                conn["listen"] += 1
                try:
                    e = listen.setdefault(int(lp), {"port": int(lp), "hosts": [], "proc": nm, "pid": pid})
                    e["hosts"].append({"*": "0.0.0.0"}.get(lh, lh))
                except ValueError: pass
                continue
            if state == "ESTABLISHED": conn["est"] += 1
            try: rxb = int(tok[lead - 4]); txb = int(tok[lead - 3])
            except ValueError: continue
            key = (pid, tok[-6]); cur[key] = (rxb, txb)
            pv = self._nprev.get(key)
            if pv is None: pv = (rxb, txb) if self._nfirst else (0, 0)
            rx = max(0, rxb - pv[0]) / dt; tx = max(0, txb - pv[1]) / dt
            if pid:
                pr = procs.setdefault(pid, {"pid": pid, "name": nm, "rx": 0.0, "tx": 0.0, "n": 0}); pr["rx"] += rx; pr["tx"] += tx; pr["n"] += 1
            if rh in ("*", ""): continue
            if rp == my_port and state == "ESTABLISHED": port_owner[lp] = (pid, nm)
            flows.append({"pid": pid, "name": nm, "local": lh, "lport": lp, "remote": rh, "rport": rp, "iface": self.iface_of(lh), "rx": rx, "tx": tx})
        for p in procs.values():
            p["app"] = self.app_of(p["pid"])
        with self.net_lock:
            self.net = {"procs": sorted(procs.values(), key=lambda x: -(x["rx"] + x["tx"])), "flows": flows, "conn": conn,
                        "listen": listen, "port_owner": port_owner, "updated": t}
        self._nprev, self._nprev_t, self._nfirst = cur, t, False

    # ---------- GPU: IOAccelerator PerformanceStatistics (device-wide only) ----------
    def gpu_sample(self):
        if self._gpu is None: self._gpu = _IOKitGPU()
        d = self._gpu.sample()
        if d.get("util") is None and d.get("mem_used") is None: return {"present": False}
        return d

    # ---------- disks / routing ----------
    def real_disk(self, n):
        return True

    def volumes(self):
        vols = []
        for v in super().volumes():
            mp = v["mount"]
            if mp.startswith("/System/Volumes/") and mp != "/System/Volumes/Data": continue
            if mp == "/" or mp.startswith("/Library/Developer"): continue
            if mp == "/System/Volumes/Data": v["mount"] = "/"
            vols.append(v)
        return vols

    def nic_kind(self, n):
        if n.startswith("utun") and n == self._tail_nic(): return "tail"
        if n in ("en0",):
            return "wifi" if self._is_wifi(n) else "wired"
        if re.match(r"^en\d+$", n): return "wifi" if self._is_wifi(n) else "wired"
        return super().nic_kind(n)

    _wifi = None
    def _is_wifi(self, n):
        if MacCollector._wifi is None:
            out = run(["networksetup", "-listallhardwareports"]); w = set(); port = ""
            for line in out.splitlines():
                if line.startswith("Hardware Port:"): port = line.split(":", 1)[1].strip()
                elif line.startswith("Device:") and port in ("Wi-Fi", "AirPort"): w.add(line.split(":", 1)[1].strip())
            MacCollector._wifi = w
        return n in MacCollector._wifi

    def _tail_nic(self):
        if not self.tail_ips: return None
        for n, addrs in psutil.net_if_addrs().items():
            if any(a.address in self.tail_ips for a in addrs): return n
        return None

    def default_gateway(self):
        m = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", run(["route", "-n", "get", "default"]))
        return m.group(1) if m else ""

    def traceroute(self, target, hops):
        out = run(["traceroute", "-n", "-q", "1", "-w", "1", "-m", str(hops), target], timeout=4 + hops * 2)
        ips = []
        for line in out.splitlines():
            m = re.match(r"^\s*\d+\s+(\d+\.\d+\.\d+\.\d+)", line)
            if m and m.group(1) not in ips: ips.append(m.group(1))
        return ips[:hops]

    def ping_cmd(self, ip, timeout=2.0):
        out = run(["ping", "-c", "1", "-t", str(int(timeout)), ip], timeout=timeout + 2)
        m = re.search(r"time=([\d.]+) ms", out)
        return float(m.group(1)) if m else None


class _IOKitGPU:
    """Reads IOAccelerator PerformanceStatistics in-process (no sudo, ~0.02 ms); falls back to `ioreg`."""
    KEYS = ["Device Utilization %", "Renderer Utilization %", "Tiler Utilization %", "In use system memory", "Alloc system memory"]

    def __init__(self):
        self.ok = False; self.cls = ""
        try:
            vp = ctypes.c_void_p
            io = self.io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            io.IOServiceMatching.restype = vp; io.IOServiceMatching.argtypes = [ctypes.c_char_p]
            io.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, vp, ctypes.POINTER(ctypes.c_uint32)]
            io.IOIteratorNext.restype = ctypes.c_uint32; io.IOIteratorNext.argtypes = [ctypes.c_uint32]
            io.IORegistryEntryCreateCFProperty.restype = vp; io.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, vp, vp, ctypes.c_uint32]
            io.IOObjectGetClass.argtypes = [ctypes.c_uint32, ctypes.c_char_p]
            cf.CFStringCreateWithCString.restype = vp; cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
            cf.CFDictionaryGetValue.restype = vp; cf.CFDictionaryGetValue.argtypes = [vp, vp]
            cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]; cf.CFRelease.argtypes = [vp]
            mk = lambda x: cf.CFStringCreateWithCString(None, x.encode(), 0x08000100)
            it = ctypes.c_uint32(0)
            if io.IOServiceGetMatchingServices(0, io.IOServiceMatching(b"IOAccelerator"), ctypes.byref(it)) == 0:
                self.entry = io.IOIteratorNext(it.value)
                if self.entry:
                    b = ctypes.create_string_buffer(128); io.IOObjectGetClass(self.entry, b); self.cls = b.value.decode()
                    self.kps = mk("PerformanceStatistics"); self.kk = {k: mk(k) for k in self.KEYS}; self.ok = True
        except Exception as e:
            log("IOKit GPU init failed %r, using ioreg" % e)

    def _wrap(self, v, cls):
        return {"present": True, "name": "GPU" + (" · " + cls if cls else ""), "util": v.get("Device Utilization %"),
                "renderer": v.get("Renderer Utilization %"), "tiler": v.get("Tiler Utilization %"),
                "mem_used": v.get("In use system memory"), "mem_alloc": v.get("Alloc system memory"), "mem_unified": True,
                "temp": None, "power": None, "clock_sm": None, "pstate": None, "procs": [], "src": "IOKit"}

    def sample(self):
        if self.ok:
            d = self.io.IORegistryEntryCreateCFProperty(self.entry, self.kps, None, 0)
            if d:
                vals = {}
                for k, ks in self.kk.items():
                    n = self.cf.CFDictionaryGetValue(d, ks); v = ctypes.c_int64(0)
                    vals[k] = v.value if n and self.cf.CFNumberGetValue(n, 4, ctypes.byref(v)) else None
                self.cf.CFRelease(d)
                return self._wrap(vals, self.cls)
        out = run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], timeout=3)
        vals = {}
        for k in self.KEYS:
            m = re.search(r'"%s"=(\d+)' % re.escape(k), out); vals[k] = int(m.group(1)) if m else None
        m = re.search(r'"IOClass" = "([^"]+)"', out)
        return self._wrap(vals, m.group(1) if m else "")
