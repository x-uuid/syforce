"""Docker plugin (optional): container names / images / published ports for model attribution. `docker ps` only (read-only)."""
import re, shutil, threading, time
from datetime import datetime
from ..util import run, log


class Docker:
    def __init__(self, cfg):
        self.lock = threading.Lock(); self.ctrs = {}; self.port2ctr = {}; self.started = {}; self.ok = False
        self.enabled = str(cfg["docker"].get("enabled", "auto")).lower() != "false" and bool(shutil.which("docker"))

    def start(self):
        if self.enabled: threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try: self.poll()
            except Exception as e: log("docker err %r" % e)
            time.sleep(30)

    def poll(self):
        out = run(["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"], timeout=8)
        if not out: self.ok = False; return
        ctrs = {}; p2c = {}
        for line in out.splitlines():
            sp = line.split("\t")
            if len(sp) < 5: continue
            cid, name, image, ports, status = sp
            c = {"id": cid[:12], "name": name, "image": image, "status": status, "ports": []}
            for m in re.finditer(r"(?:[\d.]+|\[::\]):(\d+)->(\d+)/tcp", ports):
                hp = int(m.group(1)); c.setdefault("cport", {})[hp] = int(m.group(2))
                if hp not in c["ports"]: c["ports"].append(hp)
                p2c[hp] = cid
            ctrs[cid] = c
        new = [c for c in ctrs if c not in self.started]
        if new:
            for line in run(["docker", "inspect", "-f", "{{.Id}} {{.State.StartedAt}} {{.State.Pid}}"] + new, timeout=8).splitlines():
                sp = line.split()
                if len(sp) >= 3:
                    try: ts = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", sp[1]).replace("Z", "+00:00")).timestamp()
                    except Exception: ts = None
                    self.started[sp[0]] = (ts, int(sp[2]))
        with self.lock:
            self.ctrs = ctrs; self.port2ctr = p2c; self.ok = True
