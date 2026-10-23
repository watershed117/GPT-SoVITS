#!/usr/bin/env bash
# 延长 B 臂（λ_pred=1）到 6000 步：验证 @2000 的 +0.3642 是"相变"还是"瞬态"。
# 单臂、无 ARM 标记 → 监控面板按单跑正常显示。
# 每 500 步 battery（12 个观测点，足够看轨迹），每 2000 步存 ckpt（防会话中断）。
# 用法（VM，detached）：setsid nohup bash /content/stage1_code/run_long_B.sh > /content/longB.out 2>&1 &
set -e
CODE=/content/stage1_code
LOG=/content/longB.log

echo "=== LONG-B | lambda_pred=1 | 6000 步 | 开始 $(date +%H:%M:%S) ===" | tee "$LOG"
python -u "$CODE/stage1_train.py" \
  --platform colab --wav-root /content/wav --data-root /content/stage1_data \
  --dtype bf16 --batch 10 --steps 6000 \
  --eval-every 500 --probe-every 500 --ckpt-every 2000 \
  --lambda-pred 1 --out /content/long_B 2>&1 | tee -a "$LOG"
echo "=== LONG-B 结束 $(date +%H:%M:%S) ===" | tee -a "$LOG"
echo "LONG_B_DONE" | tee -a "$LOG"
