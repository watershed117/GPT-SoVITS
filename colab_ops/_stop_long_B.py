#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停掉 long-B 的 python 训练进程（修正版：用 pgrep 取 PID 再杀，避免 pkill -f 自匹配）。

坑：`pkill -f run_long_B.sh` 会把「执行它的那个 shell」也匹配上（其 cmdline 含该字符串）
→ shell 自杀、后续命令不执行、无输出；而真正的 python 进程 cmdline 不含 run_long_B.sh，
反而活了下来（表现为日志还在推进）。
"""
import os
import signal
import subprocess
import time

me = os.getpid()
out = subprocess.run(["pgrep", "-af", "stage1_train"], capture_output=True, text=True).stdout
killed = []
for line in out.splitlines():
    pid_s, _, cmd = line.partition(" ")
    try:
        pid = int(pid_s)
    except ValueError:
        continue
    if pid == me or "pgrep" in cmd:
        continue
    try:
        os.kill(pid, signal.SIGTERM)
        killed.append((pid, cmd[:90]))
    except ProcessLookupError:
        pass
print("已发送 SIGTERM:", killed, flush=True)
time.sleep(4)

print("剩余 stage1_train 进程:",
      subprocess.run(["pgrep", "-af", "stage1_train"], capture_output=True, text=True).stdout.strip() or "无",
      flush=True)
print("--- long_B 产物 ---", flush=True)
print(subprocess.run(["bash", "-lc", "ls -l /content/long_B 2>/dev/null; echo; tail -2 /content/longB.log"],
                     capture_output=True, text=True).stdout, flush=True)