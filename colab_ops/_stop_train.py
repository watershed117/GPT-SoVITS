#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停掉当前 stage1_train 训练（pgrep 取 PID + 排除自身，不用 pkill -f 以免自匹配）。"""
import os
import signal
import subprocess
import time

ME = os.getpid()
out = subprocess.run(["pgrep", "-af", "stage1_train"], capture_output=True, text=True).stdout
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
print("已停:", hit, flush=True)
time.sleep(4)
print("剩余:", subprocess.run(["pgrep", "-af", "stage1_train"],
                            capture_output=True, text=True).stdout.strip() or "无", flush=True)