#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动 res_cap 冒烟：λ_pred=1 + res_cap=0.5，2000 步（detached）。

判据（对照 long_B 的 λ=1/no-cap：条件贡献 ≈0.01、|pro|+|res|≈9.9 而 |P2|≈2.4）：
  ① 条件贡献是否显著抬高（目标 >0.1）
  ② 码能量占比是否从"码流 80%"扭向 P 主载体
  ③ 音质不崩（stft/wav/lat 与 baseline 同量级）
"""
import subprocess
import sys
import time

LOG = "/content/cap05.log"
CMD = [sys.executable, "-u", "/content/stage1_code/stage1_train.py",
       "--platform", "colab", "--wav-root", "/content/wav",
       "--data-root", "/content/stage1_data",
       "--dtype", "bf16", "--batch", "10", "--steps", "2000",
       "--eval-every", "500", "--probe-every", "500", "--ckpt-every", "1000",
       "--lambda-pred", "1", "--res-cap", "0.5", "--out", "/content/cap05"]

with open(LOG, "ab") as f:
    p = subprocess.Popen(CMD, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         start_new_session=True)
print("cap05 pid =", p.pid, flush=True)
time.sleep(25)
print(subprocess.run(
    ["bash", "-lc", "pgrep -af stage1_train | head -2; echo ----; tail -8 " + LOG],
    capture_output=True, text=True).stdout)