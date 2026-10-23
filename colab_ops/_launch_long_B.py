#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经 colab exec 以 detached 方式启动延长 B 臂（等价 setsid nohup，可脱离内核存活）。"""
import subprocess
import time

with open("/content/longB.out", "ab") as f:
    p = subprocess.Popen(
        ["bash", "/content/stage1_code/run_long_B.sh"],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
print("long-B pid =", p.pid)
time.sleep(20)
print(subprocess.run(
    ["bash", "-lc",
     "pgrep -af run_long_B.sh | head -2; echo ----; tail -6 /content/longB.log 2>/dev/null"],
    capture_output=True, text=True).stdout)