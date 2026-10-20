"""Windows 10/11 collector. No admin needed; everything is read-only.

Sources
- processes: ONE NtQuerySystemInformation(SystemProcessInformation) snapshot per sample -> CPU time, working set and
  I/O bytes for ALL processes (incl. SYSTEM services) in a few ms. psutil per-process calls fall back to a full
  process scan for every denied pid (O(n^2)), so they are only used as a fallback.
  Names: exe basename without ".exe" ("name"), FileDescription from the exe version resource ("app", cached per path).
- CPU topology: GetLogicalProcessorInformationEx EfficiencyClass -> P / E cores (Intel hybrid).
- NICs: GetAdaptersAddresses IfType + Description -> wired / wifi / tail / virtual / lo (works with renamed or
  localized names such as "WLAN" / "以太网"); default gateway from the same call, `route print` fallback.
- GPU: NVML (nvidia-ml-py) in-process -> `nvidia-smi` in a background thread -> no GPU panel.
- ping: IcmpSendEcho (iphlpapi) -> `ping.exe` fallback.  Memory pressure: commit charge (K32GetPerformanceInfo).

Caveats
- No per-socket byte counters without admin (GetPerTcpConnectionEStats / ETW): hosts carry connection counts,
  rx/tx stay 0 and particles on host links stay idle.
- No temperature sensors without admin / vendor WMI: temps() is None (the GPU temperature still comes from NVML).
- Non-NVIDIA GPUs: no panel (route would be PDH "\\GPU Engine(*)\\Utilization Percentage").
- Polling NVML / nvidia-smi can keep an Optimus dGPU awake on battery; set "gpu": {"enabled": "false"} if that matters.
"""
import ctypes, os, platform, re, shutil, socket, struct, sys, threading, time
from ctypes import c_uint8, c_uint16, c_uint32, c_int32, c_uint64, c_int64, c_size_t, c_void_p, byref, POINTER
import psutil
from .common import BaseCollector
from ..util import run, log, MB, CLIENTS

try:
    import pynvml as NV
except Exception:
    NV = None
try:
    import winreg
except Exception:
    winreg = None


def _dll(name):
    W = getattr(ctypes, "WinDLL", None)
    if W is None: return None
    try: return W(name, use_last_error=True)
    except OSError: return None


# ------------------------------------------------------------------ native structures (explicit widths: x64 layout)
class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", c_uint16), ("MaximumLength", c_uint16), ("Buffer", c_void_p)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("NextEntryOffset", c_uint32), ("NumberOfThreads", c_uint32), ("WorkingSetPrivateSize", c_int64),
                ("HardFaultCount", c_uint32), ("NumberOfThreadsHighWatermark", c_uint32), ("CycleTime", c_uint64),
                ("CreateTime", c_int64), ("UserTime", c_int64), ("KernelTime", c_int64), ("ImageName", UNICODE_STRING),
                ("BasePriority", c_int32), ("UniqueProcessId", c_void_p), ("InheritedFromUniqueProcessId", c_void_p),
                ("HandleCount", c_uint32), ("SessionId", c_uint32), ("UniqueProcessKey", c_size_t),
                ("PeakVirtualSize", c_size_t), ("VirtualSize", c_size_t), ("PageFaultCount", c_uint32),
                ("PeakWorkingSetSize", c_size_t), ("WorkingSetSize", c_size_t),
                ("QuotaPeakPagedPoolUsage", c_size_t), ("QuotaPagedPoolUsage", c_size_t),
                ("QuotaPeakNonPagedPoolUsage", c_size_t), ("QuotaNonPagedPoolUsage", c_size_t),
                ("PagefileUsage", c_size_t), ("PeakPagefileUsage", c_size_t), ("PrivatePageCount", c_size_t),
                ("ReadOperationCount", c_int64), ("WriteOperationCount", c_int64), ("OtherOperationCount", c_int64),
                ("ReadTransferCount", c_int64), ("WriteTransferCount", c_int64), ("OtherTransferCount", c_int64)]


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", c_void_p), ("iSockaddrLength", c_int32)]


class IP_ADAPTER_GATEWAY_ADDRESS(ctypes.Structure):
    pass
IP_ADAPTER_GATEWAY_ADDRESS._fields_ = [("Length", c_uint32), ("Reserved", c_uint32),
                                       ("Next", POINTER(IP_ADAPTER_GATEWAY_ADDRESS)), ("Address", SOCKET_ADDRESS)]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass
IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", c_uint32), ("IfIndex", c_uint32), ("Next", POINTER(IP_ADAPTER_ADDRESSES)), ("AdapterName", c_void_p),
    ("FirstUnicastAddress", c_void_p), ("FirstAnycastAddress", c_void_p), ("FirstMulticastAddress", c_void_p),
    ("FirstDnsServerAddress", c_void_p), ("DnsSuffix", c_void_p), ("Description", c_void_p), ("FriendlyName", c_void_p),
    ("PhysicalAddress", c_uint8 * 8), ("PhysicalAddressLength", c_uint32), ("Flags", c_uint32), ("Mtu", c_uint32),
    ("IfType", c_uint32), ("OperStatus", c_uint32), ("Ipv6IfIndex", c_uint32), ("ZoneIndices", c_uint32 * 16),
    ("FirstPrefix", c_void_p), ("TransmitLinkSpeed", c_uint64), ("ReceiveLinkSpeed", c_uint64),
    ("FirstWinsServerAddress", c_void_p), ("FirstGatewayAddress", POINTER(IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Ipv4Metric", c_uint32), ("Ipv6Metric", c_uint32)]


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [("Address", c_uint32), ("Status", c_uint32), ("RoundTripTime", c_uint32), ("DataSize", c_uint16),
                ("Reserved", c_uint16), ("Data", c_void_p), ("Ttl", c_uint8), ("Tos", c_uint8), ("Flags", c_uint8),
                ("OptionsSize", c_uint8), ("OptionsData", c_void_p)]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [("cb", c_uint32), ("CommitTotal", c_size_t), ("CommitLimit", c_size_t), ("CommitPeak", c_size_t),
                ("PhysicalTotal", c_size_t), ("PhysicalAvailable", c_size_t), ("SystemCache", c_size_t),
                ("KernelTotal", c_size_t), ("KernelPaged", c_size_t), ("KernelNonpaged", c_size_t), ("PageSize", c_size_t),
                ("HandleCount", c_uint32), ("ProcessCount", c_uint32), ("ThreadCount", c_uint32)]


# ------------------------------------------------------------------ pure helpers (unit-tested on any OS)
def u16str(addr, limit=512):
    """NUL-terminated UTF-16LE string at addr (portable: wchar_t is 4 bytes off Windows)."""
    if not addr: return ""
    out = bytearray()
    for i in range(limit):
        ch = ctypes.string_at(addr + 2 * i, 2)
        if ch == b"\0\0": break
        out += ch
    return out.decode("utf-16-le", "replace")


def parse_spi(addr, size):
    """walk a SystemProcessInformation buffer -> [(pid, image, cpu_seconds, working_set, read_bytes, write_bytes)]"""
    out = []; off = 0
    while off + ctypes.sizeof(SYSTEM_PROCESS_INFORMATION) <= size:
        e = SYSTEM_PROCESS_INFORMATION.from_address(addr + off)
        pid = e.UniqueProcessId or 0
        if e.ImageName.Buffer and e.ImageName.Length:
            img = ctypes.string_at(e.ImageName.Buffer, e.ImageName.Length).decode("utf-16-le", "replace")
        else:
            img = "Idle" if pid == 0 else "System"
        out.append((pid, img, (e.UserTime + e.KernelTime) / 1e7, e.WorkingSetSize, e.ReadTransferCount, e.WriteTransferCount))
        if not e.NextEntryOffset: break
        off += e.NextEntryOffset
    return out


def parse_lpi_ex(raw, ncpu):
    """GetLogicalProcessorInformationEx(RelationProcessorCore) bytes -> (labels, kinds). Higher EfficiencyClass = P."""
    eff = {}; off = 0
    while off + 8 <= len(raw):
        rel, size = struct.unpack_from("<II", raw, off)
        if size == 0: break
        if rel == 0 and off + 32 <= len(raw):
            ecls = raw[off + 9]; gcount = struct.unpack_from("<H", raw, off + 30)[0]
            for g in range(gcount):
                if off + 32 + g * 16 + 10 > len(raw): break
                mask, grp = struct.unpack_from("<QH", raw, off + 32 + g * 16)
                for b in range(64):
                    if mask >> b & 1: eff[grp * 64 + b] = ecls
        off += size
    if sorted(eff) != list(range(ncpu)) or len(set(eff.values())) < 2:
        return ["P"] * ncpu, {"P": "CPU"}
    hi = max(eff.values())
    return ["P" if eff[i] == hi else "E" for i in range(ncpu)], {"P": "Performance", "E": "Efficiency"}


_VIRT = ("hyper-v", "vethernet", "virtual", "vmware", "virtualbox", "vbox", "wsl", "wintun", "tap-windows", "openvpn",
         "zerotier", "wan miniport", "wi-fi direct", "kernel debug", "teredo", "isatap", "6to4", "npcap", "docker",
         "wireguard", "loopback adapter", "pseudo-interface")
_IF_ETHERNET, _IF_WIFI, _IF_LOOPBACK = 6, 71, 24


def classify_nic(name, desc="", iftype=None):
    """wired | wifi | tail | virtual | lo | other  from friendly name, adapter description and IfType."""
    n = (name or "").lower(); d = (desc or "").lower()
    if iftype == _IF_LOOPBACK or n.startswith("loopback") or "loopback pseudo" in n: return "lo"
    if "tailscale" in n or "tailscale" in d: return "tail"
    if any(k in n or k in d for k in _VIRT): return "virtual"
    if "bluetooth" in n or "bluetooth" in d: return "other"
    if iftype == _IF_WIFI: return "wifi"
    if iftype == _IF_ETHERNET: return "wired"
    if n.startswith(("wi-fi", "wifi", "wlan", "wireless")) or any(k in d for k in ("wireless", "wi-fi", "802.11", "wlan")): return "wifi"
    if n.startswith(("ethernet", "eth", "以太网", "乙太網路", "イーサネット", "이더넷")) or "ethernet" in d or "gbe" in d: return "wired"
    return "other"


def parse_adapters(first):
    """walk an IP_ADAPTER_ADDRESSES linked list (pointer) -> {friendly_name: {...}}"""
    out = {}; p = first
    while p:
        a = p.contents; gws = []
        g = a.FirstGatewayAddress
        while g:
            sa = g.contents.Address
            if sa.lpSockaddr and sa.iSockaddrLength >= 8:
                raw = ctypes.string_at(sa.lpSockaddr, min(sa.iSockaddrLength, 28))
                fam = struct.unpack_from("<H", raw)[0]
                if fam == 2: gws.append(socket.inet_ntoa(raw[4:8]))                     # AF_INET on Windows
            g = g.contents.Next
        name = u16str(a.FriendlyName); desc = u16str(a.Description)
        out[name] = {"name": name, "desc": desc, "iftype": a.IfType, "up": a.OperStatus == 1, "gw": gws,
                     "speed": (a.TransmitLinkSpeed // 1000000) if a.TransmitLinkSpeed and a.TransmitLinkSpeed < 2 ** 63 else 0,
                     "kind": classify_nic(name, desc, a.IfType)}
        p = a.Next
    return out


def exe_label(image):
    b = os.path.basename(image or "")
    return b[:-4] if b.lower().endswith(".exe") else b


def nice_name(base, cmd):
    """'python server.py' / 'node app' instead of bare interpreter names."""
    if base.lower() in ("python", "pythonw", "python3", "py", "pyw", "node") and cmd:
        args = re.findall(r'"[^"]*"|\S+', cmd)[1:]
        for a in args:
            a = a.strip('"')
            if a.startswith("-") or not a: continue
            b = re.split(r"[\\/]", a)[-1]
            if b and b.lower() not in ("python.exe", "pythonw.exe"): return base + " " + b[:30]
    return base


# ------------------------------------------------------------------ collector
class WindowsCollector(BaseCollector):
    os_name = "windows"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.k32 = _dll("kernel32"); self.ntdll = _dll("ntdll"); self.iphlp = _dll("iphlpapi"); self.ver = _dll("version")
        self._bind()
        self.cpu_labels, self.cpu_kinds = self._topology()
        self._spibuf = None; self._nt_ok = True
        self.pid_img = {}                       # pid -> image name from the last snapshot
        self._desc = {}; self._pid_app = {}; self._cmd = {}
        self._adapters = {}; self._ad_t = 0
        self._mp = (0, {"psi": None, "pressure": 1})
        self._icmp = None
        self._label = None
        self.gpu_h = None; self.gpu_name = "GPU"; self._gtick = 0; self.gpu_procs = []
        self._smi_last = {"present": False}; self._smi_thread = False
        self.smi_bin = self._find_smi()
        if NV is not None and str(cfg["gpu"].get("enabled")) != "false":
            try:
                NV.nvmlInit(); self.gpu_h = NV.nvmlDeviceGetHandleByIndex(0)
                n = NV.nvmlDeviceGetName(self.gpu_h); self.gpu_name = n.decode() if isinstance(n, bytes) else n
            except Exception as e:
                self.gpu_h = None; log("NVML unavailable (%r) -> nvidia-smi fallback" % e)
        try: psutil.getloadavg()                # starts psutil's load emulation thread (values ramp up over ~1 min)
        except Exception: pass

    def _bind(self):
        """declare ctypes signatures once (no-op off Windows)."""
        k, nt, ip, v = self.k32, self.ntdll, self.iphlp, self.ver
        if nt:
            nt.NtQuerySystemInformation.restype = c_int32
            nt.NtQuerySystemInformation.argtypes = [c_uint32, c_void_p, c_uint32, POINTER(c_uint32)]
        if k:
            k.OpenProcess.restype = c_void_p; k.OpenProcess.argtypes = [c_uint32, c_int32, c_uint32]
            k.CloseHandle.argtypes = [c_void_p]
            k.QueryFullProcessImageNameW.argtypes = [c_void_p, c_uint32, ctypes.c_wchar_p, POINTER(c_uint32)]
            k.GetLogicalProcessorInformationEx.argtypes = [c_uint32, c_void_p, POINTER(c_uint32)]
            k.K32GetPerformanceInfo.argtypes = [POINTER(PERFORMANCE_INFORMATION), c_uint32]
        if ip:
            ip.GetAdaptersAddresses.restype = c_uint32
            ip.GetAdaptersAddresses.argtypes = [c_uint32, c_uint32, c_void_p, c_void_p, POINTER(c_uint32)]
            ip.IcmpCreateFile.restype = c_void_p
            ip.IcmpSendEcho.restype = c_uint32
            ip.IcmpSendEcho.argtypes = [c_void_p, c_uint32, c_void_p, c_uint16, c_void_p, c_void_p, c_uint32, c_uint32]
        if v:
            v.GetFileVersionInfoSizeW.restype = c_uint32; v.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, c_void_p]
            v.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, c_uint32, c_uint32, c_void_p]
            v.VerQueryValueW.argtypes = [c_void_p, ctypes.c_wchar_p, POINTER(c_void_p), POINTER(c_uint32)]

    # ---------- labels / topology ----------
    def _reg(self, path, value):
        if winreg is None: return ""
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k: return str(winreg.QueryValueEx(k, value)[0]).strip()
        except Exception: return ""

    def platform_label(self):
        if self._label is None:
            try: build = sys.getwindowsversion().build
            except Exception: build = 0
            rel = "11" if build >= 22000 else platform.release()
            model = self._reg(r"HARDWARE\DESCRIPTION\System\BIOS", "SystemProductName")
            if model.lower() in ("", "system product name", "to be filled by o.e.m."): model = ""
            self._label = "Windows %s · %s" % (rel, platform.machine()) + (" · " + model[:40] if model else "")
        return self._label

    def _topology(self):
        k = self.k32
        if not k: return ["P"] * self.ncpu, {"P": "CPU"}
        try:
            n = c_uint32(0); k.GetLogicalProcessorInformationEx(0, None, byref(n))
            buf = ctypes.create_string_buffer(n.value or 1)
            if not k.GetLogicalProcessorInformationEx(0, buf, byref(n)): raise OSError(ctypes.get_last_error())
            return parse_lpi_ex(buf.raw[:n.value], self.ncpu)
        except Exception as e:
            log("cpu topology unavailable %r" % e); return ["P"] * self.ncpu, {"P": "CPU"}

    # ---------- memory pressure from commit charge (cheap, cached 5 s) ----------
    def mem_extra(self, vm):
        t = time.time()
        if t - self._mp[0] < 5: return self._mp[1]
        commit = None
        if self.k32:
            pi = PERFORMANCE_INFORMATION(); pi.cb = ctypes.sizeof(pi)
            try:
                if self.k32.K32GetPerformanceInfo(byref(pi), pi.cb) and pi.CommitLimit:
                    commit = pi.CommitTotal / pi.CommitLimit * 100
            except Exception: pass
        p = 4 if vm.percent > 95 or (commit or 0) > 95 else 2 if vm.percent > 85 or (commit or 0) > 85 else 1
        d = {"psi": None, "pressure": p, "commit_pct": round(commit, 1) if commit is not None else None}
        self._mp = (t, d)
        return d

    # ---------- processes ----------
    def nt_processes(self):
        if not self.ntdll: raise OSError("ntdll unavailable")
        need = 1 << 20
        for _ in range(6):
            if self._spibuf is None or len(self._spibuf) < need: self._spibuf = ctypes.create_string_buffer(need)
            ret = c_uint32(0)
            st = self.ntdll.NtQuerySystemInformation(5, self._spibuf, len(self._spibuf), byref(ret)) & 0xffffffff
            if st == 0: return parse_spi(ctypes.addressof(self._spibuf), ret.value or len(self._spibuf))
            if st == 0xC0000004: need = max(need * 2, ret.value + (256 << 10)); continue     # STATUS_INFO_LENGTH_MISMATCH
            raise OSError("NtQuerySystemInformation 0x%08x" % st)
        raise OSError("process buffer kept growing")

    def exe_path(self, pid):
        if not self.k32: return ""
        h = self.k32.OpenProcess(0x1000, 0, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: return ""
        try:
            buf = ctypes.create_unicode_buffer(1024); n = c_uint32(1024)
            return buf.value if self.k32.QueryFullProcessImageNameW(h, 0, buf, byref(n)) else ""
        finally:
            self.k32.CloseHandle(h)

    def file_description(self, path):
        v = self.ver
        if not v or not path: return ""
        n = v.GetFileVersionInfoSizeW(path, None)
        if not n: return ""
        buf = ctypes.create_string_buffer(n)
        if not v.GetFileVersionInfoW(path, 0, n, buf): return ""
        p = c_void_p(); ln = c_uint32(); langs = []
        if v.VerQueryValueW(buf, "\\VarFileInfo\\Translation", byref(p), byref(ln)) and ln.value >= 4:
            arr = (c_uint16 * (ln.value // 2)).from_address(p.value)
            langs = ["%04x%04x" % (arr[i], arr[i + 1]) for i in range(0, len(arr) - 1, 2)]
        for lc in langs + ["040904b0", "040904e4", "000004b0"]:
            if v.VerQueryValueW(buf, "\\StringFileInfo\\%s\\FileDescription" % lc, byref(p), byref(ln)) and ln.value > 1:
                v = ctypes.wstring_at(p.value, ln.value).split("\0", 1)[0]          # some exes report a padded length
                return "".join(ch for ch in v if ch.isprintable()).strip()
        return ""

    def app_of(self, pid, img, budget):
        """FileDescription for the frontend's friendly label; cached per (pid, image) and per exe path."""
        k = (pid, img); v = self._pid_app.get(k)
        if v is None:
            if budget[0] <= 0: return ""
            budget[0] -= 1; v = ""
            try:
                path = self.exe_path(pid)
                if path:
                    if path not in self._desc: self._desc[path] = self.file_description(path)[:48]
                    v = self._desc[path]
            except Exception: pass
            if v.lower() in (exe_label(img).lower(), img.lower()): v = ""
            if len(self._pid_app) > 4000: self._pid_app.clear()
            if len(self._desc) > 2000: self._desc.clear()
            self._pid_app[k] = v
        return v

    def proc_cmd(self, pid, name=""):
        k = (pid, name); v = self._cmd.get(k)
        if v is None:
            try: v = " ".join(psutil.Process(pid).cmdline())[:200]
            except Exception: v = ""
            if len(self._cmd) > 4000: self._cmd.clear()
            self._cmd[k] = v
        return v

    def pid_name(self, pid):
        img = self.pid_img.get(pid)
        if img is None:
            try: img = psutil.Process(pid).name()
            except Exception: img = str(pid)
        return exe_label(img)

    def sample_procs(self):
        if self._nt_ok:
            try: plist = self.nt_processes()
            except Exception as e:
                log("NtQuerySystemInformation failed (%r) -> psutil process sampler" % e); self._nt_ok = False
        if not self._nt_ok:
            return super().sample_procs()
        t = time.time(); dt = (t - self._pprev_t) if self._pprev_t else None
        cur = {}; rows = []; memrows = []; names = {}
        for pid, img, cpu_t, rss, rd, wr in plist:
            if pid == 0: continue                               # System Idle Process = idle time, not a process
            names[pid] = img; cur[pid] = (cpu_t, rd, wr, img)
            if rss > 64 * MB: memrows.append((rss, pid, img))
            pv = self._pprev.get(pid)
            if pv and dt and pv[3] == img:
                c = max(0.0, cpu_t - pv[0]) / dt * 100
                r = max(0, rd - pv[1]) / dt; w = max(0, wr - pv[2]) / dt
                if c > 0.5 or r + w > 0: rows.append((pid, img, c, r, w, rss))
        self._pprev, self._pprev_t = cur, t
        self.pid_img = names
        budget = [6]                                            # new FileDescription lookups per sample
        def item(pid, img, **kw):
            base = exe_label(img); cmd = ""
            if base.lower() in ("python", "pythonw", "py", "pyw", "node"): cmd = self.proc_cmd(pid, img)
            d = {"pid": pid, "name": nice_name(base, cmd), "comm": img, "app": self.app_of(pid, img, budget), "cmd": cmd[:120], "ctr": ""}
            d.update(kw); return d
        cpu = sorted([x for x in rows if x[2] > 0.5], key=lambda x: -x[2])[:12]
        disk = sorted([x for x in rows if x[3] + x[4] > 0], key=lambda x: -(x[3] + x[4]))[:8]
        memrows.sort(reverse=True)
        top_cpu = [item(p, im, cpu=round(c, 1), mem=rss) for p, im, c, r, w, rss in cpu]
        top_disk = [item(p, im, r=r, w=w) for p, im, c, r, w, rss in disk]
        top_mem = [item(p, im, mem=rss) for rss, p, im in memrows[:10]]
        with self.proc_lock:
            self.top_cpu = top_cpu; self.top_disk = top_disk; self.top_mem = top_mem; self.by_ctr = {}
            self.count = len(names); self.io_denied = 0

    # ---------- sockets (psutil; no per-socket bytes without admin) ----------
    def _conns(self):
        """[(pid, type, laddr, raddr, status)]; system-wide table, else per-process for what we may open."""
        try:
            return [(c.pid, c.type, c.laddr, c.raddr, c.status) for c in psutil.net_connections(kind="inet")]
        except psutil.AccessDenied:
            out = []
            for p in psutil.process_iter():
                try:
                    fn = getattr(p, "net_connections", None) or p.connections
                    out += [(p.pid, c.type, c.laddr, c.raddr, c.status) for c in fn(kind="inet")]
                except Exception: pass
            return out
        except Exception:
            return []

    def sample_net(self):
        conn = {"tcp": 0, "udp": 0, "listen": 0, "est": 0}; flows = []; listen = {}; port_owner = {}; procs = {}
        my_port = str(self.cfg.get("port")); names = {}
        def pname(pid):
            if not pid: return ""
            if pid not in names: names[pid] = self.pid_name(pid)
            return names[pid]
        for pid, typ, la, ra, status in self._conns():
            tcp = typ == socket.SOCK_STREAM
            conn["tcp" if tcp else "udp"] += 1
            if tcp and status == "LISTEN":
                conn["listen"] += 1
                e = listen.setdefault(la.port, {"port": la.port, "hosts": [], "proc": pname(pid), "pid": pid or 0})
                e["hosts"].append(la.ip); continue
            if not ra: continue
            if tcp and status == "ESTABLISHED": conn["est"] += 1
            nm = pname(pid); lip = la.ip[7:] if la.ip.startswith("::ffff:") else la.ip; rip = ra.ip[7:] if ra.ip.startswith("::ffff:") else ra.ip
            if str(ra.port) == my_port: port_owner[str(la.port)] = (pid or 0, nm)
            flows.append({"pid": pid or 0, "name": nm, "local": lip, "lport": str(la.port), "remote": rip,
                          "rport": str(ra.port), "iface": self.iface_of(lip), "rx": 0.0, "tx": 0.0})
            if pid:
                pr = procs.setdefault(pid, {"pid": pid, "name": nm, "rx": 0.0, "tx": 0.0, "n": 0}); pr["n"] += 1
        with self.net_lock:
            self.net = {"procs": sorted(procs.values(), key=lambda x: -x["n"]), "flows": flows, "conn": conn,
                        "listen": listen, "port_owner": port_owner, "updated": time.time()}

    # ---------- GPU: NVML in-process, nvidia-smi in the background ----------
    def _find_smi(self):
        c = [shutil.which("nvidia-smi") or ""]
        for env, sub in (("ProgramW6432", r"NVIDIA Corporation\NVSMI\nvidia-smi.exe"), ("ProgramFiles", r"NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
                         ("SystemRoot", r"System32\nvidia-smi.exe")):
            if os.environ.get(env): c.append(os.path.join(os.environ[env], sub))
        return next((p for p in c if p and os.path.isfile(p)), "nvidia-smi")

    def gpu_sample(self):
        if str(self.cfg["gpu"].get("enabled")) == "false": return {"present": False}
        if self.gpu_h is not None: return self._nvml()
        if not self._smi_thread:                                # first call blocks once (~0.2 s), then a thread refreshes
            self._smi_thread = True; self._smi_last = self.nvidia_smi()
            if self._smi is True: threading.Thread(target=self._smi_loop, daemon=True).start()
        return dict(self._smi_last)

    def _smi_loop(self):
        while True:
            time.sleep(2 if CLIENTS[0] > 0 else 5)
            try: self._smi_last = self.nvidia_smi()
            except Exception: pass

    def _nvml(self):
        h = self.gpu_h
        def g(fn, *a):
            try: return fn(h, *a)
            except Exception: return None
        u = g(NV.nvmlDeviceGetUtilizationRates); temp = g(NV.nvmlDeviceGetTemperature, 0); pw = g(NV.nvmlDeviceGetPowerUsage)
        sm = g(NV.nvmlDeviceGetClockInfo, 1) or g(NV.nvmlDeviceGetClockInfo, 0); ps = g(NV.nvmlDeviceGetPerformanceState)
        mem = g(NV.nvmlDeviceGetMemoryInfo)
        self._gtick += 1
        if self._gtick % 3 == 1:
            seen = {}
            for fn in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
                f = getattr(NV, fn, None)
                for p in (g(f) if f else None) or []:
                    m = getattr(p, "usedGpuMemory", None)                  # None under WDDM (normal on Windows)
                    if p.pid not in seen: seen[p.pid] = {"pid": p.pid, "name": self.pid_name(p.pid), "mem": m, "ctr": ""}
            self.gpu_procs = sorted(seen.values(), key=lambda x: -(x["mem"] or 0))[:16]
        used = sum(p["mem"] or 0 for p in self.gpu_procs)
        return {"present": True, "name": self.gpu_name, "util": u.gpu if u else None, "mem_util": u.memory if u else None,
                "temp": temp, "power": round(pw / 1000, 1) if pw is not None else None, "clock_sm": sm, "pstate": ps,
                "mem_used": mem.used if mem else used, "mem_total": mem.total if mem else None, "mem_unified": mem is None,
                "procs": self.gpu_procs, "src": "NVML"}

    # ---------- sensors ----------
    def temps(self):
        return None                 # no unprivileged sensor API on Windows; GPU temp is in gpu_sample()

    # ---------- disks ----------
    def volumes(self):
        vols = []
        for p in psutil.disk_partitions(all=False):
            if not p.fstype or "cdrom" in (p.opts or ""): continue
            try: u = psutil.disk_usage(p.mountpoint)
            except Exception: continue
            vols.append({"mount": p.mountpoint, "device": p.device, "fs": p.fstype, "total": u.total, "used": u.used, "free": u.free, "pct": u.percent})
        return vols

    # ---------- interfaces / routing ----------
    def adapters(self, force=False):
        t = time.time()
        if self.iphlp and (force or t - self._ad_t > 60):
            self._ad_t = t
            try:
                n = c_uint32(32 << 10)
                for _ in range(4):
                    buf = ctypes.create_string_buffer(n.value)
                    rc = self.iphlp.GetAdaptersAddresses(0, 0x80 | 0x2 | 0x4 | 0x8, None, buf, byref(n))   # +gateways, -any/multicast/dns
                    if rc == 111: continue                                                               # ERROR_BUFFER_OVERFLOW
                    if rc == 0: self._adapters = parse_adapters(ctypes.cast(buf, POINTER(IP_ADAPTER_ADDRESSES)))
                    break
            except Exception as e:
                log("GetAdaptersAddresses failed %r" % e)
        return self._adapters

    def link_speeds(self, stats):
        """psutil's Windows speed is a 32-bit bps field (caps at 4294 Mbps); GetAdaptersAddresses has 64-bit speeds."""
        sp = super().link_speeds(stats)
        for n, a in self.adapters().items():
            if a["speed"] and a["up"]: sp[n] = a["speed"]
        return sp

    def nic_kind(self, n):
        a = self.adapters().get(n)
        if a is None and self.iphlp and time.time() - self._ad_t > 10: a = self.adapters(force=True).get(n)
        return a["kind"] if a else classify_nic(n)

    def default_gateway(self):
        ads = self.adapters(force=True); main = self.main_nic()
        if main in ads and ads[main]["gw"]: return ads[main]["gw"][0]
        for a in ads.values():
            if a["up"] and a["gw"] and a["kind"] in ("wired", "wifi"): return a["gw"][0]
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
        if ":" not in ip and self.iphlp:
            try:
                if self._icmp is None: self._icmp = self.iphlp.IcmpCreateFile() or False
                if self._icmp:
                    data = b"syforce-ping"; size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(data) + 8
                    buf = ctypes.create_string_buffer(size); t0 = time.perf_counter()
                    dst = int.from_bytes(socket.inet_aton(ip), "little")
                    n = self.iphlp.IcmpSendEcho(self._icmp, dst, data, len(data), None, buf, size, int(timeout * 1000))
                    if not n: return None
                    r = ICMP_ECHO_REPLY.from_buffer(buf)
                    if r.Status != 0: return None
                    return float(r.RoundTripTime) if r.RoundTripTime >= 2 else round((time.perf_counter() - t0) * 1000, 2)
            except Exception:
                self._icmp = False
        out = run(["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip], timeout=timeout + 2)
        m = re.search(r"[=<]\s*(\d+)\s*ms", out)
        return float(m.group(1)) if m else None
