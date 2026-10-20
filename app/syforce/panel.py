"""Dashboard self-cost: backend CPU (self + helper subprocesses), RSS, threads, connected tabs and their beacons."""
import os, threading, time
import psutil

SSE_CLIENTS = {}     # handler id -> {"ip","port","cid","since"}
BEACONS = {}         # cid -> beacon dict posted by each tab every 2 s


class PanelMon:
    def __init__(self, collector=None):
        self.col = collector; self.me = psutil.Process(); self.prev = None; self.cache = {}
        self.pids = {os.getpid()}; self.data = {"pids": [os.getpid()]}
        self.my_ips = {"127.0.0.1", "::1"}
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs: self.my_ips.add(a.address.split("%")[0])
        except Exception: pass

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try: self.sample()
            except Exception as e: print("panel err %r" % e)
            time.sleep(3)

    def pinfo(self, pid, role):
        p = self.cache.get(pid); c = None
        try:
            if p is None: p = self.cache[pid] = psutil.Process(pid); p.cpu_percent(None)
            else: c = round(p.cpu_percent(None), 1)
            return {"pid": pid, "name": p.name(), "role": role, "cpu": c, "rss": p.memory_info().rss}
        except Exception:
            self.cache.pop(pid, None); return None

    def sample(self):
        t = time.time(); ct = self.me.cpu_times()
        cur = (t, ct.user + ct.system, getattr(ct, "children_user", 0) + getattr(ct, "children_system", 0))
        backend = {"pid": os.getpid(), "rss": self.me.memory_info().rss, "threads": self.me.num_threads(), "sse": len(SSE_CLIENTS)}
        if self.prev:
            w = max(0.001, cur[0] - self.prev[0])
            backend["cpu_self"] = round((cur[1] - self.prev[1]) / w * 100, 2)
            backend["cpu_children"] = round((cur[2] - self.prev[2]) / w * 100, 2)
            backend["cpu"] = round(backend["cpu_self"] + backend["cpu_children"], 2)
        self.prev = cur
        for k in [k for k, v in BEACONS.items() if t - v["ts"] > 15]: BEACONS.pop(k, None)
        owners = {}
        if self.col is not None:
            with self.col.net_lock: owners = dict(self.col.net.get("port_owner") or {})
        pids = {os.getpid()}; clients = []
        for c in list(SSE_CLIENTS.values()):
            bc = BEACONS.get(c.get("cid") or "", {})
            local = c["ip"] in self.my_ips
            item = {"cid": c.get("cid"), "ip": c["ip"], "local": local, "since": c["since"],
                    "beacon": {k: bc.get(k) for k in ("fps", "frame_ms", "js_ms", "js_pct", "particles", "maxp", "vis", "ua", "w", "h", "mode")} if bc else None}
            if local:
                ow = owners.get(str(c["port"]))
                if ow and ow[0]:
                    pi = self.pinfo(ow[0], "客户端进程")
                    if pi: item["procs"] = [pi]; pids.add(ow[0]); item["cpu"] = pi["cpu"]; item["rss"] = pi["rss"]
            clients.append(item)
        self.pids = pids
        if len(self.cache) > 100: self.cache.clear()
        self.data = {"backend": backend, "clients": clients, "pids": sorted(pids), "t": t}
