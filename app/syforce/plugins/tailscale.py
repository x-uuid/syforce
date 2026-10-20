"""Tailscale plugin (optional; only shown when `tailscale status --json` works). Read-only."""
import json, os, shutil, threading, time
from ..util import run, log

CANDIDATES = ["tailscale", "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale", "/usr/bin/tailscale",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale", r"C:\Program Files\Tailscale\tailscale.exe"]


class Tailscale:
    def __init__(self, cfg, collector):
        self.cfg = cfg; self.col = collector
        self.lock = threading.Lock(); self.data = {"ok": False}; self.prev = {}; self.prev_t = None; self.ips = set()
        conf = cfg["tailscale"]
        self.enabled = str(conf.get("enabled", "auto")).lower() != "false"
        self.bin = conf.get("binary") or next((c for c in CANDIDATES if shutil.which(c) or os.path.exists(c)), "")

    def start(self):
        if self.enabled and self.bin:
            threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try: self.poll()
            except Exception as e: log("tailscale err %r" % e)
            time.sleep(8)

    def poll(self):
        out = run([self.bin, "status", "--json"], timeout=8)
        if not out:
            with self.lock: self.data = {"ok": False}
            return
        d = json.loads(out); t = time.time(); dt = (t - self.prev_t) if self.prev_t else None
        s = d.get("Self", {}); peers = []; cur = {}; exit_node = None
        for k, p in (d.get("Peer") or {}).items():
            rx, tx = p.get("RxBytes", 0), p.get("TxBytes", 0)
            cur[k] = (rx, tx); pv = self.prev.get(k); rr = tr = 0.0
            if pv and dt: rr = max(0, rx - pv[0]) / dt; tr = max(0, tx - pv[1]) / dt
            name = p.get("HostName") or ""; dns = (p.get("DNSName") or "").rstrip(".")
            if name in ("localhost", "") and dns: name = dns.split(".")[0]
            item = {"id": k[:12], "name": name, "dns": dns, "ip": (p.get("TailscaleIPs") or [""])[0], "ips": p.get("TailscaleIPs") or [],
                    "os": p.get("OS", ""), "online": bool(p.get("Online")), "active": bool(p.get("Active")),
                    "exit": bool(p.get("ExitNode")), "exit_opt": bool(p.get("ExitNodeOption")),
                    "cur": p.get("CurAddr", ""), "relay": p.get("Relay", ""), "rx": rr, "tx": tr}
            if item["exit"]: exit_node = item["name"]
            peers.append(item)
        peers.sort(key=lambda x: (not x["exit"], not x["online"], -(x["rx"] + x["tx"]), x["name"]))
        self.prev, self.prev_t = cur, t
        ips = s.get("TailscaleIPs") or []; self.ips = set(ips); self.col.tail_ips = self.ips
        with self.lock:
            self.data = {"ok": True, "state": d.get("BackendState"),
                         "self": {"name": s.get("HostName"), "ips": ips},
                         "exit_node": exit_node, "peers": peers, "updated": t}

    def snapshot(self):
        with self.lock: return self.data
