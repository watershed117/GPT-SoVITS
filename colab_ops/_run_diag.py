#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑条件补偿诊断：加载 long_B / smoke_B 的 ckpt，读数后退出（不训练）。

用法（VM 经 colab exec -f）：本文件无参运行，会顺序诊断两个 ckpt。
"""
import subprocess
import sys

CKPTS = [
    ("long_B  (step2000, λ=1, 平凡态 +0.0107)", "/content/long_B/ckpt.pt"),
    ("smoke_B (step2000, λ=1, 尖峰态 +0.3642)", "/content/smoke_B/ckpt.pt"),
]

for label, ck in CKPTS:
    print("\n########## %s ##########" % label, flush=True)
    r = subprocess.run(
        [sys.executable, "-u", "/content/stage1_code/stage1_train.py",
         "--platform", "colab", "--wav-root", "/content/wav",
         "--data-root", "/content/stage1_data",
         "--batch", "10", "--resume", ck, "--diag-compensation", "4",
         "--out", "/content/_diag_out"],
        capture_output=True, text=True, timeout=1800)
    tail = [ln for ln in (r.stdout or "").splitlines()
            if any(k in ln for k in ("条件补偿", "恒等", "量化误差", "码幅度", "条件贡献",
                                     "对照:", "resumed", "Traceback", "Error", "错误"))]
    print("\n".join(tail) if tail else (r.stdout or "")[-1200:], flush=True)
    if r.returncode != 0:
        print("[stderr 尾]", (r.stderr or "")[-600:], flush=True)