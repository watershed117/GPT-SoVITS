#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查可用 ckpt：列表 + 读 step/λ_pred（判断能否从上一轮 B 臂状态续跑）。"""
import glob
import os
import subprocess

print(subprocess.run(["bash", "-lc", "ls -l /content/smoke_B /content/long_B 2>/dev/null"],
                     capture_output=True, text=True).stdout)

for p in sorted(glob.glob("/content/smoke_B/*.pt") + glob.glob("/content/long_B/*.pt")):
    try:
        import torch
        ck = torch.load(p, map_location="cpu", weights_only=False)
        a = ck.get("args", {})
        print("CKPT %s | step=%s | lambda_pred=%s | keys=%d"
              % (p, ck.get("step"), a.get("lambda_pred"), len(ck.get("model", {}))))
    except Exception as e:  # noqa: BLE001
        print("CKPT %s | 读取失败 %r" % (p, e))