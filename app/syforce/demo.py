"""--demo: synthetic but plausible data with the exact live schema. No collectors run, nothing is probed.

All names are generic and all addresses are RFC 5737 documentation ranges (192.0.2.x, 198.51.100.x, 203.0.113.x),
so screenshots taken in demo mode never contain private data.
"""
import math, random, time
from .util import now_str, ALERTS, MB
from .panel import PanelMon

R = random.Random(7)
LAN, TS, NET = "192.0.2.", "198.51.100.", "203.0.113."


def w(t, period, phase=0.0, lo=0.0, hi=1.0):
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * t / period + phase))


def burst(t, period, width, phase=0.0):
    x = ((t + phase) % period) / period
    return max(0.0, 1 - abs(x - 0.5) / (width / 2)) if abs(x - 0.5) < width / 2 else 0.0


PEERS = [("laptop", TS + "21", "macOS", True), ("nas", TS + "22", "linux", True), ("vps", TS + "23", "linux", True),
         ("phone", TS + "24", "iOS", False), ("desktop", TS + "25", "windows", True), ("tablet", TS + "26", "android", False)]
INET = [(NET + "10", "cdn.example.net", ["chrome"], "443"), (NET + "25", "api.example.com", ["python3 agent.py"], "443"),
        (NET + "40", "git.example.org", ["git"], "443"), ("198.51.100.80", "mirror.example.net", ["apt"], "80"),
        (NET + "77", "-", ["tailscaled"], "41641"), (NET + "91", "storage.example.com", ["rclone"], "443"),
        (NET + "120", "stream.example.org", ["chrome"], "443"), (NET + "150", "ntp.example.net", ["chronyd"], "123")]
LANH = [(LAN + "23", ["smbd"], "445"), (LAN + "40", ["sshd"], "22"), (LAN + "61", ["python3 agent.py"], "8000")]
MODELS = [(8000, "llama-3.1-8b-instruct", "vLLM", "vllm-a"), (8001, "qwen2.5-coder-7b", "SGLang", "sglang-b"), (11434, "mistral:7b", "Ollama", "")]


def gen(t, t0, panel):
    up = t - t0
    cpu_base = w(t, 47, 0, 8, 38) + 30 * burst(t, 90, 0.25)
    per = []
    for i in range(16):
        v = cpu_base * (1.3 if i < 8 else 0.7) + w(t, 7 + i, i, -8, 8) + R.uniform(-3, 3)
        if i == 3: v += 40 * burst(t, 60, 0.3, 10)
        per.append(max(0, min(100, v)))
    total = sum(per) / 16
    gen_a = 40 + 35 * w(t, 23) + R.uniform(-3, 3)          # tok/s on model A
    gen_b = 18 * burst(t, 40, 0.5, 5) + R.uniform(0, 1)
    gpu_u = min(100, 35 + gen_a * 0.6 + gen_b * 1.2 + R.uniform(-4, 4))
    eth_rx = (2.5 + 6 * w(t, 31) + 25 * burst(t, 75, 0.2, 20)) * MB
    eth_tx = (0.4 + 1.2 * w(t, 19, 1)) * MB
    ts_rx = (0.6 + 1.5 * w(t, 27, 2)) * MB; ts_tx = (0.9 + 2.5 * w(t, 33, 3)) * MB
    dr = (3 + 60 * burst(t, 50, 0.2)) * MB; dw = (1.5 + 8 * w(t, 29, 1)) * MB

    peers = []
    for i, (n, ip, osn, on) in enumerate(PEERS):
        k = 1 + i
        rx = ts_rx * (0.5 / k) if on else 0; tx = ts_tx * (0.5 / k) if on else 0
        peers.append({"id": "p%d" % i, "name": n, "dns": "", "ip": ip, "ips": [ip], "os": osn, "online": on, "active": on and i < 3,
                      "exit": n == "vps", "exit_opt": n == "vps", "cur": "direct" if i != 2 else "", "relay": "" if i != 2 else "relay-1", "rx": rx, "tx": tx})
    hosts = []
    for i, (ip, h, procs, port) in enumerate(INET):
        f = (0.45 / (i + 1)) * (0.6 + 0.4 * w(t, 11 + i, i))
        hosts.append({"ip": ip, "cls": "internet", "rx": eth_rx * f, "tx": eth_tx * f * 0.6, "n": 1 + i % 3, "iface": "lan", "host": h, "procs": procs, "ports": [port]})
    for i, (ip, procs, port) in enumerate(LANH):
        f = 0.08 / (i + 1)
        hosts.append({"ip": ip, "cls": "lan", "rx": eth_rx * f, "tx": eth_tx * f * 2, "n": 1, "iface": "lan", "host": "", "procs": procs, "ports": [port]})
    for p in peers[:3]:
        hosts.append({"ip": p["ip"], "cls": "tailnet", "rx": p["rx"], "tx": p["tx"], "n": 2, "iface": "tailscale", "host": "", "procs": ["tailscaled", "sshd"], "ports": ["22"]})
    hosts.sort(key=lambda h: -(h["rx"] + h["tx"]))

    procs_cpu = [("python3 train.py", 101, 60 + 30 * w(t, 17), 6.2), ("VLLM::EngineCore", 202, 35 + gen_a * 0.3, 18.5), ("chrome", 303, 8 + 6 * w(t, 9), 1.1),
                 ("node", 404, 4 + 5 * burst(t, 30, 0.4), 0.6), ("python3 server.py", panel.data.get("backend", {}).get("pid", 505), 1.2, 0.05),
                 ("dockerd", 606, 1.5, 0.1), ("rsync", 707, 12 * burst(t, 50, 0.3), 0.02), ("tailscaled", 808, 2.1, 0.06)]
    top_cpu = sorted([{"pid": p, "name": n, "cmd": "", "cpu": round(max(0.6, c + R.uniform(-1, 1)), 1), "mem": int(m * 1024 ** 3), "ctr": "ctr000a" if "VLLM" in n else ""}
                      for n, p, c, m in procs_cpu], key=lambda x: -x["cpu"])
    top_mem = sorted([{"pid": x["pid"], "name": x["name"], "mem": x["mem"], "ctr": x["ctr"]} for x in top_cpu], key=lambda x: -x["mem"])
    nprocs = [{"pid": 303, "name": "chrome", "rx": eth_rx * 0.5, "tx": eth_tx * 0.3, "n": 14}, {"pid": 707, "name": "rsync", "rx": eth_rx * 0.2 + dr * 0.05, "tx": eth_tx * 0.4, "n": 1},
              {"pid": 808, "name": "tailscaled", "rx": ts_rx, "tx": ts_tx, "n": 6}, {"pid": 202, "name": "vllm", "rx": 40e3, "tx": 180e3, "n": 3},
              {"pid": 909, "name": "git", "rx": 30e3 * w(t, 13), "tx": 4e3, "n": 1}]
    models = []
    for i, (port, name, eng, ctr) in enumerate(MODELS):
        g = gen_a if i == 0 else gen_b if i == 1 else 0
        st = "ok" if i < 2 else ("loading" if (up % 240) < 120 else "ok")
        cl = [{"ip": TS + "21", "n": 2, "rx": 2e3, "tx": 8e3, "cls": "tailnet"}] if i == 0 else [{"ip": LAN + "61", "n": 1, "rx": 1e3, "tx": 4e3, "cls": "lan"}] if i == 1 else []
        models.append({"port": port, "engine": eng, "models": [name], "running": (2 if i == 0 else 1 if g > 2 else 0) if st == "ok" else 0, "waiting": 0,
                       "prompt_tps": g * 6, "gen_tps": g, "kv": (0.32 + 0.2 * w(t, 41)) if i == 0 else 0.08, "reqs_ps": g / 120, "health_code": 200 if st == "ok" else 503,
                       "container": ctr, "image": "", "ctr_id": "ctr000a" if i == 0 else "", "uptime_s": int(86400 * 2.3 + up) if i == 0 else int(5400 + up),
                       "gpu_mem": (16.5 if i == 0 else 9.2 if i == 1 else 4.1) * 1024 ** 3, "cpu": None, "rss": None, "status": st, "clients": cl, "active": g > 0.5})
    tok = sum(m["gen_tps"] for m in models)
    nics = [{"name": "eth0", "kind": "wired", "rx": eth_rx, "tx": eth_tx, "ip": LAN + "10", "main": True, "tail": False, "wifi": False},
            {"name": "wlan0", "kind": "wifi", "rx": 0, "tx": 0, "ip": "", "main": False, "tail": False, "wifi": True},
            {"name": "tailscale0", "kind": "tail", "rx": ts_rx, "tx": ts_tx, "ip": TS + "20", "main": False, "tail": True, "wifi": False}]
    hops = [{"ip": LAN + "1", "name": "", "ms": round(0.6 + R.uniform(0, 0.5), 2)}, {"ip": LAN + "254", "name": "", "ms": round(1.4 + R.uniform(0, 0.8), 2)}]
    pings = [{"name": "", "ip": h["ip"], "ms": h["ms"], "role": "hop%d" % i} for i, h in enumerate(hops)] + \
            [{"name": "DNS", "ip": NET + "53", "ms": round(12 + 6 * w(t, 37) + R.uniform(0, 2), 1), "role": "inet"}]
    mem_total = 64 * 1024 ** 3; mem_pct = 52 + 8 * w(t, 120)
    gpu = {"present": True, "name": "Demo GPU 24 GB", "util": round(gpu_u), "device": round(gpu_u), "temp": round(55 + gpu_u * 0.22), "power": round(80 + gpu_u * 2.6, 1),
           "clock_sm": 1800 + int(600 * gpu_u / 100), "pstate": 2, "mem_used": int(20.6 * 1024 ** 3), "mem_total": 24 * 1024 ** 3, "mem_unified": False,
           "procs": [{"pid": 202, "name": "VLLM::EngineCore", "mem": int(16.5 * 1024 ** 3), "ctr": "ctr000a"}, {"pid": 101, "name": "python3", "mem": int(4.1 * 1024 ** 3), "ctr": ""}], "src": "demo"}
    return {
        "t": t, "time": now_str(), "host": "workstation", "title": "syforce", "platform": "Linux · x86_64 · demo", "os": "linux",
        "features": {"gpu": True, "models": True, "tail": True, "wifi": True, "docker": True, "demo": True},
        "cpu": {"total": round(total, 1), "per": [round(v) for v in per], "labels": ["P"] * 8 + ["E"] * 8, "kinds": {"P": "Performance", "E": "Efficiency"},
                "load": [round(total / 6, 2), round(total / 7, 2), round(total / 8, 2)], "n": 16, "freq": None},
        "mem": {"total": mem_total, "used": int(mem_total * mem_pct / 100), "avail": int(mem_total * (1 - mem_pct / 100)), "pct": round(mem_pct, 1),
                "cached": 9 * 1024 ** 3, "swap_used": 0, "swap_total": 8 * 1024 ** 3, "psi": 0.0, "pressure": 1},
        "gpu": gpu,
        "disk": {"r": dr, "w": dw, "disks": [{"name": "nvme0n1", "r": dr * 0.8, "w": dw * 0.9, "riops": dr / 65536, "wiops": dw / 32768},
                                              {"name": "sda", "r": dr * 0.2, "w": dw * 0.1, "riops": 12, "wiops": 4}],
                 "vols": [{"mount": "/", "device": "/dev/nvme0n1p2", "fs": "ext4", "total": 2e12, "used": 1.1e12, "free": 0.9e12, "pct": 55.0},
                          {"mount": "/data", "device": "/dev/sda1", "fs": "xfs", "total": 8e12, "used": 5.2e12, "free": 2.8e12, "pct": 65.0}],
                 "top": [{"pid": 707, "name": "rsync", "r": dr * 0.6, "w": dw * 0.2}, {"pid": 101, "name": "python3 train.py", "r": dr * 0.3, "w": dw * 0.5}], "io_denied": 0},
        "nics": nics, "main_nic": "eth0",
        "net": {"rx": eth_rx, "tx": eth_tx, "procs": nprocs, "hosts": hosts[:16], "conn": {"tcp": 142, "udp": 23, "listen": 18, "est": 96}, "age": 1.0},
        "procs": {"cpu": top_cpu, "mem": top_mem, "count": 412},
        "models": models, "manager": None, "tok": round(tok, 1),
        "tail": {"ok": True, "state": "Running", "self": {"name": "workstation", "ips": [TS + "20"]}, "exit_node": "vps", "peers": peers, "updated": t},
        "topo": {"hops": hops}, "ping": pings,
        "overhead": {"self": 1.1, "children": 0.0, "total": 1.1}, "clients": 1, "panel": panel.data,
    }


DEMO_ALERTS = [("gpu", "warn", "gpu", "", "92%", "gpu"), ("model_busy_8000", "info", "model_busy", "llama-3.1-8b-instruct", "68 tok/s · R 2", "model"),
               ("nic_eth0_rx", "crit", "nic", "eth0", "↓ 31.2 MB/s", "net"), ("disk_r", "warn", "disk_r", "rsync", "64.0 MB/s", "disk"),
               ("talker_chrome", "info", "talker", "chrome", "↓6.1 MB/s ↑0.4 MB/s", "net")]


def demo_sampler(cfg, finalize, HIST):
    panel = PanelMon(None); panel.start()
    t0 = time.time()
    for k in range(300, 0, -1):          # pre-fill 5 min of history so charts are populated immediately
        s = gen(t0 - k, t0 - 300, panel)
        tu = s["nics"][2]
        for key, v in (("t", int(s["t"])), ("cpu", s["cpu"]["total"]), ("mem", s["mem"]["pct"]), ("gpu", s["gpu"]["util"]), ("disk_r", round(s["disk"]["r"])),
                       ("disk_w", round(s["disk"]["w"])), ("net_rx", round(s["net"]["rx"])), ("net_tx", round(s["net"]["tx"])),
                       ("tail_rx", round(tu["rx"])), ("tail_tx", round(tu["tx"])), ("tok", s["tok"])):
            HIST[key].append(v)
    for i, a in enumerate(DEMO_ALERTS[:3]):
        ALERTS.push(a[0], a[1], a[2], a[3], a[4], 0, a[5], ts=t0 - 240 + i * 70)
    n = 0
    while True:
        t = time.time()
        try:
            finalize(gen(t, t0, panel))
            n += 1
            if n % 45 == 20:
                a = DEMO_ALERTS[(n // 45) % len(DEMO_ALERTS)]
                ALERTS.push(a[0] + str(n), a[1], a[2], a[3], a[4], 0, a[5])
        except Exception:
            import traceback; traceback.print_exc()
        time.sleep(max(0.05, 1.0 - (time.time() - t)))
