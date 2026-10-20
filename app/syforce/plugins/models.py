"""Model-endpoint plugin (optional): read-only probes of OpenAI-compatible servers (vLLM / SGLang / llama.cpp / Ollama).

SAFETY: only HTTP GET of /v1/models, /health, /metrics (and /api/tags for Ollama, optional manager /api/status).
Never POST, never load/unload, never restart, never touch containers, caches or weights.
Only shown in the page when at least one endpoint answers.
"""
import json, os, re, threading, time
from ..util import http_get, log, classify, ALERTS, CLIENTS

METRIC_LINE = re.compile(r'^([a-zA-Z_:][\w:]*)(\{[^}]*\})?\s+([-+\deE.naNInf]+)')


def parse_prom(txt, prefixes):
    out = {}
    for line in txt.splitlines():
        if not line or line[0] == "#" or not line.startswith(prefixes): continue
        if "_bucket" in line[:80] or "_created" in line[:80]: continue
        m = METRIC_LINE.match(line)
        if not m: continue
        try: v = float(m.group(3))
        except ValueError: continue
        out.setdefault(m.group(1), []).append((m.group(2) or "", v))
    return out


def msum(d, *names):
    for n in names:
        if n in d: return sum(v for _, v in d[n])
    return None


def hist_mean(d, name, prev, key):
    s = msum(d, name + "_sum"); c = msum(d, name + "_count")
    if s is None or c is None: return None, (s, c)
    p = prev.get(key); r = None
    if p and p[1] is not None and c > p[1]: r = (s - p[0]) / (c - p[1])
    return r, (s, c)


def netns_clients(pid, port):
    """Linux: established peers on <port> inside a container netns via world-readable /proc/<pid>/net/tcp."""
    ips = []; hp = "%04X" % port
    for fn, v6 in (("tcp", False), ("tcp6", True)):
        try:
            with open("/proc/%d/net/%s" % (pid, fn)) as f:
                next(f)
                for line in f:
                    sp = line.split()
                    if len(sp) < 4 or sp[3] != "01" or sp[1].rsplit(":", 1)[1] != hp: continue
                    h = sp[2].rsplit(":", 1)[0]
                    if not v6: ips.append(".".join(str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0)))
                    elif h.startswith("0000000000000000FFFF0000"):
                        x = h[24:]; ips.append(".".join(str(int(x[i:i + 2], 16)) for i in (6, 4, 2, 0)))
        except Exception: pass
    return list(dict.fromkeys(ips))


class Models:
    def __init__(self, cfg, collector, docker):
        self.cfg = cfg; self.col = collector; self.docker = docker
        c = cfg["models"]
        self.enabled = str(c.get("enabled", "auto")).lower() != "false"
        self.known = [int(p) for p in c.get("ports") or []]
        self.auto = bool(c.get("autodiscover", True))
        self.mgr_port = c.get("manager_port")
        self.skip = set(int(p) for p in c.get("skip_ports") or []) | {int(cfg["port"])} | ({int(self.mgr_port)} if self.mgr_port else set())
        self.lock = threading.Lock(); self.list = []; self.ports = {p: {"kind": None, "checked": 0} for p in self.known}
        self.prev = {}; self.mgr = {}; self.mgr_t = 0; self.mgr_ok = False; self.mgr_code = None; self.t0 = time.time()

    def start(self):
        if self.enabled: threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            t0 = time.time()
            try: self.poll()
            except Exception as e: log("models err %r" % e)
            time.sleep(max(0.5, (2.0 if CLIENTS[0] > 0 else 5.0) - (time.time() - t0)))

    def discover(self, listen):
        if self.auto:
            for p, e in listen.items():
                if p in self.skip or p in self.ports or p >= 32768: continue
                self.ports[p] = {"kind": None, "checked": 0}
        for p, st in self.ports.items():
            if st["kind"] in (None, "none") and time.time() - st["checked"] > (60 if p in self.known else 300) and (p in listen or p in self.known):
                st["checked"] = time.time()
                code, body = http_get("http://127.0.0.1:%d/v1/models" % p, timeout=1.5)
                if code == 200 and '"data"' in body: st["kind"] = "openai"
                else:
                    code, body = http_get("http://127.0.0.1:%d/api/tags" % p, timeout=1.5)
                    st["kind"] = "ollama" if code == 200 and '"models"' in body else "none"

    def manager(self):
        """optional: a vllm-manager style controller exposing GET /api/status {"slots": {...: {"port": ..}}}"""
        if not self.mgr_port or time.time() - self.mgr_t < 10: return
        self.mgr_t = time.time()
        code, body = http_get("http://127.0.0.1:%d/api/status" % int(self.mgr_port), timeout=2)
        self.mgr_ok = code == 200; self.mgr_code = code
        if code == 200:
            try:
                m = {}
                for s in (json.loads(body).get("slots") or {}).values():
                    if isinstance(s, dict) and s.get("port"):
                        m[int(s["port"])] = {"slot": s.get("slot"), "state": s.get("state"), "uptime_s": s.get("uptime_s"),
                                             "display": (s.get("model_info") or {}).get("name"),
                                             "gpu_mem_gb": (s.get("metrics") or {}).get("gpu_mem_gb")}
                self.mgr = m
            except Exception: pass

    def poll(self):
        with self.col.net_lock:
            listen = dict(self.col.net.get("listen") or {}); flows = list(self.col.net.get("flows") or [])
        self.discover(listen); self.manager()
        with self.docker.lock: p2c = dict(self.docker.port2ctr); ctrs = dict(self.docker.ctrs)
        gprocs = list(getattr(self.col, "gpu_procs", []) or [])
        with self.col.proc_lock: byc = dict(self.col.by_ctr)
        t = time.time(); res = []
        for port, st in sorted(self.ports.items()):
            if st["kind"] in (None, "none"): continue
            if port not in listen and port not in self.known: continue
            m = {"port": port, "engine": "?", "models": [], "running": None, "waiting": None, "prompt_tps": 0.0, "gen_tps": 0.0, "kv": None, "reqs_ps": 0.0}
            pv = self.prev.get(port, {})
            if t - pv.get("models_t", 0) > 30 or not pv.get("models"):
                path = "/api/tags" if st["kind"] == "ollama" else "/v1/models"
                code, body = http_get("http://127.0.0.1:%d%s" % (port, path), timeout=1.5); names = []
                if code == 200:
                    try:
                        d = json.loads(body)
                        for x in d.get("data", []) or d.get("models", []):
                            names.append(x.get("id") or x.get("name"))
                            ob = x.get("owned_by")
                            if ob == "vllm": pv["engine"] = "vLLM"
                            elif ob == "llamacpp": pv["engine"] = "llama.cpp"
                            elif ob == "sglang": pv["engine"] = "SGLang"
                    except Exception: pass
                pv["models"] = names; pv["models_t"] = t
            m["models"] = pv.get("models") or []
            if st["kind"] == "ollama":
                code, _ = http_get("http://127.0.0.1:%d/api/version" % port, timeout=1.5, limit=256)
            else:
                code, _ = http_get("http://127.0.0.1:%d/health" % port, timeout=1.5, limit=256)
                if code == 404: code, _ = http_get("http://127.0.0.1:%d/v1/models" % port, timeout=1.5, limit=256)
            m["health_code"] = code
            code, txt = http_get("http://127.0.0.1:%d/metrics" % port, timeout=2) if st["kind"] != "ollama" else (None, "")
            if code == 200 and txt:
                if "vllm:" in txt[:200000]:
                    d = parse_prom(txt, ("vllm:",))
                    ttft, pv["ttft"] = hist_mean(d, "vllm:time_to_first_token_seconds", pv, "ttft")
                    itl, pv["itl"] = hist_mean(d, "vllm:inter_token_latency_seconds", pv, "itl")
                    m.update({"engine": "vLLM", "running": msum(d, "vllm:num_requests_running"), "waiting": msum(d, "vllm:num_requests_waiting"),
                              "kv": msum(d, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"),
                              "prompt_total": msum(d, "vllm:prompt_tokens_total"), "gen_total": msum(d, "vllm:generation_tokens_total"),
                              "req_total": msum(d, "vllm:request_success_total"), "ttft": ttft, "itl": itl})
                elif "sglang:" in txt:
                    d = parse_prom(txt, ("sglang:",))
                    m.update({"engine": "SGLang", "running": msum(d, "sglang:num_running_reqs"), "waiting": msum(d, "sglang:num_queue_reqs"),
                              "kv": msum(d, "sglang:token_usage"), "prompt_total": msum(d, "sglang:prompt_tokens_total"),
                              "gen_total": msum(d, "sglang:generation_tokens_total"), "req_total": msum(d, "sglang:num_requests_total")})
                elif "llamacpp:" in txt:
                    d = parse_prom(txt, ("llamacpp:",))
                    m.update({"engine": "llama.cpp", "running": msum(d, "llamacpp:requests_processing"), "waiting": msum(d, "llamacpp:requests_deferred"),
                              "kv": msum(d, "llamacpp:kv_cache_usage_ratio"), "prompt_total": msum(d, "llamacpp:prompt_tokens_total"),
                              "gen_total": msum(d, "llamacpp:tokens_predicted_total")})
            if m["engine"] == "?": m["engine"] = pv.get("engine") or ("Ollama" if st["kind"] == "ollama" else "OpenAI")
            pt0, gt0, rq0, t_0 = pv.get("pt"), pv.get("gt"), pv.get("rq"), pv.get("t")
            if t_0 and m.get("prompt_total") is not None and pt0 is not None:
                dtt = max(0.2, t - t_0)
                m["prompt_tps"] = max(0.0, m["prompt_total"] - pt0) / dtt
                m["gen_tps"] = max(0.0, (m.get("gen_total") or 0) - (gt0 or 0)) / dtt
                if m.get("req_total") is not None and rq0 is not None: m["reqs_ps"] = max(0.0, m["req_total"] - rq0) / dtt
            pv["pt"], pv["gt"], pv["rq"], pv["t"] = m.get("prompt_total"), m.get("gen_total"), m.get("req_total"), t
            cid = p2c.get(port, ""); c = ctrs.get(cid) if cid else None
            m["container"] = c["name"] if c else ""; m["image"] = c["image"] if c else ""; m["ctr_id"] = cid[:12]
            if m["engine"] == "OpenAI" and c:
                im = c["image"].lower()
                m["engine"] = "vLLM" if "vllm" in im else "SGLang" if "sglang" in im else "llama.cpp" if "llama" in im else "Ollama" if "ollama" in im else m["engine"]
            stt = self.docker.started.get(cid)
            m["uptime_s"] = int(t - stt[0]) if stt and stt[0] else None
            m["gpu_mem"] = sum(p["mem"] or 0 for p in gprocs if cid and p.get("ctr") and cid.startswith(p["ctr"])) or None
            b = byc.get(cid) if cid else None
            m["cpu"] = round(b["cpu"], 1) if b else None; m["rss"] = b["rss"] if b else None
            mg = self.mgr.get(port)
            if mg:
                m["display"] = mg.get("display"); m["mgr_state"] = mg.get("state")
                if m["uptime_s"] is None: m["uptime_s"] = mg.get("uptime_s")
                if not m["gpu_mem"] and mg.get("gpu_mem_gb"): m["gpu_mem"] = mg["gpu_mem_gb"] * 1024 ** 3
            loading = (m.get("mgr_state") or "online") not in ("online", "error", "failed", "crashed", "exited", "stopped", "dead") or (m["uptime_s"] or 1e9) < 900
            m["status"] = "ok" if m["health_code"] == 200 else ("loading" if loading else "down")
            cl = {}
            for f in flows:
                if f["lport"] == str(port):
                    k = f["remote"]; e = cl.setdefault(k, {"ip": k, "n": 0, "rx": 0.0, "tx": 0.0, "cls": classify(k)})
                    e["n"] += 1; e["rx"] += f["rx"]; e["tx"] += f["tx"]
            if stt and stt[1] and os.path.isdir("/proc"):
                for ip in netns_clients(stt[1], (c.get("cport") or {}).get(port, port)):
                    if ip.startswith("172.17.") or ip in cl: continue
                    cl.setdefault(ip, {"ip": ip, "n": 1, "rx": 0.0, "tx": 0.0, "cls": classify(ip)})
            m["clients"] = sorted(cl.values(), key=lambda x: -(x["rx"] + x["tx"] + x["n"]))[:8]
            m["active"] = (m.get("running") or 0) > 0 or m["gen_tps"] > 0.5
            self.prev[port] = pv; res.append(m)
        for m in res:
            nm = (m["models"] or [str(m["port"])])[0]
            if m.get("status") == "loading" or time.time() - self.t0 < 40: pass
            elif m["health_code"] != 200:
                ALERTS.push("model_down_%d" % m["port"], "crit", "model_down", nm, ":%d → %s" % (m["port"], m["health_code"]), 120, "model")
            if (m.get("waiting") or 0) >= 4:
                ALERTS.push("model_queue_%d" % m["port"], "warn", "model_queue", nm, "Q %d · R %d" % (m["waiting"], m.get("running") or 0), 90, "model")
            if (m.get("kv") or 0) >= 0.9:
                ALERTS.push("model_kv_%d" % m["port"], "warn", "model_kv", nm, "KV %.0f%%" % (m["kv"] * 100), 120, "model")
            was = self.prev[m["port"]].get("was_active", False)
            if m["gen_tps"] > 5 and not was:
                ALERTS.push("model_busy_%d" % m["port"], "info", "model_busy", nm, "%.0f tok/s · R %d" % (m["gen_tps"], m.get("running") or 0), 60, "model")
            self.prev[m["port"]]["was_active"] = m["gen_tps"] > 1
        with self.lock: self.list = res

    def snapshot(self):
        with self.lock: return list(self.list)

    def manager_info(self):
        if not self.mgr_port: return None
        return {"port": int(self.mgr_port), "ok": self.mgr_ok, "code": self.mgr_code, "slots": len(self.mgr)}
