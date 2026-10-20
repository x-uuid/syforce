"""Config loading: defaults <- config.json (or --config path) <- CLI flags. Hide list is hot-reloaded."""
import json, os, re, time

DEFAULTS = {
    "port": 9001,
    "bind": "0.0.0.0",
    "title": "",                      # top-left title; "" = short hostname
    "host_label": "",                 # label of the centre node; "" = short hostname
    "language": "zh",                 # zh | en | ja (default when the browser has no saved choice)
    "fps_mode": "eco",                # eco (节能) | smooth (流畅)
    "names": {},                      # {"<ip or tailscale name>": "label"}  e.g. {"192.168.1.1": "Router"}
    "hide": [],                       # names or IPs never sent to the browser (still collected)
    "ping_targets": [{"name": "DNS", "ip": "1.1.1.1", "role": "inet"}],
    "topology": {"gateway": "auto", "traceroute_hops": 2, "traceroute_target": "1.1.1.1", "refresh_s": 600},
    "tailscale": {"enabled": "auto", "binary": ""},
    "models": {"enabled": "auto", "ports": [8000, 8001, 8002, 11434], "autodiscover": True,
               "manager_port": None, "skip_ports": [22, 53, 631]},
    "docker": {"enabled": "auto"},
    "gpu": {"enabled": "auto"},
}

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path=None):
        self.path = path or os.path.join(HERE, "config.json")
        self.data = dict(DEFAULTS); self.mtime = None; self.chk = 0
        self.hide_names = set(); self.hide_ips = set()
        self.reload(force=True)

    def reload(self, force=False):
        t = time.time()
        if not force and t - self.chk < 2: return
        self.chk = t
        try: mt = os.stat(self.path).st_mtime
        except OSError: mt = 0
        if mt == self.mtime and not force: return
        self.mtime = mt
        user = {}
        if mt:
            try:
                with open(self.path, encoding="utf-8") as f: user = json.load(f)
            except Exception as e:
                print("config parse error %r (using defaults)" % e)
        self.data = _merge(DEFAULTS, user)
        self.hide_names = {str(x).strip().lower() for x in self.data.get("hide") or [] if str(x).strip()}

    def __getitem__(self, k): return self.data[k]
    def get(self, k, d=None): return self.data.get(k, d)

    # ---- hide list ----
    def name_hit(self, v):
        v = (v or "").lower().rstrip(".")
        return bool(v) and any(v == n or v.startswith(n + ".") for n in self.hide_names)

    def resolve_hidden(self, peers):
        ips = {n for n in self.hide_names if re.match(r"^[\d.:a-f]+$", n) and any(c.isdigit() for c in n)}
        for p in peers or []:
            if self.name_hit(p.get("name")) or self.name_hit(p.get("dns")) or (p.get("ip") or "").lower() in self.hide_names:
                ips |= set(p.get("ips") or [p.get("ip")])
        ips.discard(None); ips.discard("")
        self.hide_ips = ips

    def ip_hit(self, ip): return bool(ip) and ip in self.hide_ips

    def text_hit(self, txt):
        if not self.hide_names and not self.hide_ips: return False
        low = txt.lower()
        return any(re.search(r"(^|[^\w.-])" + re.escape(x) + r"($|[^\w-])", low) for x in self.hide_names | self.hide_ips)

    def public(self):
        """subset the browser is allowed to see (/api/config)"""
        return {"language": self.data.get("language"), "fps_mode": self.data.get("fps_mode"),
                "title": self.data.get("title"), "host_label": self.data.get("host_label")}
