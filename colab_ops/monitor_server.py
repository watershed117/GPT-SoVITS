#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage1 训练监控页（Colab VM 侧，纯标准库，无外部依赖）。

解析 stage1_train.py 的日志格式，渲染成一个自刷新网页：

  [  1202.7s] step    400: loss 369.2118 (stft 0.1437 wav 0.0399 lat 6.7376
                cp 251.084 cr 123.342) | 1.10 s/步 | mem 1763MB |E| 4.03 |P| 4.02 |P2| 8.05
  [ 37640.1s] ckpt saved: /content/stage1_out/ckpt.pt (step 33000)
  [   ...   ] battery @ step 2000 ...
                GT: lat 7.3900 cos 0.2980 rms 0.3150
                条件贡献 ZERO-GT = +0.0200
                探针 E→Sem: 0.0612

用法（VM 上）：
  nohup python3 monitor_server.py --log /content/stage1_out/train_L4.log \
        --out-dir /content/stage1_out --port 8000 > /content/monitor.log 2>&1 &

端点：
  /            网页（每 10s 自动刷新，可暂停）
  /partial     只返回面板 HTML 片段（网页用它做局部刷新）
  /api/data    JSON（所有解析结果，方便脚本消费）
  /api/sys     JSON（CPU / 内存 / GPU 利用率 / 显存，实时采样）
  /raw?n=N     日志尾部 N 行原始文本
  /health      ok
"""
import argparse
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 解析

RE_STEP = re.compile(
    r"step\s+(\d+):\s+loss\s+([-\d.eE+]+)\s*\(stft\s+([-\d.eE+]+)\s+wav\s+([-\d.eE+]+)"
    r"\s+lat\s+([-\d.eE+]+)\s+cp\s+([-\d.eE+]+)\s+cr\s+([-\d.eE+]+)\)"
)
RE_RATE = re.compile(r"\|\s*([\d.]+)\s*s/")
RE_MEM = re.compile(r"mem\s+([\d.]+)MB")
RE_E = re.compile(r"\|E\|\s+([\d.]+)")
RE_P = re.compile(r"\|P\|\s+([\d.]+)")
RE_P2 = re.compile(r"\|P2\|\s+([\d.]+)")
RE_CKPT = re.compile(r"ckpt saved:\s*(\S+)\s*\(step\s+(\d+)\)")
RE_BATT = re.compile(r"battery @ step (\d+)")
RE_STATE = re.compile(r"\b(GT|ZERO|SHUF):\s*lat\s+([\d.]+)\s+cos\s+([-\d.]+)\s+rms\s+([\d.]+)")
RE_COND = re.compile(r"条件贡献 ZERO-GT\s*=\s*([+-][\d.]+)")
RE_PROBE = re.compile(r"探针\s+(\w+)→Sem:\s*([\d.]+)")
RE_ELAPSED = re.compile(r"^\s*\[\s*([\d.]+)s\]")
RE_BAD = re.compile(r"!!|非有限|Traceback|Error|FAIL")
RE_DONE = re.compile(r"\bDONE\b|到达 --max-hours|到点存盘退出")
# 多臂脚本(如 smoke_pred.sh)在同一日志里顺序输出,用 ARM 标记切段 → 面板只显示当前臂
# 需带 `|` 才匹配:开始行是 "=== ARM A | lambda_pred=0 ...",结束行 "=== ARM A 结束 ..." 不匹配
RE_ARM = re.compile(r"===\s*ARM\s+(\w+)\s*\|")

FIELDS = ["loss", "stft", "wav", "lat", "cp", "cr"]


def parse_log(path, tail_lines=250):
    """返回 (series, ckpts, batteries, alerts, tail, meta)。日志不大，每次请求全量解析。"""
    out = {"series": {k: [] for k in FIELDS + ["rate", "mem", "E", "P", "P2"]},
           "steps": [], "ckpts": [], "batteries": [], "alerts": [], "tail": [],
           "meta": {"lines": 0, "mtime": None, "size": None, "done": False, "last_step": None}}
    if not os.path.isfile(path):
        out["meta"]["error"] = "log not found: %s" % path
        return out

    st = os.stat(path)
    out["meta"]["mtime"] = st.st_mtime
    out["meta"]["size"] = st.st_size

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        out["meta"]["error"] = str(e)
        return out

    out["meta"]["lines"] = len(lines)

    # 臂感知:同一日志里多臂顺序输出时,只解析**最后一段**(当前臂),
    # 否则两臂 step 都从 1 开始,会混在同一张图上、看起来"没重置"。
    arm_idx = None
    arm_name = None
    arm_count = 0
    for i, ln in enumerate(lines):
        m = RE_ARM.search(ln)
        if m:
            arm_idx, arm_name = i, m.group(1)
            arm_count += 1
    if arm_idx is not None:
        out["meta"]["arm"] = arm_name
        out["meta"]["arm_count"] = arm_count
        lines = lines[arm_idx:]

    cur_batt = None
    for ln in lines:
        m = RE_STEP.search(ln)
        if m:
            step = int(m.group(1))
            out["steps"].append(step)
            for i, k in enumerate(FIELDS):
                out["series"][k].append(float(m.group(2 + i)))
            r = RE_RATE.search(ln)
            out["series"]["rate"].append(float(r.group(1)) if r else None)
            mm = RE_MEM.search(ln)
            out["series"]["mem"].append(float(mm.group(1)) if mm else None)
            for k, rx in (("E", RE_E), ("P", RE_P), ("P2", RE_P2)):
                x = rx.search(ln)
                out["series"][k].append(float(x.group(1)) if x else None)
            out["meta"]["last_step"] = step
            continue

        m = RE_CKPT.search(ln)
        if m:
            out["ckpts"].append({"path": m.group(1), "step": int(m.group(2))})
            continue

        m = RE_BATT.search(ln)
        if m:
            cur_batt = {"step": int(m.group(1)), "states": {}, "cond": None, "probes": {}}
            out["batteries"].append(cur_batt)
            continue

        m = RE_STATE.search(ln)
        if m and cur_batt is not None:
            cur_batt["states"][m.group(1)] = {"lat": float(m.group(2)),
                                              "cos": float(m.group(3)),
                                              "rms": float(m.group(4))}
            continue

        m = RE_COND.search(ln)
        if m and cur_batt is not None:
            cur_batt["cond"] = float(m.group(1))
            continue

        m = RE_PROBE.search(ln)
        if m and cur_batt is not None:
            cur_batt["probes"][m.group(1)] = float(m.group(2))
            continue

        if RE_BAD.search(ln):
            out["alerts"].append(ln.rstrip()[:300])
        if RE_DONE.search(ln):
            out["meta"]["done"] = True

    out["tail"] = [l.rstrip() for l in lines[-tail_lines:]]
    return out


# ---------------------------------------------------------------- 系统资源

def _n(v, f="%.0f"):
    """None 安全的数字格式化（采样失败显示 —）。"""
    return (f % v) if v is not None else "—"


def _cpu_percent():
    """两次读 /proc/stat 求差（0.15s 窗口）得整机 CPU 占用率。"""
    def snap():
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
        return sum(v), idle

    t1, i1 = snap()
    time.sleep(0.15)
    t2, i2 = snap()
    dt = t2 - t1
    return 100.0 * (1.0 - (i2 - i1) / dt) if dt > 0 else None


def _mem():
    """/proc/meminfo → (used GB, total GB, used %)。"""
    info = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, _, v = ln.partition(":")
            info[k] = float(v.split()[0])  # 单位 kB
    total = info["MemTotal"] / 1048576.0
    used = (info["MemTotal"] - info.get("MemAvailable", info["MemFree"])) / 1048576.0
    return used, total, 100.0 * used / total


def _gpu():
    """nvidia-smi → (利用率 %, 显存 used MiB, 显存 total MiB, 显存 %)；不可用时 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        util, mu, mt = [float(x) for x in out.splitlines()[0].split(",")]
        return util, mu, mt, 100.0 * mu / mt
    except Exception:  # noqa: BLE001  # 无 GPU / 无 nvidia-smi / 输出异常统一降级
        return None


def collect_sys():
    """一次系统资源采样；单项失败置 None，不抛异常。"""
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
    """后台每 1s 采一次系统资源,环形缓冲最近 N 点 → 页面画负载曲线。

    为什么要它:页面每 10s 才刷一次,若在请求里现采,得到的是**稀疏瞬时值**
    (10s 里约 7 个 step,采样点随机落在 step 不同相位 → GPU 读数在 58~100 间跳)。
    1s 密度采样 + 画曲线才能看出真实分布与长时低占用。
    """

    def __init__(self, interval=1.0, keep=1800):   # keep=1800 → 30 分钟
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
            except Exception:  # noqa: BLE001  # 采样失败不致命
                pass
            self._stop.wait(self.interval)

    def snapshot(self):
        return list(self.buf)


SAMPLER = SysSampler()


# ---------------------------------------------------------------- 画图（服务端 SVG）

def _svg(series_list, title, logy=True, w=980, h=170, refs=None, fmt="%.4g", ylim=None):
    """series_list: [(label, values, color)]；values 里允许 None（缺列）。
    ylim=(lo, hi) 时用固定纵轴（负载曲线用 0-100），否则按数据范围自适应。"""
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
        tf = lambda v: (v - lo) / (hi - lo)

    n = max(len(vs) for _, vs, _ in series_list)
    def X(i):
        return pad_l + (iw * i / max(n - 1, 1))
    def Y(v):
        return pad_t + ih * (1 - min(max(tf(v), 0.0), 1.0))

    parts = ['<svg viewBox="0 0 %d %d" class="svg" preserveAspectRatio="none">' % (w, h)]
    # 网格 + y 轴刻度
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
        seg, started = [], False
        for i, v in enumerate(vs):
            if v is None:
                if len(seg) > 1:
                    parts.append('<polyline fill="none" stroke="%s" stroke-width="1.8" points="%s"/>'
                                 % (color, " ".join(seg)))
                seg, started = [], False
                continue
            seg.append("%.1f,%.1f" % (X(i), Y(v)))
            started = True
        if len(seg) > 1:
            parts.append('<polyline fill="none" stroke="%s" stroke-width="1.8" points="%s"/>'
                         % (color, " ".join(seg)))
    parts.append('</svg>')

    legend = " ".join('<span class="lg"><i style="background:%s"></i>%s</span>' % (c, l)
                      for l, _, c in series_list)
    return ('<div class="chart"><div class="ct">%s%s</div>%s</div>'
            % (title, ('<span class="leg">%s</span>' % legend) if len(series_list) > 1 else "", "".join(parts)))


def build_panel(d, total_steps=0):
    s, steps = d["series"], d["steps"]
    h = []
    last = steps[-1] if steps else None

    # 顶部状态
    age = time.time() - d["meta"]["mtime"] if d["meta"].get("mtime") else None
    if d["meta"].get("error"):
        status, cls = d["meta"]["error"], "bad"
    elif d["meta"]["done"]:
        status, cls = "已结束（日志出现 DONE/退出标记）", "ok"
    elif age is not None and age < 180:
        status, cls = "运行中（日志 %ds 前更新）" % int(age), "ok"
    else:
        status, cls = "已停滞（日志 %.1f 分钟未更新）" % ((age or 0) / 60.0), "warn"

    rate = next((v for v in reversed(s["rate"]) if v), None)
    mem = next((v for v in reversed(s["mem"]) if v), None)
    eta = ""
    if rate and total_steps and last and last < total_steps:
        eta = " · 剩余 %d 步 ≈ %.1f h" % (total_steps - last, (total_steps - last) * rate / 3600.0)

    h.append('<div class="row">')
    h.append('<span class="pill %s">%s</span>' % (cls, status))
    if d["meta"].get("arm"):
        h.append('<span class="kv">当前臂 <b>%s</b>（共 %d 臂，只显示本臂）</span>'
                 % (d["meta"]["arm"], d["meta"]["arm_count"]))
    if last:
        h.append('<span class="kv">step <b>%s</b></span>' % f"{last:,}")
        h.append('<span class="kv">日志 %s 行</span>' % f"{d['meta']['lines']:,}")
        h.append('<span class="kv">共 %d 个记录点</span>' % len(steps))
    if rate:
        h.append('<span class="kv">%.2f s/步%s</span>' % (rate, eta))
    if mem:
        h.append('<span class="kv">mem %.0fMB</span>' % mem)
    h.append('</div>')

    # 系统资源（后台 1s 采样窗口：当前 / 均值 / 峰值）
    try:
        snap = SAMPLER.snapshot()
        cur = snap[-1] if snap else collect_sys()

        def _agg(key):
            vs = [x[key] for x in snap if x.get(key) is not None]
            return (sum(vs) / len(vs) if vs else None, max(vs) if vs else None)

        def _line(key, unit, dec="%.0f"):
            m, p = _agg(key)
            return "%s%s · 均%s 峰%s" % (_n(cur.get(key), dec), unit, _n(m, dec), _n(p, dec))

        items = [
            ("CPU", _line("cpu_percent", "%")),
            ("系统内存", "%s / %s GB (%s) · 均%s 峰%s" % (
                _n(cur.get("mem_used_gb"), "%.1f"), _n(cur.get("mem_total_gb"), "%.1f"),
                _n(cur.get("mem_percent")), _n(_agg("mem_percent")[0]), _n(_agg("mem_percent")[1]))),
            ("GPU", _line("gpu_util", "%")),
            ("显存", "%s / %s MB (%s) · 均%s 峰%s" % (
                _n(cur.get("vram_used_mb")), _n(cur.get("vram_total_mb")),
                _n(cur.get("vram_percent")), _n(_agg("vram_percent")[0]), _n(_agg("vram_percent")[1]))),
            ("窗口", "%d s" % len(snap)),
        ]
        h.append('<div class="row">' + "".join(
            '<span class="kv">%s <b>%s</b></span>' % (k, v) for k, v in items) + '</div>')
    except Exception:  # noqa: BLE001  # 采样异常不影响面板其余部分
        pass

    if d["alerts"]:
        h.append('<div class="alerts"><b>告警（最近 5 条）</b><pre>%s</pre></div>'
                 % "\n".join(d["alerts"][-5:]))

    # 图
    h.append(_svg([("loss", s["loss"], "#5aa9ff")], "总 loss（对数轴）"))
    h.append(_svg([("stft", s["stft"], "#5aa9ff"), ("wav", s["wav"], "#63d29b"),
                   ("lat", s["lat"], "#f2a33c")], "质量项：stft / wav / lat（对数轴）"))
    h.append(_svg([("cp", s["cp"], "#e0607e"), ("cr", s["cr"], "#b07bd6")],
                  "commit 项：cp / cr（对数轴）——v7 的爆炸信号"))
    if any(v is not None for v in s["P"]):
        ratio_p = [(p / e if (p is not None and e) else None) for p, e in zip(s["P"], s["E"])]
        ratio_p2 = [(p / e if (p is not None and e) else None) for p, e in zip(s["P2"], s["E"])]
        h.append(_svg([("|P|/|E|", ratio_p, "#63d29b"), ("|P2|/|E|", ratio_p2, "#e0607e")],
                      "尺度锚：|P|/|E| 与 |P2|/|E|（天花板 = 1.0，锚生效应 ≤1）",
                      logy=False, refs=[(1.0, "天花板 1.0")], fmt="%.2f"))
    else:
        h.append(_svg([("|E|", s["E"], "#5aa9ff"), ("|P|", s["P"], "#63d29b"),
                       ("|P2|", s["P2"], "#e0607e")], "尺度：|E| / |P| / |P2|（对数轴）"))

    # 负载曲线（后台 1s 采样，固定 0-100% 纵轴）
    snap = SAMPLER.snapshot()
    if snap:
        h.append(_svg([("GPU 利用率 %", [x["gpu_util"] for x in snap], "#5aa9ff"),
                       ("显存占用 %", [x["vram_percent"] for x in snap], "#f2a33c")],
                      "负载①：GPU 利用率 / 显存占用（每秒采样，最近 %d s）" % len(snap),
                      logy=False, ylim=(0, 100), fmt="%.0f"))
        h.append(_svg([("CPU %", [x["cpu_percent"] for x in snap], "#63d29b"),
                       ("内存占用 %", [x["mem_percent"] for x in snap], "#e0607e")],
                      "负载②：CPU / 内存占用（每秒采样，最近 %d s）" % len(snap),
                      logy=False, ylim=(0, 100), fmt="%.0f"))

    # 最近 8 个记录点
    if steps:
        h.append('<table class="tbl"><tr><th>step</th><th>loss</th><th>stft</th><th>wav</th>'
                 '<th>lat</th><th>cp</th><th>cr</th><th>|E|</th><th>|P|</th><th>|P2|</th>'
                 '<th>|P|/|E|</th><th>s/步</th></tr>')
        for i in range(max(0, len(steps) - 8), len(steps)):
            pe = (s["P"][i] / s["E"][i]) if (s["P"][i] and s["E"][i]) else None
            cells = [f"{steps[i]:,}"] + [("%.4f" % s[k][i]) if s[k][i] is not None else "—"
                                         for k in FIELDS[1:]] \
                    + [("%.2f" % s[k][i]) if s[k][i] is not None else "—" for k in ("E", "P", "P2")] \
                    + [("%.3f" % pe) if pe is not None else "—",
                       ("%.2f" % s["rate"][i]) if s["rate"][i] else "—"]
            h.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in cells))
        h.append("</table>")

    # battery
    if d["batteries"]:
        h.append('<div class="ct">battery 探针（判据：条件贡献 &gt; 0.1 才算条件解码器启动；'
                 '探针 →Sem 要显著高于 chance）</div>')
        h.append('<table class="tbl"><tr><th>step</th><th>GT lat</th><th>ZERO lat</th>'
                 '<th>条件贡献</th><th>GT cos</th><th>GT rms</th><th>探针 E→Sem</th>'
                 '<th>P→Sem</th><th>pro→Sem</th><th>res→Sem</th></tr>')
        for b in d["batteries"][-12:]:
            st = b["states"]
            h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class='%s'>%s</td><td>%s</td>"
                     "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                         f"{b['step']:,}",
                         ("%.4f" % st["GT"]["lat"]) if "GT" in st else "—",
                         ("%.4f" % st["ZERO"]["lat"]) if "ZERO" in st else "—",
                         "good" if (b["cond"] or 0) > 0.1 else "dim",
                         ("%+.4f" % b["cond"]) if b["cond"] is not None else "—",
                         ("%.4f" % st["GT"]["cos"]) if "GT" in st else "—",
                         ("%.4f" % st["GT"]["rms"]) if "GT" in st else "—",
                         ("%.4f" % b["probes"]["E"]) if "E" in b["probes"] else "—",
                         ("%.4f" % b["probes"]["P"]) if "P" in b["probes"] else "—",
                         ("%.4f" % b["probes"]["pro"]) if "pro" in b["probes"] else "—",
                         ("%.4f" % b["probes"]["res"]) if "res" in b["probes"] else "—"))
        h.append("</table>")

    # ckpt（日志记录 + 实际文件）
    h.append('<div class="ct">ckpt</div><table class="tbl"><tr><th>step</th><th>路径</th>'
             '<th>大小</th><th>mtime</th></tr>')
    seen = set()
    for c in d["ckpts"][-8:]:
        key = (c["step"], c["path"])
        if key in seen:
            continue
        seen.add(key)
        sz = mt = "—"
        if os.path.isfile(c["path"]):
            stt = os.stat(c["path"])
            sz = "%.1f MB" % (stt.st_size / 1048576.0)
            mt = time.strftime("%H:%M:%S", time.localtime(stt.st_mtime))
        h.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (f"{c['step']:,}", c["path"], sz, mt))
    h.append("</table>")

    # 日志尾
    h.append('<div class="ct">日志尾部（%d 行）<a class="lnk" href="/raw?n=500">原始文本</a></div>'
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
.tbl td.good{color:#63d29b;font-weight:600}
.tbl td.dim{color:#8b93a3}
.alerts{background:#2a1520;border:1px solid #6d2337;border-radius:6px;padding:8px 10px;margin:10px 0}
.alerts pre{margin:6px 0 0;white-space:pre-wrap;color:#f0a9bd;font-size:12px}
.tail{background:#0e1117;border:1px solid #232936;border-radius:6px;padding:10px;max-height:340px;overflow:auto;font-size:12px;white-space:pre-wrap}
.lnk{float:right;color:#5aa9ff;text-decoration:none}
"""

JS = """
let paused=false, timer=null;
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
timer=setInterval(tick,10000);
"""


def make_page(title, log_path, total_steps):
    d = parse_log(log_path)
    panel = build_panel(d, total_steps)
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
    title = "stage1 训练监控"

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
                d["meta"]["out_dir_listing"] = sorted(os.listdir(self.out_dir)) if os.path.isdir(self.out_dir) else []
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
                return self._send(200, build_panel(parse_log(self.log_path), self.total_steps))
            return self._send(200, make_page(self.title, self.log_path, self.total_steps))
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            return self._send(500, "monitor error: %r" % (e,), "text/plain; charset=utf-8")

    def log_message(self, *a):  # 静音访问日志
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/content/stage1_out/train_L4.log")
    ap.add_argument("--out-dir", default="/content/stage1_out")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--total-steps", type=int, default=0, help="用于算 ETA，0=不显示")
    ap.add_argument("--title", default="stage1 训练监控")
    a = ap.parse_args()

    Handler.log_path = a.log
    Handler.out_dir = a.out_dir
    Handler.total_steps = a.total_steps
    Handler.title = a.title
    SAMPLER.start()   # 后台 1s 采样线程（负载曲线用）
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("[monitor] http://%s:%d/  log=%s  out=%s" % (a.host, a.port, a.log, a.out_dir), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
