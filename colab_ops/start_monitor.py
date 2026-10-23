#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 Colab VM 上启动训练监控页（detached，可存活于内核重启）。

等价于 ssh 进去跑 `bash /content/start_monitor.sh`，但可经 `colab exec -f` 触发。
用法：
  colab upload -s <session> colab_ops/monitor_server.py /content/monitor_server.py
  colab exec -s <session> -f colab_ops/start_monitor.py
环境变量：MON_LOG（默认 /content/stage1_out/train_L4.log）、MON_PORT（默认 8000）。
"""
import os
import subprocess
import sys
import time

LOG = os.environ.get("MON_LOG", "/content/stage1_out/train_L4.log")
PORT = os.environ.get("MON_PORT", "8000")
OUT_DIR = os.path.dirname(LOG)
os.makedirs(OUT_DIR, exist_ok=True)

subprocess.run(["bash", "-lc", "pkill -f monitor_server.py; sleep 1; true"])

with open("/content/monitor.out", "ab") as f:
    p = subprocess.Popen(
        [sys.executable, "-u", "/content/monitor_server.py",
         "--log", LOG, "--out-dir", OUT_DIR, "--port", PORT,
         "--title", "stage1 训练监控"],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True,   # 脱离内核与调用方
    )
print("monitor_server pid =", p.pid)
time.sleep(3)
print(subprocess.run(
    ["bash", "-lc",
     'curl -s -m 8 -o /dev/null -w "health=%%{http_code}\\n" http://127.0.0.1:%s/health; '
     "pgrep -af monitor_server.py | head -1" % PORT],
    capture_output=True, text=True).stdout)
print("本机隧道: ssh -F <ssh_config> -N -L 8010:127.0.0.1:%s colabmon" % PORT)
