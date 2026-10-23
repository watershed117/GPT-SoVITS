#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成一份「像真的一样」的 stage1 训练日志，用来演示/验证监控页（不影响真实训练）。

写的是**当前 stage1_train.py 的格式**（含 |E|/|P|/|P2| 尺度锚列与 battery 探针），
每 2 秒追加 100 步，模拟尺度锚生效后的健康曲线：
  - lat 从 ~9.6 降到 ~6.2
  - cp/cr 平稳（不出现 v7 那种 10× 膨胀）
  - |P|/|E| ≤ 1.0（天花板生效）
  - battery 条件贡献 ≈ +1.6（条件解码器已启动）

用法：
  nohup python3 demo_log.py /content/stage1_out/train_L4.log > /content/demo_log.out 2>&1 &
"""
import math
import os
import random
import sys
import time

path = sys.argv[1] if len(sys.argv) > 1 else "/content/stage1_out/train_L4.log"
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w"):
    pass

T0 = time.time()
STEP = 0


def log(msg):
    with open(path, "a", encoding="utf-8") as f:
        f.write("[%8.1fs] %s\n" % (time.time() - T0, msg))


log("=" * 70)
log("stage1 训练: bf16 | bs4×48帧 | lr 0.0003 | λ_lat 1 λ_stft 1 λ_wav 0.5")
log("out=/content/stage1_out  (DEMO: 合成日志，非真实训练)")
log("=" * 70)

while True:
    STEP += 100
    k = STEP / 20000.0
    stft = 0.28 * math.exp(-1.1 * k) + 0.14 + random.uniform(-0.008, 0.008)
    wav = 0.11 * math.exp(-1.2 * k) + 0.06 + random.uniform(-0.004, 0.004)
    lat = 9.6 * math.exp(-1.4 * k) + 6.2 + random.uniform(-0.05, 0.05)
    cp = 72 - 12 * k + random.uniform(-2.5, 2.5)
    cr = 64 - 9 * k + random.uniform(-2.5, 2.5)
    e = 4.2 + random.uniform(-0.08, 0.08)
    p = min(e * 0.99, 0.55 + e * 0.42) + random.uniform(-0.04, 0.04)   # 锚生效：≤E
    p2 = min(e * 1.0, 0.85 + e * 0.55) + random.uniform(-0.05, 0.05)   # 锚生效：≤E
    log("step %6d: loss %.4f (stft %.4f wav %.4f lat %.4f cp %.3f cr %.3f) "
        "| %.2f s/步 | mem %.0fMB |E| %.2f |P| %.2f |P2| %.2f"
        % (STEP, stft + wav + lat, stft, wav, lat, cp, cr, 1.10, 1980, e, p, p2))

    if STEP % 2000 == 0:
        log("battery @ step %d ..." % STEP)
        g = 6.35 + random.uniform(-0.04, 0.04)
        log("  GT: lat %.4f cos %.4f rms %.4f" % (g, 0.58, 0.99))
        log("  ZERO: lat %.4f cos %.4f rms %.4f" % (g + 1.62, 0.30, 0.70))
        log("  SHUF: lat %.4f cos %.4f rms %.4f" % (g + 0.95, 0.34, 0.82))
        log("  条件贡献 ZERO-GT = %+.4f" % 1.62)
        for name in ("E", "P", "pro", "res", "prores"):
            log("  探针 %s→Sem: %.4f" % (name, 0.30 + random.uniform(-0.02, 0.02)))

    if STEP % 1000 == 0:
        log("ckpt saved: %s (step %d)" % (os.path.join(os.path.dirname(path), "ckpt.pt"), STEP))

    time.sleep(2)
