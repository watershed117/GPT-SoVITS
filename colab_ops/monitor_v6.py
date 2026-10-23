#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""v6 训练监控页（纯标准库，无外部依赖；本地 Windows / Colab Linux 均可跑）。

解析 v6 train.py 的 `log.jsonl`（JSON 行），渲染成自刷新网页：
  {"kind":"train","step":..,"cfm":..,"ce":..,"acc":..,"lam":..,"gnorm":..,
   "data_frac":..,"mem_gb":..,"s_per_it":..,"eta_h":..,"lr":..}
  {"kind":"val","split":"val_seen|unseen","step":..,"L_c":..,"L_shuf":..,"L_null":..,
   "L_same":..,"L_other":..,"gain_shuf":..,"gain_null":..,"clip":..,"ident":..,"n":..}

面板：状态条 / CFM / 判别器 ce·acc / λ 日程 / gnorm / **验证 gain_shuf·gain_null·clip·ident** /
L 五条件 / 负载曲线（后台 1s 采样）/ 最近记录表 / ckpt 表（扫 out 目录）/ 日志尾。

用法（本地，GSV 根目录）：
  start /b .venv\Scripts\python.exe colab_ops\monitor_v6.py --log _deliverytransfer\v6\out_base\log.jsonl --port 8010
用法（Colab VM，detached 存活于内核重启）：
  setsid nohup python3 monitor_v6.py --log /content/out_base/log.jsonl --port 8000 \
        > /content/monitor.log 2>&1 < /dev/null &
  本机侧开 SSH 隧道后访问 http://127.0.0.1:8010/（docs/colab_ops_sop.md §2.2）

端点：/  网页（10s 自刷新） · /partial 面板片段 · /api/data JSON · /api/sys 实时资源
      /raw?n=N 日志尾 N 行 · /health ok
"""
import argparse
import ctypes
import json
import os
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WIN = os.name == "nt"

TRAIN_FIELDS = ["cfm", "ce", "acc", "ce_x1", "lam", "gnorm", "lr", "data_frac", "mem_gb", "s_per_it", "eta_h"]
VAL_FIELDS = ["L_c", "L_shuf", "L_null", "L_same", "L_other", "gain_shuf", "gain_null", "clip", "ident"]


# ---------------------------------------------------------------- 解析
def parse_log(path, tail_lines=250):
    """返回 dict（train 系列 / val 按 split / tail / meta）。每次请求全量解析——log.jsonl 很小。"""
    out = {"train": {"steps": [], "series": {k: [] for k in TRAIN_FIELDS}},
           "val": {}, "tail": [],
           "meta": {"lines": 0, "mtime": None, "size": None, "last_step": None, "error": None}}
    if not os.path.isfile(path):
        out["meta"]["error"] = "log not found: %s" % path
        return out
    st = os.stat(path)
    out["meta"]["mtime"], out["meta"]["size"] = st.st_mtime, st.st_size
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        out["meta"]["error"] = str(e)
        return out
    out["meta"]["lines"] = len(lines)

    for ln in lines:
        ln = ln.strip()
        if not ln or ln[0] != "{":
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("kind") == "train":
            out["train"]["steps"].append(int(r.get("step", 0)))
            for k in TRAIN_FIELDS:
                out["train"]["series"][k].append(r.get(k))
            out["meta"]["last_step"] = r.get("step")
        elif r.get("kind") == "val":
            sp = r.get("split", "?")
            d = out["val"].setdefault(sp, {"steps": [], "series": {k: [] for k in VAL_FIELDS}})
            d["steps"].append(int(r.get("step", 0)))
            for k in VAL_FIELDS:
                d["series"][k].append(r.get(k))
    out["tail"] = [l.rstrip() for l in lines[-tail_lines:]]
    return out


# ---------------------------------------------------------------- 系统资源
def _n(v, f="%.0f"):
    return (f % v) if v is not None else "—"


def _cpu_percent():
    """两次采样求差（0.15s 窗口）。Linux 读 /proc/stat；Windows 用 ctypes GetSystemTimes。"""
    if not WIN:
        def snap():
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            return sum(v), v[3] + (v[4] if len(v) > 4 else 0)   # idle + iowait
        t1, i1 = snap()
        time.sleep(0.15)
        t2, i2 = snap()
        dt = t2 - t1
        return 100.0 * (1.0 - (i2 - i1) / dt) if dt > 0 else None

    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_long)]

    def uv(ft):
        return ft.lo + (ft.hi << 32)

    k32 = ctypes.windll.kernel32
    idle, kern, user = FT(), FT(), FT()

    def snap():
        k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        return uv(idle), uv(kern) + uv(user)      # kernel 含 idle
    i1, t1 = snap()
    time.sleep(0.15)
    i2, t2 = snap()
    dt = t2 - t1
    return 100.0 * (1.0 - (i2 - i1) / dt) if dt > 0 else None


def _mem():
    if not WIN:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                info[k] = float(v.split()[0])      # kB
        total = info["MemTotal"] / 1048576.0
        used = (info["MemTotal"] - info.get("MemAvailable", info["MemFree"])) / 1048576.0
        return used, total, 100.0 * used / total
    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong)] + \
                   [(n, ctypes.c_ulonglong) for n in ("ullTotalPageFile", "ullAvailPageFile",
                                                      "ullTotalVirtual", "ullAvailVirtual",
                                                      "ullAvailExtendedVirtual")]
    ms = MS()
    ms.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
    total = ms.ullTotalPhys / 1073741824.0
    used = (ms.ullTotalPhys - ms.ullAvailPhys) / 1073741824.0
    return used, total, float(ms.dwMemoryLoad)


def _gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        util, mu, mt = [float(x) for x in out.splitlines()[0].split(",")]
        return util, mu, mt, 100.0 * mu / mt
    except Exception:  # noqa: BLE001
        return None


def collect_sys():
    s = {"ts": time.time(), "cpu_percent": None, "mem_used_gb": None,
         "mem_total_gb": None, "mem_percent": None, "gpu_util": None,
         "vram_used_mb": None, "vram_total_mb": None, "vram_percent": None}
    try:
        s["cpu_percent"] = _cpu_percent()
    except Exception:  # noqa: BLE001
        pass
    try:
        s["mem_used_gb"], s["mem_total_gb"], s["mem_percent"] = _mem()
    except Exception:  # noqa: BLE001
        pass
    g = _gpu()
    if g:
        s["gpu_util"], s["vram_used_mb"], s["vram_total_mb"], s["vram_percent"] = g
    return s


class SysSampler:
    """后台每 1s 采一次系统资源，环形缓冲最近 N 点 → 页面画负载曲线。

    为什么 1s：页面 10s 刷一次，请求里现采是稀疏瞬时值——采样点随机落在 step 不同相位，
    GPU 读数会在 58~100 间跳（混叠，不是真的掉）。"""

    def __init__(self, interval=1.0, keep=1800):   # 30 分钟
        self.interval = interval
        self.buf = deque(maxlen=keep)
        self._stop = threading.Event()
        self._th = None

    def start(self):
        if self._th is not None:
            return
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.buf.append(collect_sys())
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self.interval)

    def snapshot(self):
        return list(self.buf)


SAMPLER = SysSampler()


# ---------------------------------------------------------------- 画图（服务端 SVG）
def _svg(series_list, title, logy=True, w=980, h=170, refs=None, fmt="%.4g", ylim=None):
    """series_list: [(label, values, color)]；values 里允许 None。ylim 固定纵轴，否则自适应。"""
    pad_l, pad_r, pad_t, pad_b = 62, 12, 22, 22
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    pts_all = [v for _, vs, _ in series_list for v in vs if v is not None]
    if not pts_all:
        return ('<div class="chart"><div class="ct">%s</div>'
                '<div class="empty">暂无数据</div></div>' % title)

    import math
    if ylim is not None:
        lo, hi = ylim
        tf = lambda v: (v - lo) / (hi - lo or 1)
    elif logy:
        vals = [abs(v) for v in pts_all if v > 0] or [1e-6]
        lo, hi = min(vals), max(vals)
        lo = max(lo, hi * 1e-4)
        tf = lambda v: (math.log10(max(abs(v), lo)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo) or 1)
    else:
        lo, hi = min(pts_all), max(pts_all)
        if hi == lo:
            hi = lo + 1
        lo = min(lo, 0)
        tf = lambda v: (v - lo) / (hi - lo)

    n = max(len(vs) for _, vs, _ in series_list)
    def X(i):
        return pad_l + (iw * i / max(n - 1, 1))
    def Y(v):
        return pad_t + ih * (1 - min(max(tf(v), 0.0), 1.0))

    parts = ['<svg viewBox="0 0 %d %d" class="svg" preserveAspectRatio="none">' % (w, h)]
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + ih * (1 - f)
        val = (10 ** (math.log10(lo) + f * (math.log10(hi) - math.log10(lo)))) if logy else (lo + f * (hi - lo))
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#2a2f3a" stroke-width="1"/>'
                     % (pad_l, y, w - pad_r, y))
        parts.append('<text x="%d" y="%.1f" fill="#7b8494" font-size="11" text-anchor="end">%s</text>'
                     % (pad_l - 6, y + 4, fmt % val))
    if refs:
        for rv, rl in refs:
            if lo <= rv <= hi:
                parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#c9a227" '
                             'stroke-width="1" stroke-dasharray="5,4"/>' % (pad_l, Y(rv), w - pad_r, Y(rv)))
                parts.append('<text x="%d" y="%.1f" fill="#c9a227" font-size="11">%s</text>'
                             % (w - pad_r - 4, Y(rv) - 4, rl))
    for label, vs, color in series_list:
        seg = []
        for i, v in enumerate(vs):
            if v is None:
                if len(seg) > 1:
                    parts.append('<polyline fill="none" stroke="%s" stroke-width="1.8" points="%s"/>'
                                 % (color, " ".join(seg)))
                seg = []
                continue
            seg.append("%.1f,%.1f" % (X(i), Y(v)))
        if len(seg) > 1:
            parts.append('<polyline fill="none" stroke="%s" stroke-width="1.8" points="%s"/>'
                         % (color, " ".join(seg)))
    parts.append('</svg>')

    legend = " ".join('<span class="lg"><i style="background:%s"></i>%s</span>' % (c, l)
                      for l, _, c in series_list)
    return ('<div class="chart"><div class="ct">%s%s</div>%s</div>'
            % (title, ('<span class="leg">%s</span>' % legend) if len(series_list) > 1 else "", "".join(parts)))


# ---------------------------------------------------------------- 面板
def _last(vs):
    return next((v for v in reversed(vs) if v is not None), None)


def build_panel(d, total_steps=0, out_dir=""):
    tr, ts = d["train"], d["train"]["steps"]
    h = []
    last = ts[-1] if ts else None

    age = time.time() - d["meta"]["mtime"] if d["meta"].get("mtime") else None
    if d["meta"].get("error"):
        status, cls = d["meta"]["error"], "bad"
    elif age is not None and age < 600:
        status, cls = "运行中（日志 %ds 前更新）" % int(age), "ok"
    else:
        status, cls = "已停滞 / 结束（日志 %.1f 分钟未更新）" % ((age or 0) / 60.0), "warn"
    s = tr["series"]
    h.append('<div class="row"><span class="pill %s">%s</span>' % (cls, status))
    if last is not None:
        h.append('<span class="kv">step <b>%s</b>%s</span>' % (
            f"{last:,}", (" / %s" % f"{total_steps:,}") if total_steps else ""))
        for k, fmt, unit in (("lam", "%.3g", ""), ("cfm", "%.4f", ""), ("acc", "%.4f", ""),
                             ("s_per_it", "%.2f", " s/it"), ("eta_h", "%.2f", " h 剩"),
                             ("data_frac", "%.2f", ""), ("mem_gb", "%.1f", " GB")):
            v = _last(s[k])
            if v is not None:
                h.append('<span class="kv">%s <b>%s</b>%s</span>' % (k, fmt % v, unit))
    h.append('<span class="kv">日志 %s 行</span></div>' % f"{d['meta']['lines']:,}")

    # 系统资源（1s 采样窗口：当前 / 均值 / 峰值）
    try:
        snap = SAMPLER.snapshot()
        cur = snap[-1] if snap else collect_sys()

        def _agg(key):
            vs = [x[key] for x in snap if x.get(key) is not None]
            return (sum(vs) / len(vs) if vs else None, max(vs) if vs else None)

        def _line(key, dec="%.0f"):
            m, p = _agg(key)
            return "%s · 均%s 峰%s" % (_n(cur.get(key), dec), _n(m, dec), _n(p, dec))

        items = [("CPU%%".replace("%%", "%"), _line("cpu_percent")),
                 ("内存", "%s/%s GB · 均%s 峰%s" % (_n(cur.get("mem_used_gb"), "%.1f"),
                                                 _n(cur.get("mem_total_gb"), "%.1f"),
                                                 _n(_agg("mem_percent")[0]), _n(_agg("mem_percent")[1]))),
                 ("GPU", _line("gpu_util")),
                 ("显存", "%s/%s MB · 均%s 峰%s" % (_n(cur.get("vram_used_mb")), _n(cur.get("vram_total_mb")),
                                                 _n(_agg("vram_percent")[0]), _n(_agg("vram_percent")[1])))]
        h.append('<div class="row">' + "".join(
            '<span class="kv">%s <b>%s</b></span>' % (k, v) for k, v in items) + '</div>')
    except Exception:  # noqa: BLE001
        pass

    # 训练曲线
    h.append(_svg([("cfm", s["cfm"], "#5aa9ff")], "CFM 损失（对数轴）"))
    if any(v is not None for v in s["ce"]):
        h.append(_svg([("ce(池)", s["ce"], "#e0607e"), ("ce_x1(X₁)", s["ce_x1"], "#b07bd6")],
                      "判别器 CE（对数轴）"))
    if any(v is not None for v in s["acc"]):
        h.append(_svg([("acc", s["acc"], "#f2a33c")], "判别器 acc（对数轴；下降 = GRL 起效）"))
    if any(v is not None for v in s["lam"]):
        h.append(_svg([("λ", s["lam"], "#63d29b")], "λ 日程（gain_shuf 门控渐开）", logy=False))
    if any(v is not None for v in s["gnorm"]):
        h.append(_svg([("gnorm", s["gnorm"], "#b07bd6")], "梯度范数（对数轴）"))

    # 验证曲线（主判据）：每 split 一张
    for sp in sorted(d["val"]):
        v = d["val"][sp]["series"]
        h.append(_svg([("gain_shuf", v["gain_shuf"], "#63d29b"), ("gain_null", v["gain_null"], "#5aa9ff"),
                       ("clip", v["clip"], "#f2a33c"), ("ident", v["ident"], "#e0607e")],
                      "验证[%s] 主判据（线性，0 参考线）：gain_shuf↑ 且 >0.001 开 λ；GRL 组 ident→0"
                      % sp, logy=False, refs=[(0.0, "0"), (0.001, "λ 门控 0.001")], fmt="%.3g"))
        h.append(_svg([("L_c", v["L_c"], "#63d29b"), ("L_shuf", v["L_shuf"], "#5aa9ff"),
                       ("L_null", v["L_null"], "#7b8494"), ("L_same", v["L_same"], "#f2a33c"),
                       ("L_other", v["L_other"], "#e0607e")],
                      "验证[%s] 五条件损失（对数轴，同一 X₂ 只换 M）" % sp))

    # 负载曲线（1s 采样，固定 0-100% 纵轴）
    snap = SAMPLER.snapshot()
    if snap:
        h.append(_svg([("GPU 利用率 %", [x["gpu_util"] for x in snap], "#5aa9ff"),
                       ("显存占用 %", [x["vram_percent"] for x in snap], "#f2a33c")],
                      "负载①：GPU / 显存（每秒采样，最近 %d s）" % len(snap),
                      logy=False, ylim=(0, 100), fmt="%.0f"))
        h.append(_svg([("CPU %", [x["cpu_percent"] for x in snap], "#63d29b"),
                       ("内存占用 %", [x["mem_percent"] for x in snap], "#e0607e")],
                      "负载②：CPU / 内存（每秒采样，最近 %d s）" % len(snap),
                      logy=False, ylim=(0, 100), fmt="%.0f"))

    # 最近 8 个 train 记录
    if ts:
        cols = ["step", "cfm", "ce", "acc", "lam", "gnorm", "data_frac", "s_per_it"]
        h.append('<table class="tbl"><tr>' + "".join("<th>%s</th>" % c for c in cols) + "</tr>")
        for i in range(max(0, len(ts) - 8), len(ts)):
            cells = [f"{ts[i]:,}"] + [
                ("%.4f" % s[c][i]) if s[c][i] is not None else "—" for c in cols[1:]]
            h.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells))
        h.append("</table>")

    # 验证记录表（每 split 最近 6 条）
    for sp in sorted(d["val"]):
        vd = d["val"][sp]
        if not vd["steps"]:
            continue
        cols = ["step", "L_c", "gain_shuf", "gain_null", "clip", "ident"]
        h.append('<div class="ct">验证[%s] 最近记录</div><table class="tbl"><tr>' % sp
                 + "".join("<th>%s</th>" % c for c in cols) + "</tr>")
        for i in range(max(0, len(vd["steps"]) - 6), len(vd["steps"])):
            vs = vd["series"]
            cells = [f"{vd['steps'][i]:,}"] + [
                ("%+.4f" % vs[c][i]) if c.startswith(("gain", "clip", "ident")) and vs[c][i] is not None
                else (("%.4f" % vs[c][i]) if vs[c][i] is not None else "—") for c in cols[1:]]
            h.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells))
        h.append("</table>")

    # ckpt（扫 out 目录的实际文件）
    if out_dir and os.path.isdir(out_dir):
        ck = []
        for fn in sorted(os.listdir(out_dir)):
            if fn == "last.pt" or (fn.startswith("ckpt_") and fn.endswith(".pt")):
                p = os.path.join(out_dir, fn)
                stt = os.stat(p)
                ck.append((stt.st_mtime, fn, stt.st_size))
        if ck:
            ck.sort(reverse=True)
            h.append('<div class="ct">ckpt（out 目录实扫，最近 8 个）</div>'
                     '<table class="tbl"><tr><th>文件</th><th>大小</th><th>mtime</th></tr>')
            for mt, fn, sz in ck[:8]:
                h.append("<tr><td>%s</td><td>%.1f MB</td><td>%s</td></tr>"
                         % (fn, sz / 1048576.0, time.strftime("%m-%d %H:%M:%S", time.localtime(mt))))
            h.append("</table>")

    h.append('<div class="ct">日志尾部（%d 行）<a class="lnk" href="/raw?n=500">原始 JSONL</a></div>'
             '<pre class="tail" id="tail">%s</pre>' % (len(d["tail"]), escape("\n".join(d["tail"]))))
    return "\n".join(h)


def escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


CSS = """
:root{color-scheme:dark}
body{margin:0;background:#12151c;color:#dfe4ee;font:13px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,"Microsoft YaHei",sans-serif}
header{padding:12px 18px;background:#171b24;border-bottom:1px solid #262c38;position:sticky;top:0;z-index:9}
h1{margin:0;font-size:15px;font-weight:600;display:inline-block}
header .sub{color:#7b8494;font-size:12px;margin-left:10px}
header .tools{float:right;font-size:12px;color:#7b8494}
main{padding:14px 18px 40px}
.row{margin:6px 0 10px}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;margin-right:8px}
.pill.ok{background:#14351f;color:#63d29b;border:1px solid #245c36}
.pill.warn{background:#3a2f12;color:#f2c14e;border:1px solid #6b5518}
.pill.bad{background:#3a1620;color:#e0607e;border:1px solid #6d2337}
.kv{display:inline-block;margin-right:12px;color:#9aa3b2}
.kv b{color:#dfe4ee}
.chart{margin:10px 0 16px;background:#151922;border:1px solid #232936;border-radius:6px;padding:8px 10px}
.ct{color:#9aa3b2;font-size:12px;margin:2px 0 6px}
.leg{margin-left:12px}
.lg{margin-right:10px}
.lg i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
.svg{width:100%;height:170px;display:block}
.empty{color:#5c6472;padding:24px 0;text-align:center}
.tbl{width:100%;border-collapse:collapse;font-family:ui-monospace,Consolas,monospace;font-size:12px;margin:4px 0 14px}
.tbl th{color:#7b8494;text-align:right;font-weight:500;border-bottom:1px solid #262c38;padding:3px 6px}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.tbl td{text-align:right;padding:3px 6px;border-bottom:1px solid #1b202a}
.tail{background:#0e1117;border:1px solid #232936;border-radius:6px;padding:10px;max-height:340px;overflow:auto;font-size:12px;white-space:pre-wrap}
.lnk{float:right;color:#5aa9ff;text-decoration:none}
"""

JS = """
let paused=false;
function tick(){
  if(paused) return;
  fetch('/partial').then(r=>r.text()).then(t=>{
    document.getElementById('panel').innerHTML=t;
    const t2=document.getElementById('tail'); if(t2) t2.scrollTop=t2.scrollHeight;
  }).catch(()=>{});
}
function toggle(){
  paused=!paused;
  document.getElementById('btn').textContent = paused ? '继续刷新' : '暂停刷新';
}
setInterval(tick,10000);
"""


def make_page(title, log_path, total_steps, out_dir):
    d = parse_log(log_path)
    panel = build_panel(d, total_steps, out_dir)
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>%s</title><style>%s</style></head><body>
<header>
  <h1>%s</h1><span class="sub">%s</span>
  <span class="tools"><button id="btn" onclick="toggle()">暂停刷新</button>
  每 10s 自动刷新 · <a class="lnk" href="/api/data">JSON</a></span>
</header>
<main><div id="panel">%s</div></main>
<script>%s</script></body></html>""" % (escape(title), CSS, escape(title), escape(log_path), panel, JS)


class Handler(BaseHTTPRequestHandler):
    log_path = ""
    out_dir = ""
    total_steps = 0
    title = "v6 训练监控"

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/health":
                return self._send(200, "ok", "text/plain")
            if path == "/api/data":
                d = parse_log(self.log_path)
                d["meta"]["now"] = time.time()
                d["meta"]["sys"] = collect_sys()
                return self._send(200, json.dumps(d, ensure_ascii=False), "application/json; charset=utf-8")
            if path == "/api/sys":
                return self._send(200, json.dumps(collect_sys(), ensure_ascii=False),
                                  "application/json; charset=utf-8")
            if path == "/raw":
                n = 200
                if "n=" in self.path:
                    try:
                        n = int(self.path.split("n=")[1].split("&")[0])
                    except ValueError:
                        pass
                if not os.path.isfile(self.log_path):
                    return self._send(404, "log not found", "text/plain; charset=utf-8")
                with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-n:]
                return self._send(200, "".join(lines), "text/plain; charset=utf-8")
            if path == "/partial":
                return self._send(200, build_panel(parse_log(self.log_path), self.total_steps, self.out_dir))
            return self._send(200, make_page(self.title, self.log_path, self.total_steps, self.out_dir))
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            return self._send(500, "monitor error: %r" % (e,), "text/plain; charset=utf-8")

    def log_message(self, *a):  # 静音访问日志
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="v6 train.py 的 log.jsonl")
    ap.add_argument("--out-dir", default="", help="训练 out 目录（ckpt 表用；默认 = log 所在目录）")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--total-steps", type=int, default=0, help="总步数（0 = 从 out/args.json 自动读）")
    ap.add_argument("--title", default="")
    a = ap.parse_args()
    out_dir = a.out_dir or os.path.dirname(os.path.abspath(a.log))
    total = a.total_steps
    title = a.title or "v6 训练监控 · %s" % os.path.basename(out_dir.rstrip("/\\"))

    if not total:                                   # 从 args.json 自动读 steps
        try:
            args = json.load(open(os.path.join(out_dir, "args.json"), encoding="utf-8"))
            total = int(args.get("steps", 0))
        except Exception:  # noqa: BLE001
            pass

    Handler.log_path = a.log
    Handler.out_dir = out_dir
    Handler.total_steps = total
    Handler.title = title
    SAMPLER.start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("[monitor] http://%s:%d/  log=%s  out=%s  total_steps=%d" % (a.host, a.port, a.log, out_dir, total),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
