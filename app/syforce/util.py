"""Shared helpers: subprocess runner with cost accounting, HTTP GET, event bus, alerts, spike detector, rDNS."""
import json, math, os, re, socket, subprocess, sys, threading, time, urllib.request, urllib.error
from collections import deque
from datetime import datetime

MB = 1024 * 1024
CLIENTS = [0]            # number of connected SSE viewers (samplers slow down when 0)
CMD_COST = {}            # "cmd arg0" -> [count, wall seconds]   exposed at /api/cost


def log(msg):
    sys.stdout.write(msg + "\n"); sys.stdout.flush()


def now_str(fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.now().astimezone().strftime(fmt)


def run(cmd, timeout=5):
    """Run a read-only helper command; never raises, returns stdout ('' on failure)."""
    t = time.time()
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw).stdout
    except Exception:
        return ""
    finally:
        c = CMD_COST.setdefault(os.path.basename(cmd[0]) + " " + (cmd[1] if len(cmd) > 1 else ""), [0, 0.0])
        c[0] += 1; c[1] += time.time() - t


def http_get(url, timeout=2.0, limit=4 * MB):
    """GET only. Returns (status or None, body)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "syforce/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return None, ""


def fmt_rate(b):
    if b >= MB: return "%.1f MB/s" % (b / MB)
    if b >= 1024: return "%.0f KB/s" % (b / 1024)
    return "%d B/s" % b


def classify(ip):
    """lan / tailnet / docker / local / internet (by address range only)."""
    ip = ip or ""
    if ip.startswith("::ffff:"): ip = ip[7:]
    if ip.startswith("100."):
        try:
            if 64 <= int(ip.split(".")[1]) <= 127: return "tailnet"   # CGNAT range used by Tailscale
        except Exception: pass
    low = ip.lower()
    if low.startswith("fd7a:115c:a1e0"): return "tailnet"
    if ip.startswith("172.17.") or ip.startswith("172.18."): return "docker"
    if ip.startswith(("192.168.", "10.", "169.254.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip): return "lan"
    if low.startswith(("fe80", "fd", "fc")): return "lan"
    if ip.startswith("127.") or ip in ("::1", "0.0.0.0", "::"): return "local"
    return "internet"


def is_private(ip):
    return classify(ip) == "lan"


class Detector:
    """EWMA mean/variance spike detector: fires once when x is far above its own baseline."""
    def __init__(self, floor, k=4.0, ratio=2.0, sustain=1, alpha=0.03, warm=15):
        self.floor, self.k, self.ratio, self.sustain, self.alpha, self.warm = floor, k, ratio, sustain, alpha, warm
        self.mean = None; self.var = 0.0; self.n = 0; self.hot = 0

    def update(self, x):
        fire = False
        if self.mean is None:
            self.mean = x
        else:
            std = math.sqrt(self.var)
            if self.n >= self.warm and x >= self.floor and x > self.mean + self.k * std and x > self.mean * self.ratio:
                self.hot += 1
            elif x >= self.floor and self.hot and x > self.mean * 1.3:
                self.hot += 1
            else:
                self.hot = 0
            fire = self.hot == self.sustain
            a = self.alpha * (0.25 if self.hot else 1.0)
            d = x - self.mean
            self.mean += a * d
            self.var = (1 - a) * (self.var + a * d * d)
        self.n += 1
        return fire


class Bus:
    """In-process pub/sub feeding every SSE connection."""
    def __init__(self):
        self.cond = threading.Condition(); self.events = deque(maxlen=50); self.seq = 0

    def publish(self, typ, data):
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, typ, json.dumps(data, separators=(",", ":"), ensure_ascii=False)))
            self.cond.notify_all()


BUS = Bus()


class Alerts:
    """Alert list with per-key cooldown. Texts are terse (subject + value), localised in the page."""
    def __init__(self):
        self.list = deque(maxlen=200); self.cool = {}; self.lock = threading.Lock(); self.seq = 0
        self.hidden = lambda txt: False      # replaced by server with the hide-list matcher

    @staticmethod
    def text(code, subj, val):
        pre = {"cpu": "CPU", "gpu": "GPU", "gputemp": "GPU", "disk_r": "Disk R", "disk_w": "Disk W", "memlow": "RAM", "mempsi": ""}
        if code in pre and not subj: return (pre[code] + " " + val).strip()
        if code in ("cpu", "disk_r", "disk_w") and subj: return ("%s %s · %s" % (pre[code], val, subj)).strip()
        return ("%s %s" % (subj, val)).strip()

    def push(self, key, level, code, subj, val, cooldown=60, kind="sys", ts=None):
        t = ts or time.time()
        if t - self.cool.get(key, 0) < cooldown: return None
        self.cool[key] = t
        txt = self.text(code, subj, val)
        if self.hidden(txt + " " + key): return None
        with self.lock:
            self.seq += 1
            ev = {"id": self.seq, "ts": t, "time": datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                  "key": key, "level": level, "code": code, "subj": subj, "val": val, "kind": kind,
                  "text": txt, "title": txt, "detail": ""}
            self.list.append(ev)
        log("ALERT %s %s" % (ev["time"], txt))
        BUS.publish("alert", ev)
        return ev


ALERTS = Alerts()


class RDNS:
    """Background reverse-DNS cache (never blocks the sampler)."""
    def __init__(self):
        self.cache = {}; self.q = deque(); self.started = False

    def get(self, ip):
        if not self.started:
            self.started = True
            threading.Thread(target=self.loop, daemon=True).start()
        v = self.cache.get(ip)
        if v is None and ip not in self.q and len(self.q) < 200: self.q.append(ip)
        return v or ""

    def loop(self):
        socket.setdefaulttimeout(2)
        while True:
            if not self.q: time.sleep(0.5); continue
            ip = self.q.popleft()
            try: n = socket.gethostbyaddr(ip)[0]
            except Exception: n = "-"
            self.cache[ip] = n
            if len(self.cache) > 3000: self.cache.clear()


RDNS_ = RDNS()


def split_hp(a):
    """'[::1]:80' / '1.2.3.4:80' -> (host, port)"""
    h, _, p = a.rpartition(":")
    h = h.strip("[]").split("%")[0]
    if h.startswith("::ffff:"): h = h[7:]
    return h, p
