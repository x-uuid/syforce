# syforce — AGENT_PROMPT

> 中文在前 · English below（[跳到 English](#english)）

---

## 0. 复制粘贴给 agent 的提示词

```text
你是一名资深系统工程师。这个文件夹是 syforce（系统力导向图）：一个单进程、零构建的实时系统拓扑仪表盘
（Python 后端 + SSE + 单文件 HTML/Canvas 前端）。请在【本机】完成部署并按本机环境适配：
1. 识别操作系统与硬件（macOS / Linux / Windows；CPU 架构；GPU 类型；是否有 Tailscale、Docker、本地模型服务）。
2. 在 app/ 下创建 venv，pip install -r requirements.txt，先跑 `python server.py --demo --port 9001` 确认页面无 JS 报错。
3. 跑真实模式 `python server.py --port 9001`，对照第 3 节数据契约检查 /api/state，缺什么补什么（只改对应 collector / plugin）。
4. 生成 app/config.json：名称映射、隐藏列表、模型端口、语言、帧率模式。不要把隐私写进代码。
5. 按第 5 节安装为用户级服务（launchd / systemd --user + linger / 计划任务或 NSSM），端口 9001。
6. 严格遵守第 6 节安全规则；执行第 7 节清单与验收测试，最后报告 URL、测试结果、未完成项。
```

## 1. 要做什么

- 一个页面，中心是本机，四周是 CPU / GPU / 内存 / 磁盘、热点进程、网卡 → 网关 → 路由 → 互联网链路、远端主机、Tailscale 节点（含出口节点）、本地模型服务。
- 连线上的光粒子 = 实时字节流：方向（↓ 入 / ↑ 出）、速率、颜色均来自真实数据。
- **真正的力导向布局**：节点互斥；边是弹簧，刚度和静止长度随实时流量变化（流量越大越紧越短）；温和阻尼使其平静收敛；结构节点（本机居中，网关/互联网链路水平可读）有软锚点；节点可拖拽，松手弹回；点击聚焦仍可用。
- 后端每秒（无观众时每 2 秒）推送一次快照（SSE `event: snap`），告警单独推送（`event: alert`）。

## 2. 设计规则（必须保留）

**视觉语言**
- 深色太空背景 `#05070d`，面板 `#0b1020`，分隔线 `#1a2340`，文字 `#d6def5`，次要 `#7d89ab`，40px 网格淡线。
- 颜色语义固定：LAN `#3ee0ff`、互联网 `#5b8cff`、Tailnet `#b36bff`、出口 `#ffcc33`、磁盘 `#ff9d3b`、GPU `#4dff9a`、CPU `#ff5ca8`、内存 `#ffe066`、模型 `#b4ff3c`、告警红 `#ff4d6d`。
- 节点：深色圆盘 + 彩色描边 + 负载弧 + 辉光精灵；CPU 节点外圈每核一段弧；出口节点双圈闪烁。

**图标规范**：一套线性图标（Lucide 风格），24 单位网格，描边 1.75，圆角端点/连接，`currentColor`；顶栏图标 18px，其他 16px。不得混用 emoji 或多套图标。

**布局**
- 顶栏：标题 · 图标行（CPU、内存、GPU、模型、磁盘、网络、Tailnet、延迟、告警、面板占用）· 弹性空白 · 出口节点按钮 · 节能/流畅 · 文A 语言菜单（中文/English/日本語）· 侧栏开关 · 全屏 · 连接状态 · 时钟。
- 图标后的数值用**固定宽度槽位**（`.rv[data-w]`，tabular-nums），数值变化时顶栏绝不抖动。
- 点图标打开**弹窗**（图表 + 键值 + 表格），弹窗贴在图标下方、避开"此刻"与聚焦标签、可拖动标题栏、点空白/Esc 关闭。
- 右侧栏**只放远端主机**（可按 总/↓/↑ 排序），点击即聚焦。窄屏（<1200px）侧栏落到画布下方。
- 不存在的功能（无 GPU、无 Tailscale、无模型）对应图标、节点、图例**自动隐藏**。

**聚焦规则**：点击节点/连线/表格行/主机行 → 相关节点与边高亮，其余变暗到 22%；表格行 `fsel`(选中) / `fhl`(相关) / `fdim`(无关)；再次点击或 Esc 取消。点 CPU/内存/GPU/磁盘/模型节点会同时打开对应弹窗。

**Tooltip 放置**：6 个候选位置（上、右、左、右上、左上、下），选与其他节点框、标签框、浮层重叠面积最小的；不得超出画布。

**简洁文字规则**：只用简短标签和数字，没有说明性句子。所有文字走 i18n（zh 为源语言，en/ja 译文）。

**力导向物理**：每渲染帧积分一次（与 fps 同步），排斥为有界线性软碰撞（避免抖动），标签框之间额外分离，弹簧静止长度 = 锚点距离 ×(1−0.3×流量)，刚度 0.01+0.04×流量，阻尼 0.72/帧，限速，最多 90 个节点参与；动能低于阈值时休眠，数据变化/拖拽时唤醒。

**性能预算**：后端 ≈ 单核 3%；页面在节能模式（20 fps、粒子上限 600）≈ 单核 15%；流畅模式 30 fps / 1200 粒子。标签预渲染缓存；粒子上限按帧耗时自适应；页面隐藏时不绘制；无观众时后端降频。`/api/cost` 显示各子命令耗时，面板占用弹窗显示后端与本页的开销。

## 3. 数据契约（SSE `event: snap` 的 JSON）

```jsonc
{
  "t": 1792500000.1, "time": "2026-01-01 12:00:00", "host": "workstation", "title": "syforce",
  "platform": "Linux · x86_64", "os": "linux|macos|windows|generic",
  "features": {"gpu": true, "models": true, "tail": true, "wifi": false, "docker": true, "demo": false},
  "cpu":  {"total": 23.5, "per": [12, 40, ...], "labels": ["P","P","E",...], "kinds": {"P":"Performance","E":"Efficiency"},
           "load": [1.2, 0.9, 0.8], "n": 16, "freq": [3200, ...] | null},
  "mem":  {"total": 0, "used": 0, "avail": 0, "pct": 52.1, "cached": 0, "swap_used": 0, "swap_total": 0,
           "psi": 0.0 | null, "pressure": 1 | 2 | 4},
  "gpu":  {"present": true, "name": "…", "util": 65, "device": 65, "temp": 70 | null, "power": 250.0 | null, "clock_sm": 1800 | null,
           "pstate": 2 | null, "mem_used": 0 | null, "mem_total": 0 | null, "procs": [{"pid":1,"name":"…","mem":0,"ctr":"<12-hex>"}], "src": "NVML|nvidia-smi|IOKit|demo"},
  "disk": {"r": 0, "w": 0, "disks": [{"name":"nvme0n1","r":0,"w":0,"riops":0,"wiops":0}],
           "vols": [{"mount":"/","total":0,"used":0,"free":0,"pct":55.0}], "top": [{"pid":1,"name":"…","r":0,"w":0}], "io_denied": 0},
  "nics": [{"name":"eth0","kind":"wired|wifi|tail|virtual|other","rx":0,"tx":0,"ip":"…","main":true,"tail":false,"wifi":false}],
  "main_nic": "eth0",
  "net":  {"rx": 0, "tx": 0,                                   // main NIC bytes/s
           "procs": [{"pid":1,"name":"chrome","rx":0,"tx":0,"n":3}],
           "hosts": [{"ip":"…","cls":"lan|internet|tailnet","rx":0,"tx":0,"n":1,"host":"rDNS or config name","procs":["chrome"],"ports":["443"]}],
           "conn": {"tcp":0,"udp":0,"listen":0,"est":0}, "age": 1.0},
  "procs": {"cpu": [{"pid":1,"name":"…","cpu":12.5,"mem":0,"ctr":""}], "mem": [{"pid":1,"name":"…","mem":0}], "count": 400},
  "models": [{"port":8000,"engine":"vLLM|SGLang|llama.cpp|Ollama|OpenAI","models":["name"],"status":"ok|loading|down","health_code":200,
              "running":1,"waiting":0,"prompt_tps":0,"gen_tps":0,"kv":0.3,"uptime_s":0,"gpu_mem":0,"container":"","ctr_id":"",
              "clients":[{"ip":"…","cls":"lan|tailnet|internet","n":1,"rx":0,"tx":0}]}],
  "manager": null | {"port":9000,"ok":true,"slots":2}, "tok": 0.0,
  "tail": {"ok": true, "exit_node": "name|null", "self": {"ips": ["…"]},
           "peers": [{"name":"laptop","ip":"…","online":true,"active":true,"exit":false,"cur":"…","relay":"…","rx":0,"tx":0}]},
  "topo": {"hops": [{"ip":"…","name":"","ms":0.8}]},          // hops[0] = default gateway, then private traceroute hops
  "ping": [{"name":"DNS","ip":"…","ms":12.3|null,"role":"hop0|hop1|inet"}],
  "overhead": {"self":1.1,"children":0.2,"total":1.3}, "clients": 1,
  "panel": {"backend": {"cpu":1.2,"cpu_self":1.0,"cpu_children":0.2,"rss":0,"threads":12}, "clients": [...], "pids": [...]}
}
```
告警 `event: alert`：`{"id","ts","time","key","level":"info|warn|crit","code","subj","val","kind":"cpu|gpu|mem|disk|net|model","text"}`。
其它接口：`GET /api/state`、`/api/history`（300 点历史 + 告警）、`/api/config`（语言/帧率/标题）、`/api/cost`、`/healthz`、`POST /api/beacon`（页面自测）。速率单位一律 bytes/s，内存一律 bytes。

## 4. 各系统采集要点与注意事项

| | 进程 | 每连接字节 | GPU | 内存压力 | 拓扑 |
|---|---|---|---|---|---|
| macOS | `proc_pid_rusage`（他人进程用 `ps` 每 30 s 近似） | `netstat -anvb`（全用户，免 sudo，约 10 ms） | IOKit `IOAccelerator` / `ioreg`，**仅整机利用率；无 sudo 拿不到每进程 GPU**（powermetrics 需要 root，不要用） | `kern.memorystatus_vm_pressure_level` | `route -n get default`、`traceroute -n` |
| Linux | `/proc/<pid>/stat`（全用户），`/proc/<pid>/io`（仅本用户） | `ss -tinp`（字节全用户；**root 拥有的 socket 无 pid，除非 sudo**——不要为此提权，显示为 `:端口`/`?`） | NVML（`nvidia-ml-py`）→ `nvidia-smi` 回退；统一内存平台 `mem_total` 可能为空 | `/proc/pressure/memory` PSI | `/proc/net/route`、`traceroute`/`tracepath` |
| Windows | `NtQuerySystemInformation` 一次快照（全用户，免管理员）；名称 = exe 名去 `.exe`，`app` = FileDescription | **无每连接字节**（GetPerTcpConnectionEStats / ETW 需管理员）：主机只有连接数 | NVML → `nvidia-smi`；非 NVIDIA 无面板；无温度传感器 | 提交量（`K32GetPerformanceInfo`） | `GetAdaptersAddresses` 网关、`tracert -d` |

- ICMP：优先免权限 ICMP socket（macOS 允许；Linux 取决于 `net.ipv4.ping_group_range`），否则调用系统 `ping`。
- 拓扑节点**一律泛称**（网关、路由、路由 2…），真实名字只通过 `config.json` 的 `names` 提供。只保留私网段的 traceroute 跳。
- Tailscale：找不到二进制或 `status --json` 失败则整个插件隐藏。模型插件：配置端口 + 自动发现监听端口，`GET /v1/models` 成功才显示。

## 5. 服务安装

- **macOS（launchd）**：`service/macos/com.syforce.dashboard.plist`，把 `__APP__` 换成 app 绝对路径，放到 `~/Library/LaunchAgents/`，`launchctl bootstrap gui/$(id -u) <plist>`。
- **Linux（systemd 用户服务 + linger）**：`service/linux/syforce.service` → `~/.config/systemd/user/`，`systemctl --user daemon-reload && systemctl --user enable --now syforce`，`sudo loginctl enable-linger $USER`（开机自启、注销不停）。
- **Windows**：管理员 PowerShell 运行 `service/windows/install-task.ps1`（venv + 计划任务：开机/登录启动、S4U 以当前用户运行、pythonw 隐藏、防火墙放行 Private 9001；可选 `-PublicLanSubnet` 在局域网被标为 Public 时放行本子网）；卸载 `uninstall-task.ps1`。控制：`syforce-ctl.ps1 start|stop|restart|status`。计划任务停在 Queued 时 start 改为直接拉起；单实例 supervisor 在进程退出后重启。
- 端口默认 9001；若被占用，改 `config.json` 的 `port` 或 `--port`。

## 6. 安全规则（硬性）

1. 所有探测只读：只允许 HTTP GET（`/v1/models`、`/health`、`/metrics`、`/api/tags`、可选 manager `/api/status`）和只读命令（`ss`、`netstat`、`ps`、`docker ps/inspect`、`tailscale status`、`nvidia-smi` 查询）。
2. **绝不**启动/停止/重启/重载模型服务、容器、缓存、权重或其它任何服务；绝不 POST 到模型端点；绝不清理缓存。
3. 不需要 sudo；不为拿更多数据而提权。
4. 隐私：真实主机名、IP、域名、用户名只放在本机 `config.json`；截图一律用 `--demo`。
5. 只管理 syforce 自己的服务单元。

## 7. 部署清单与验收测试

**清单**
1. `python3 --version` ≥ 3.9；创建 venv；`pip install -r requirements.txt`。
2. `python server.py --demo --port 9001` → 浏览器打开，无控制台错误 → 停止。
3. `python server.py --port 9001` → `curl -s localhost:9001/api/state` 检查字段（第 3 节）。
4. 按需修 collector（只改 `app/syforce/collectors/<os>.py` 或 plugins）。
5. 写 `app/config.json`（从 `config.example.json` 复制）。
6. 安装服务（第 5 节），确认开机/登录后自动运行。

**验收测试**
- [ ] `/healthz` 返回 `ok`；`/api/state` 每秒更新（`t` 递增）。
- [ ] 页面无 JS 错误；顶栏数值变化时宽度不变。
- [ ] 文A 菜单切换 中文/English/日本語，全部标签随之切换；刷新后保持。
- [ ] 全屏（按钮和 `F` 键）、侧栏开关、节能/流畅切换可用；滚动条细。
- [ ] 点击 CPU 图标弹窗贴在图标下方，可拖动，Esc 关闭。
- [ ] 点击节点 → 聚焦高亮其关联，其它变暗；点击空白取消。
- [ ] 拖动节点后松手，节点弹回；静止约 3 s 后物理休眠（性能角标 `⚛` 消失或保持低频唤醒）。
- [ ] 下载一个大文件：对应进程、主机、网卡、网关→互联网链路出现粒子流，链路变短变紧。
- [ ] 无 GPU / Tailscale / 模型的机器上，相关图标与节点不出现。
- [ ] 面板占用：后端 ≤ 约 3%（单核），节能模式页面 ≤ 约 15%。
- [ ] 隐藏列表中的名字/IP 不出现在 `/api/state`。
- [ ] 重启机器（或注销再登录）后服务自动恢复。

---

<a id="english"></a>

# English

## 0. Copy-paste prompt for your agent

```text
You are a senior systems engineer. This folder is syforce (OS force-directed graph): a single-process, zero-build,
real-time system topology dashboard (Python backend + SSE + one-file HTML/Canvas frontend). Deploy and adapt it on THIS machine:
1. Detect OS and hardware (macOS / Linux / Windows; CPU arch; GPU type; Tailscale, Docker, local model servers present?).
2. In app/ create a venv, pip install -r requirements.txt, run `python server.py --demo --port 9001` and confirm no JS errors.
3. Run live `python server.py --port 9001`, compare /api/state with the data contract (section 3), fix gaps only in the matching collector/plugin.
4. Write app/config.json: names, hide list, model ports, language, fps mode. Never hardcode private data in code.
5. Install it as a user service on port 9001 (launchd / systemd --user + linger / Task Scheduler or NSSM), see section 5.
6. Obey the safety rules (section 6). Run the checklist and acceptance tests (section 7); report the URL, results and anything unfinished.
```

## 1. What to build

- One page: this machine in the centre; around it CPU / GPU / memory / disk, hot processes, NIC → gateway → router → Internet chain, remote hosts, Tailscale peers (with exit node) and local model servers.
- Light particles on links = live bytes: direction (↓ in / ↑ out), rate and colour come from real counters.
- **Real force-directed layout**: node repulsion; edges are springs whose stiffness and rest length follow live traffic (more traffic = tighter, shorter); gentle damping so it settles calmly; soft anchors keep structural nodes in place (machine centred, gateway/Internet chain readable); nodes are draggable and spring back; click-to-focus still works.
- The backend pushes one snapshot per second (every 2 s with no viewers) as SSE `event: snap`; alerts are pushed separately as `event: alert`.

## 2. Design rules (keep all of them)

**Visual language** — dark space background `#05070d`, panels `#0b1020`, lines `#1a2340`, text `#d6def5`, dim `#7d89ab`, faint 40 px grid. Fixed colour semantics: LAN `#3ee0ff`, Internet `#5b8cff`, Tailnet `#b36bff`, exit `#ffcc33`, disk `#ff9d3b`, GPU `#4dff9a`, CPU `#ff5ca8`, memory `#ffe066`, models `#b4ff3c`, alert red `#ff4d6d`. Nodes: dark disc + coloured stroke + load arc + glow sprite; the CPU node has one arc segment per core; the exit node has a pulsing double ring.

**Icon spec** — one line-icon set (Lucide style), 24-unit grid, 1.75 stroke, round caps/joins, `currentColor`; 18 px in the top bar, 16 px elsewhere. No emoji, no second icon set.

**Layout** — top bar: title · icon row (CPU, memory, GPU, models, disk, network, Tailnet, latency, alerts, panel cost) · spacer · exit-node button · 节能/流畅 (eco/smooth) · 文A language menu (中文/English/日本語) · sidebar toggle · fullscreen · connection state · clock. Values after icons live in **fixed-width slots** (`.rv[data-w]`, tabular-nums) so the bar never jitters. Clicking an icon opens a **popover** (chart + key/values + table) anchored under the icon, pushed below the "Now" and focus chips, draggable by its header, closed by outside click or Esc. The right sidebar holds **remote hosts only** (sortable total/↓/↑), click to focus; below 1200 px it drops under the canvas. Absent features (no GPU, Tailscale or models) hide their icon, nodes and legend entries automatically.

**Focus rules** — clicking a node, link, table row or host row highlights everything related and dims the rest to 22 %; rows get `fsel` (selected) / `fhl` (related) / `fdim` (unrelated); click again or Esc to clear. Clicking the CPU/memory/GPU/disk/model nodes also opens the matching popover.

**Tooltip placement** — six candidates (above, right, left, upper-right, upper-left, below); choose the one with the least overlap against node boxes, label boxes and overlays; never leave the canvas.

**Terse-text rule** — short labels and numbers only, no descriptive sentences. All strings go through i18n (zh is the source, en/ja translations).

**Force-directed physics** — integrate once per rendered frame (same fps), bounded linear soft-collision repulsion (no jitter), extra separation between label boxes, spring rest = anchor distance × (1 − 0.3 × traffic), stiffness 0.01 + 0.04 × traffic, damping 0.72/frame, speed cap, at most 90 simulated nodes; sleep when kinetic energy is low, wake on data change or drag.

**Performance budget** — backend ≈ 3 % of one core; page ≈ 15 % of one core in 节能/eco (20 fps, 600 particles); smooth = 30 fps / 1200 particles. Pre-rendered label cache, adaptive particle cap by frame time, no drawing while the tab is hidden, slower sampling with no viewers. `/api/cost` shows helper-command cost; the panel-cost popover shows backend and tab cost.

## 3. Data contract

See the JSON schema in section 3 above (identical for all OSes and for `--demo`). Alerts: `{"id","ts","time","key","level":"info|warn|crit","code","subj","val","kind","text"}`. Other endpoints: `GET /api/state`, `/api/history` (300-point history + alerts), `/api/config` (language / fps mode / title), `/api/cost`, `/healthz`, `POST /api/beacon` (tab self-measurement). Rates are bytes/s, memory is bytes.

## 4. Per-OS collector guidance and caveats

- **macOS** — processes via `proc_pid_rusage` (other users' processes approximated with `ps` every 30 s); per-socket bytes via `netstat -anvb` (all users, no sudo, ~10 ms); GPU via IOKit `IOAccelerator` / `ioreg` — **device-wide only, no per-process GPU without sudo** (powermetrics needs root: don't); memory pressure via `kern.memorystatus_vm_pressure_level`; gateway `route -n get default`.
- **Linux** — `/proc/<pid>/stat` for all users, `/proc/<pid>/io` only for your own; per-socket bytes via `ss -tinp` (bytes for all users, but **root-owned sockets have no pid unless run with sudo** — do not escalate, they show as `:port`/`?`); GPU via NVML (`nvidia-ml-py`) with `nvidia-smi` fallback (unified-memory boards may report no `mem_total`); PSI from `/proc/pressure/memory`; gateway from `/proc/net/route`.
- **Windows** — processes from one `NtQuerySystemInformation` snapshot (all users, no admin; name = exe without `.exe`, `app` = FileDescription); NIC kind from `GetAdaptersAddresses` (vEthernet/WSL/Hyper-V = virtual, Tailscale = tail); GPU via NVML, `nvidia-smi` fallback, non-NVIDIA = no panel; **no per-connection bytes** (GetPerTcpConnectionEStats / ETW need admin) and no temperatures; memory pressure from commit charge; gateway from `GetAdaptersAddresses`, hops `tracert -d`.
- ICMP: unprivileged ICMP socket first (allowed on macOS; Linux depends on `net.ipv4.ping_group_range`), else the system `ping`.
- Topology nodes are **generic** (Gateway, Router, Router 2 …); real names only via `config.json` `names`. Only private-range traceroute hops are kept.
- Tailscale plugin hides itself if the binary or `status --json` is missing. The models plugin probes configured ports plus auto-discovered listening ports and shows only endpoints that answer `GET /v1/models` (or Ollama `/api/tags`).

## 5. Service setup

- **macOS (launchd)** — `service/macos/com.syforce.dashboard.plist`: replace `__APP__`, copy to `~/Library/LaunchAgents/`, `launchctl bootstrap gui/$(id -u) <plist>`.
- **Linux (systemd user + linger)** — `service/linux/syforce.service` → `~/.config/systemd/user/`, `systemctl --user daemon-reload && systemctl --user enable --now syforce`, `sudo loginctl enable-linger $USER`.
- **Windows** — elevated PowerShell: `service/windows/install-task.ps1` (venv + Scheduled Task at startup and logon, runs as you via S4U, hidden pythonw, firewall rule for 9001 on Private; optional `-PublicLanSubnet` allows the local subnet when the LAN is Public); remove with `uninstall-task.ps1`. Control: `syforce-ctl.ps1 start|stop|restart|status`. If Task Scheduler leaves the task Queued, start launches it directly; a single-instance supervisor restarts the server if it exits.
- Default port 9001; change with `config.json` `port` or `--port`.

## 6. Safety rules (hard)

1. Read-only probes only: HTTP GET (`/v1/models`, `/health`, `/metrics`, `/api/tags`, optional manager `/api/status`) and read-only commands (`ss`, `netstat`, `ps`, `docker ps/inspect`, `tailscale status`, `nvidia-smi` queries).
2. **Never** start/stop/restart/reload model servers, containers, caches, weights or any other service; never POST to model endpoints; never clear caches.
3. No sudo; never escalate to get more data.
4. Privacy: real hostnames, IPs, domains and usernames live only in the local `config.json`; screenshots always use `--demo`.
5. Manage only syforce's own service unit.

## 7. Deploy checklist and acceptance tests

**Checklist** — (1) Python ≥ 3.9, venv, `pip install -r requirements.txt`; (2) `--demo` runs with no console errors; (3) live mode `/api/state` matches section 3; (4) fix only the OS collector / plugins; (5) write `app/config.json`; (6) install the service and confirm it starts at boot/logon.

**Acceptance tests**
- [ ] `/healthz` → `ok`; `/api/state` updates every second (`t` increases).
- [ ] No JS errors; top-bar widths stay fixed while values change.
- [ ] 文A switches 中文/English/日本語 for every label and persists across reloads.
- [ ] Fullscreen (button and `F`), sidebar toggle and eco/smooth toggle work; scrollbars are thin.
- [ ] CPU icon popover opens under the icon, is draggable and closes with Esc.
- [ ] Clicking a node focuses its relations and dims the rest; clicking empty space clears.
- [ ] A dragged node springs back after release; after ~3 s at rest the physics sleeps (the `⚛` perf marker disappears or wakes only briefly).
- [ ] Downloading a large file shows particles on the process, host, NIC and gateway → Internet links, and those links tighten.
- [ ] On a machine without GPU / Tailscale / models those icons and nodes do not appear.
- [ ] Panel cost: backend ≲ 3 % of one core, page ≲ 15 % in eco.
- [ ] Names/IPs in the hide list never appear in `/api/state`.
- [ ] After reboot (or logout/login) the service is back automatically.
