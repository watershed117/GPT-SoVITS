#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切换冒烟：停掉 cap=0.5，改跑 cap=0.3（并把监控切到新日志）。

为什么换 0.3：残差两级串联，可覆盖总量 ≤ 2c|E|；补偿需要覆盖 ≈|E|，
故 2c<1 才堵得住。c=0.5 → 2c=1.0 刚好够用（实测 @500 无改善，与推算一致）；
c=0.3 → 2c=0.6，缺口 0.4|E| 只能由 P 承担 ⇒ 逼 P 变准、消融 sem 才会伤重建。

坑：不要用 pkill -f（会匹配到执行它的 shell）。统一 pgrep 取 PID + 排除自身。
"""
import os
import signal
import subprocess
import sys
import time

ME = os.getpid()


def kill_matching(keyword):
    out = subprocess.run(["pgrep", "-af", keyword], capture_output=True, text=True).stdout
    hit = []
    for line in out.splitlines():
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == ME or "pgrep" in cmd:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            hit.append(pid)
        except ProcessLookupError:
            pass
    return hit


print("停止旧训练:", kill_matching("stage1_train"), flush=True)
time.sleep(5)

LOG = "/content/cap03.log"
with open(LOG, "ab") as f:
    p = subprocess.Popen(
        [sys.executable, "-u", "/content/stage1_code/stage1_train.py",
         "--platform", "colab", "--wav-root", "/content/wav",
         "--data-root", "/content/stage1_data",
         "--dtype", "bf16", "--batch", "10", "--steps", "2000",
         "--eval-every", "500", "--probe-every", "500", "--ckpt-every", "1000",
         "--lambda-pred", "1", "--res-cap", "0.3", "--out", "/content/cap03"],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
print("cap03 pid =", p.pid, flush=True)

print("重启监控:", kill_matching("monitor_server.py"), flush=True)
time.sleep(2)
with open("/content/monitor.out", "ab") as f:
    q = subprocess.Popen(
        [sys.executable, "-u", "/content/monitor_server.py", "--log", LOG,
         "--out-dir", "/content", "--port", "8000", "--title", "stage1 训练监控"],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL, start_new_session=True)
print("monitor pid =", q.pid, flush=True)

time.sleep(20)
print(subprocess.run(
    ["bash", "-lc", "pgrep -af stage1_train | head -2; echo ----; tail -6 " + LOG],
    capture_output=True, text=True).stdout)