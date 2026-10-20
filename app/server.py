#!/usr/bin/env python3
"""syforce — OS force-directed graph. Real-time system topology dashboard, one process, SSE, no build step.

    python server.py                 # real collectors for this OS (macOS / Linux / Windows)
    python server.py --port 9001     # choose port (default from config.json or 9001)
    python server.py --demo          # synthetic data, no collectors, safe for screenshots
    python server.py --config my.json
    python server.py --log syforce.log   # append output to a file (automatic under pythonw)

Read-only by design: never controls model servers, containers or services.
"""
import argparse, json, os, socket, sys, threading, time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from syforce.util import log, now_str, MB, CLIENTS, CMD_COST, BUS, ALERTS, Detector, RDNS_, classify, fmt_rate
from syforce.config import Config
from syforce.panel import PanelMon, SSE_CLIENTS, BEACONS

STATIC = os.path.join(HERE, "static")
HIST_LEN = 300
HIST = {k: deque(maxlen=HIST_LEN) for k in ["t", "cpu", "mem", "gpu", "disk_r", "disk_w", "net_rx", "net_tx", "tail_rx", "tail_tx", "tok"]}
STATE = {"snap": None}
CFG = None


def hist_push(snap):
    tu = next((x for x in snap["nics"] if x.get("tail")), {"rx": 0, "tx": 0})
    for k, v in (("t", int(snap["t"])), ("cpu", snap["cpu"]["total"]), ("mem", snap["mem"]["pct"]), ("gpu", snap["gpu"].get("util") or 0),
                 ("disk_r", round(snap["disk"]["r"])), ("disk_w", round(snap["disk"]["w"])), ("net_rx", round(snap["net"]["rx"])),
                 ("net_tx", round(snap["net"]["tx"])), ("tail_rx", round(tu["rx"])), ("tail_tx", round(tu["tx"])), ("tok", snap.get("tok") or 0)):
        HIST[k].append(v)


def apply_hide(snap):
    """display-only filter: hidden names/IPs never leave the server (they are still collected)."""
    tail = snap.get("tail") or {}
    CFG.resolve_hidden(tail.get("peers") if tail.get("ok") else [])
    if not CFG.hide_names: return snap
    if tail.get("ok"):
        tail = dict(tail); tail["peers"] = [p for p in tail.get("peers", []) if not (CFG.name_hit(p.get("name")) or CFG.name_hit(p.get("dns")) or CFG.ip_hit(p.get("ip")))]
        if tail.get("exit_node") and CFG.name_hit(tail["exit_node"]): tail["exit_node"] = None
        snap["tail"] = tail
    net = dict(snap["net"]); net["hosts"] = [h for h in net["hosts"] if not (CFG.ip_hit(h["ip"]) or CFG.name_hit(h.get("host")))]; snap["net"] = net
    snap["models"] = [dict(m, clients=[c for c in (m.get("clients") or []) if not CFG.ip_hit(c["ip"])]) for m in snap.get("models") or []]
    snap["ping"] = [p for p in snap["ping"] if not (CFG.ip_hit(p["ip"]) or CFG.name_hit(p.get("name")))]
    return snap


def finalize(snap):
    snap = apply_hide(snap)
    hist_push(snap)
    STATE["snap"] = snap
    BUS.publish("snap", snap)


# ---------------------------------------------------------------- real sampler
def real_sampler():
    import psutil
    from syforce.collectors import get_collector
    from syforce.plugins.tailscale import Tailscale
    from syforce.plugins.docker import Docker
    from syforce.plugins.models import Models
    from syforce.plugins.topology import Topology

    col = get_collector(CFG); col.start()
    tail = Tailscale(CFG, col); tail.start()
    docker = Docker(CFG); docker.start()
    models = Models(CFG, col, docker); models.start()
    topo = Topology(CFG, col); topo.start()
    panel = PanelMon(col); panel.start()
    plat = col.platform_label()
    log("syforce collector=%s cpu=%s gpu=%s tailscale=%s docker=%s" % (col.os_name, "".join(col.cpu_labels), col.gpu_sample().get("name", "-"), bool(tail.bin), docker.enabled))

    DET = {}
    def det(key, **kw):
        d = DET.get(key)
        if d is None: d = DET[key] = Detector(**kw)
        return d

    psutil.cpu_percent(percpu=True)
    prev_net = psutil.net_io_counters(pernic=True); prev_disk = psutil.disk_io_counters(perdisk=True) or {}
    prev_t = time.time(); tick = 0; vols = col.volumes(); nic_ips = {}; nic_speed = {}; nic_up = set(); talkers = {}; ns_seen = 0; power = {}
    ov_prev = None; overhead = {"self": None, "children": None, "total": None}; me = psutil.Process()
    time.sleep(1)
    while True:
        t0 = time.time()
        try:
            CFG.reload()
            if tick % 10 == 0:
                ct = me.cpu_times(); cur = (t0, ct.user + ct.system, getattr(ct, "children_user", 0) + getattr(ct, "children_system", 0))
                if ov_prev:
                    w = cur[0] - ov_prev[0]
                    overhead = {"self": round((cur[1] - ov_prev[1]) / w * 100, 2), "children": round((cur[2] - ov_prev[2]) / w * 100, 2)}
                    overhead["total"] = round(overhead["self"] + overhead["children"], 2)
                ov_prev = cur
            dt = max(0.2, t0 - prev_t); prev_t = t0
            percpu = psutil.cpu_percent(percpu=True); cpu_total = sum(percpu) / max(1, len(percpu))
            vm = psutil.virtual_memory(); sw = psutil.swap_memory(); mx = col.mem_extra(vm)
            gpu = col.gpu_sample() if str(CFG["gpu"].get("enabled")) != "false" else {"present": False}
            gpu.setdefault("procs", []); gpu["device"] = gpu.get("util")
            if tick % 3 == 1: col.sample_procs()
            if tick % 15 == 0: vols = col.volumes()
            freqs = col.cpu_freqs() if tick % 2 == 0 else None
            main = col.main_nic()
            if tick % 15 == 0:
                try:
                    st = psutil.net_if_stats(); nic_ips.clear()
                    nic_ips.update({n: next((a.address for a in ad if a.family == socket.AF_INET), "") for n, ad in psutil.net_if_addrs().items() if st.get(n) and st[n].isup})
                    nic_speed.clear(); nic_speed.update(col.link_speeds(st)); nic_up.clear(); nic_up.update(n for n, x in st.items() if x.isup)
                except Exception: pass
                power = {"battery": col.battery(), "temps": col.temps()}
            net = psutil.net_io_counters(pernic=True); nics = []
            for n, c in net.items():
                p = prev_net.get(n)
                if not p: continue
                kind = "tail" if (n == main and False) else col.nic_kind(n)
                if nic_ips.get(n) and nic_ips[n] in col.tail_ips: kind = "tail"
                rx = max(0, c.bytes_recv - p.bytes_recv) / dt; tx = max(0, c.bytes_sent - p.bytes_sent) / dt
                if kind == "lo" or (kind == "virtual" and n != main and rx + tx < 2048): continue
                if kind == "other" and n != main and not nic_ips.get(n) and rx + tx < 2048: continue
                if nic_up and n not in nic_up and n != main and rx + tx < 2048: continue    # unplugged NIC / Wi-Fi off
                nics.append({"name": n, "kind": kind, "rx": rx, "tx": tx, "ip": nic_ips.get(n, ""), "speed": nic_speed.get(n), "main": n == main, "tail": kind == "tail", "wifi": kind == "wifi"})
            prev_net = net
            nics.sort(key=lambda x: (not x["main"], {"wired": 1, "wifi": 2, "tail": 3}.get(x["kind"], 4), x["name"]))
            dk = psutil.disk_io_counters(perdisk=True) or {}; disks = []
            for n, c in dk.items():
                if not col.real_disk(n): continue
                p = prev_disk.get(n)
                if not p: continue
                disks.append({"name": n, "r": max(0, c.read_bytes - p.read_bytes) / dt, "w": max(0, c.write_bytes - p.write_bytes) / dt,
                              "riops": max(0, c.read_count - p.read_count) / dt, "wiops": max(0, c.write_count - p.write_count) / dt})
            prev_disk = dk
            dr = sum(d["r"] for d in disks); dw = sum(d["w"] for d in disks)
            mn = next((x for x in nics if x["main"]), {"rx": 0, "tx": 0})
            with col.net_lock:
                nn = col.net; nprocs = [dict(p) for p in nn["procs"][:12]]; flows = list(nn["flows"]); nconn = dict(nn["conn"]); nupd = nn["updated"]
            for p in nprocs:
                if p.get("pid") and not p.get("cmd"): p["cmd"] = col.proc_cmd(p["pid"], p["name"])[:120]
            names = CFG.get("names") or {}
            hosts = {}
            for f in flows:
                cls = classify(f["remote"])
                if cls in ("local", "docker"): continue
                h = hosts.get(f["remote"])
                if h is None: h = hosts[f["remote"]] = {"ip": f["remote"], "cls": cls, "rx": 0.0, "tx": 0.0, "n": 0, "procs": {}, "iface": f["iface"], "ports": set()}
                h["rx"] += f["rx"]; h["tx"] += f["tx"]; h["n"] += 1
                pn = f["name"] or (":" + f["lport"] if int(f["lport"] or 0) < 32768 else "-")
                h["procs"][pn] = h["procs"].get(pn, 0) + f["rx"] + f["tx"] + 1
                h["ports"].add(f["lport"] if int(f["lport"] or 0) < 32768 else f["rport"])
            top_hosts = []
            for h in sorted(hosts.values(), key=lambda x: (-(x["rx"] + x["tx"]), -x["n"]))[:16]:
                top_hosts.append({"ip": h["ip"], "cls": h["cls"], "rx": h["rx"], "tx": h["tx"], "n": h["n"], "iface": h["iface"],
                                  "host": names.get(h["ip"]) or (RDNS_.get(h["ip"]) if h["cls"] == "internet" else ""),
                                  "procs": [p for p, _ in sorted(h["procs"].items(), key=lambda kv: -kv[1])[:3]],
                                  "ports": sorted(h["ports"], key=lambda x: int(x or 0))[:4]})
            with col.proc_lock: top_cpu = list(col.top_cpu); top_disk = list(col.top_disk); top_mem = list(col.top_mem); pcount = col.count; io_denied = col.io_denied
            ms = models.snapshot(); tok = sum(m.get("gen_tps") or 0 for m in ms)
            hops, pings = topo.snapshot()
            ts = tail.snapshot()
            if ts.get("ok") and names:
                ts = dict(ts); ts["peers"] = [dict(p, name=names.get(p["name"], names.get(p["ip"], p["name"]))) for p in ts["peers"]]
            # ---- alerts ----
            gname = gpu.get("name") or "GPU"
            for i, v in enumerate(percpu):
                if det("core%d" % i, floor=85, k=3, ratio=1.5, sustain=4, alpha=0.05).update(v):
                    ALERTS.push("core%d" % i, "warn", "core", "CPU%d" % i, "%.0f%%" % v, 120, "cpu")
            if det("cpu", floor=50, k=4, ratio=1.8, sustain=2).update(cpu_total):
                tp = [p for p in top_cpu if p["pid"] not in panel.pids]
                ALERTS.push("cpu", "warn", "cpu", tp[0]["name"] if tp else "", "%.0f%%" % cpu_total, 60, "cpu")
            gu = gpu.get("util") or 0
            if gpu.get("present") and det("gpu", floor=50, k=4, ratio=1.8).update(gu):
                ALERTS.push("gpu", "warn", "gpu", "", "%d%%" % gu, 60, "gpu")
            if (gpu.get("temp") or 0) >= 85:
                ALERTS.push("gputemp", "crit", "gputemp", "", "%s°C" % gpu.get("temp"), 300, "gpu")
            for x in nics:
                for dirn, val in (("rx", x["rx"]), ("tx", x["tx"])):
                    if det("nic_%s_%s" % (x["name"], dirn), floor=20 * MB, k=5, ratio=3).update(val):
                        ALERTS.push("nic_%s_%s" % (x["name"], dirn), "crit", "nic", x["name"], ("↓ " if dirn == "rx" else "↑ ") + fmt_rate(val), 45, "net")
            for nm, val in (("disk_r", dr), ("disk_w", dw)):
                if det(nm, floor=150 * MB, k=5, ratio=3).update(val):
                    td = [p for p in top_disk if p["pid"] not in panel.pids and (p["r"] if nm == "disk_r" else p["w"]) >= 0.3 * val]
                    ALERTS.push(nm, "warn", nm, td[0]["name"] if td else "", fmt_rate(val), 45, "disk")
            if mx["pressure"] >= 2:
                ALERTS.push("mempsi", "crit" if mx["pressure"] >= 4 else "warn", "mempsi", "", ("PSI %.1f%%" % mx["psi"]) if mx["psi"] is not None else "L%d" % mx["pressure"], 300, "mem")
            if nupd != ns_seen:
                ns_seen = nupd
                for pr in [p for p in nprocs if p.get("pid") and p["pid"] not in panel.pids][:3]:
                    rate = pr["rx"] + pr["tx"]
                    if rate > 5 * MB and t0 - talkers.get(pr["name"], 0) > 120:
                        ALERTS.push("talker_" + pr["name"], "info", "talker", pr["name"], "↓%s ↑%s" % (fmt_rate(pr["rx"]), fmt_rate(pr["tx"])), 120, "net")
                    if rate > MB: talkers[pr["name"]] = t0
            for p in pings:
                if p["ms"] is not None and det("ping_" + p["ip"], floor=80 if p["role"] != "inet" else 150, k=5, ratio=3, sustain=2).update(p["ms"]):
                    ALERTS.push("ping_" + p["ip"], "warn", "ping", p["name"] or p["ip"], "%.0f ms" % p["ms"], 120, "net")
            host = socket.gethostname().split(".")[0]
            snap = {
                "t": t0, "time": now_str(), "host": CFG["host_label"] or host, "title": CFG["title"] or "syforce", "platform": plat, "os": col.os_name,
                "features": {"gpu": bool(gpu.get("present")), "models": bool(ms), "tail": bool(ts.get("ok")),
                             "wifi": any(x["wifi"] for x in nics), "docker": docker.ok, "battery": bool(power.get("battery")), "demo": False},
                "cpu": {"total": round(cpu_total, 1), "per": [round(v) for v in percpu], "labels": col.cpu_labels, "kinds": col.cpu_kinds,
                        "load": [round(x, 2) for x in col.loadavg()], "n": col.ncpu, "freq": freqs},
                "mem": {"total": vm.total, "used": vm.total - vm.available, "avail": vm.available, "pct": vm.percent,
                        "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0), "swap_used": sw.used, "swap_total": sw.total,
                        "psi": mx["psi"], "pressure": mx["pressure"]},
                "gpu": gpu,
                "disk": {"r": dr, "w": dw, "disks": disks, "vols": vols, "top": top_disk, "io_denied": io_denied},
                "nics": nics, "main_nic": main,
                "net": {"rx": mn["rx"], "tx": mn["tx"], "procs": nprocs, "hosts": top_hosts, "conn": nconn, "age": round(t0 - nupd, 1) if nupd else None},
                "procs": {"cpu": top_cpu, "mem": top_mem, "count": pcount},
                "models": ms, "manager": models.manager_info(), "tok": round(tok, 1),
                "tail": ts, "topo": {"hops": hops}, "ping": pings, "battery": power.get("battery"), "temps": power.get("temps"),
                "overhead": overhead, "clients": CLIENTS[0], "panel": panel.data,
            }
            finalize(snap)
        except Exception:
            import traceback; traceback.print_exc()
        tick += 1
        time.sleep(max(0.05, (1.0 if CLIENTS[0] > 0 else 2.0) - (time.time() - t0)))


# ---------------------------------------------------------------- HTTP
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".png": "image/png", ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def send_bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache"); self.end_headers(); self.wfile.write(body)

    def send_json(self, obj):
        self.send_bytes(200, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events": return self.sse()
        if path == "/api/state": return self.send_json(STATE["snap"])
        if path == "/api/config": return self.send_json(CFG.public())
        if path == "/api/history":
            h = {k: list(v) for k, v in HIST.items()}
            h["alerts"] = [a for a in ALERTS.list if not CFG.text_hit(a.get("text", "") + " " + a.get("key", ""))]
            return self.send_json(h)
        if path == "/api/cost": return self.send_json({k: [v[0], round(v[1], 2)] for k, v in CMD_COST.items()})
        if path == "/healthz": return self.send_bytes(200, b"ok", "text/plain")
        if path == "/": path = "/index.html"
        fp = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not fp.startswith(STATIC) or not os.path.isfile(fp): return self.send_bytes(404, b"not found", "text/plain")
        with open(fp, "rb") as f: body = f.read()
        self.send_bytes(200, body, MIME.get(os.path.splitext(fp)[1], "application/octet-stream"))

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/beacon": return self.send_bytes(404, b"not found", "text/plain")
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 8192)
            d = json.loads(self.rfile.read(n) or b"{}"); cid = str(d.get("cid", ""))[:40]
            if cid:
                rec = {"ts": time.time(), "ip": self.client_address[0]}
                for k in ("fps", "frame_ms", "js_ms", "js_pct", "particles", "maxp", "w", "h"):
                    v = d.get(k)
                    if isinstance(v, (int, float)): rec[k] = round(float(v), 2)
                rec["vis"] = str(d.get("vis", ""))[:12]; rec["ua"] = str(d.get("ua", ""))[:60]; rec["mode"] = str(d.get("mode", ""))[:8]
                BEACONS[cid] = rec
                if len(BEACONS) > 50: BEACONS.pop(next(iter(BEACONS)))
        except Exception: pass
        self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()

    def sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive"); self.send_header("X-Accel-Buffering", "no"); self.end_headers()
        last = BUS.seq; CLIENTS[0] += 1
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        cid = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("cid", "")[:40]
        ip = self.client_address[0]
        if ip.startswith("::ffff:"): ip = ip[7:]
        SSE_CLIENTS[id(self)] = {"ip": ip, "port": self.client_address[1], "cid": cid, "since": time.time()}
        try:
            if STATE["snap"]:
                self.wfile.write(("event: snap\ndata: %s\n\n" % json.dumps(STATE["snap"], ensure_ascii=False)).encode()); self.wfile.flush()
            while True:
                with BUS.cond:
                    BUS.cond.wait_for(lambda: BUS.seq > last, timeout=15)
                    evs = [e for e in BUS.events if e[0] > last]
                if not evs:
                    self.wfile.write(b": ping\n\n"); self.wfile.flush(); continue
                lastsnap = None; out = []
                for seq, typ, data in evs:
                    if typ == "snap": lastsnap = (typ, data)
                    else: out.append((typ, data))
                if lastsnap: out.append(lastsnap)
                last = evs[-1][0]
                self.wfile.write("".join("event: %s\ndata: %s\n\n" % (t, d) for t, d in out).encode()); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            CLIENTS[0] -= 1; SSE_CLIENTS.pop(id(self), None)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"     # on Windows SO_REUSEADDR lets a 2nd instance bind the same port

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionError, TimeoutError)): return     # incl. WinError 10053 ConnectionAbortedError
        super().handle_error(request, client_address)


def main():
    global CFG
    ap = argparse.ArgumentParser(description="syforce — OS force-directed graph")
    ap.add_argument("--port", type=int, default=None, help="HTTP port (default: config or 9001)")
    ap.add_argument("--bind", default=None, help="bind address (default: config or 0.0.0.0)")
    ap.add_argument("--config", default=os.environ.get("SYFORCE_CONFIG"), help="path to config.json")
    ap.add_argument("--demo", action="store_true", help="synthetic demo data (no collectors)")
    ap.add_argument("--log", default=os.environ.get("SYFORCE_LOG"), help="append stdout/stderr to this file")
    a = ap.parse_args()
    if a.log or sys.stdout is None or sys.stderr is None:      # pythonw has no console: keep logs instead of crashing
        lp = a.log or os.path.join(HERE, "syforce.log")
        try:
            if os.path.getsize(lp) > 5 * MB: os.replace(lp, lp + ".1")
        except OSError: pass
        sys.stdout = sys.stderr = open(lp, "a", encoding="utf-8", buffering=1, errors="replace")
    CFG = Config(a.config)
    if a.port: CFG.data["port"] = a.port
    elif os.environ.get("SYFORCE_PORT"): CFG.data["port"] = int(os.environ["SYFORCE_PORT"])
    if a.bind: CFG.data["bind"] = a.bind
    ALERTS.hidden = CFG.text_hit
    port, bind = int(CFG["port"]), CFG["bind"]
    if a.demo:
        from syforce.demo import demo_sampler
        target = lambda: demo_sampler(CFG, finalize, HIST)
    else:
        target = real_sampler
    threading.Thread(target=target, daemon=True).start()
    log("syforce %s on http://%s:%d/" % ("DEMO" if a.demo else "live", bind, port))
    Server.address_family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    try:
        Server((bind, port), H).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
