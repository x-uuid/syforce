"""Topology plugin: default gateway + optional first private traceroute hops + latency pings.

Hops are labelled generically (Router, Router 2, ...) in the page; give them names via config "names": {"<ip>": "label"}.
Only private-range hops are kept (your own network); public ISP hops are folded into the Internet node.
"""
import threading, time
from ..util import log, is_private


class Topology:
    def __init__(self, cfg, collector):
        self.cfg = cfg; self.col = collector
        self.hops = []           # [{"ip", "name", "ms"}]
        self.pings = {}          # ip -> {"name","ip","ms","role","ts"}
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self.discover_loop, daemon=True).start()
        threading.Thread(target=self.ping_loop, daemon=True).start()

    def discover(self):
        t = self.cfg["topology"]
        gw = t.get("gateway") or "auto"
        if gw == "auto": gw = self.col.default_gateway()
        hops = [gw] if gw else []
        n = int(t.get("traceroute_hops") or 0)
        if n > 1:
            try:
                for ip in self.col.traceroute(t.get("traceroute_target") or "1.1.1.1", n + 1):
                    if ip not in hops and is_private(ip) and len(hops) < n: hops.append(ip)
            except Exception as e:
                log("traceroute err %r" % e)
        names = self.cfg.get("names") or {}
        with self.lock:
            old = {h["ip"]: h for h in self.hops}
            self.hops = [{"ip": ip, "name": names.get(ip, ""), "ms": (old.get(ip) or {}).get("ms")} for ip in hops]

    def discover_loop(self):
        while True:
            try: self.discover()
            except Exception as e: log("topology err %r" % e)
            time.sleep(max(60, int(self.cfg["topology"].get("refresh_s") or 600)))

    def targets(self):
        with self.lock: hops = list(self.hops)
        out = [(h["name"] or "", h["ip"], "hop%d" % i) for i, h in enumerate(hops)]
        for p in self.cfg.get("ping_targets") or []:
            if p.get("ip"): out.append((p.get("name") or p["ip"], p["ip"], p.get("role") or "inet"))
        return out

    def ping_loop(self):
        time.sleep(1)
        while True:
            for name, ip, role in self.targets():
                ms = self.col.ping(ip)
                self.pings[ip] = {"name": name, "ip": ip, "ms": ms, "role": role, "ts": time.time()}
                with self.lock:
                    for h in self.hops:
                        if h["ip"] == ip: h["ms"] = ms
            live = {ip for _, ip, _ in self.targets()}
            for ip in list(self.pings):
                if ip not in live: self.pings.pop(ip, None)
            time.sleep(5)

    def snapshot(self):
        with self.lock: hops = [dict(h) for h in self.hops]
        return hops, list(self.pings.values())
