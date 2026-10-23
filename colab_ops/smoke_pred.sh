#!/usr/bin/env bash
# 条件预测损失(λ_pred)双臂冒烟 —— 验证"让 P 承担内容"能否把条件贡献从 0 抬起来。
#
# 设计要点:
#   * 训练/评测严格分离:stage1_train.py 内置 TRAIN_ROWS / VAL_ROWS
#     (md5(clip key)%1000<20 的哈希切分),battery 只在 val 上跑 —— 不重蹈
#     smoke_anchor 用 64 条"同批训练+评测"的记忆假阳性。
#   * A 臂 λ_pred=0 = 对照(与修复前逐位一致);B 臂 λ_pred=1。
#   * ckpt-every 设大,冒烟不写大 ckpt(只在收尾存一次)。
#
# 用法(VM):bash smoke_pred.sh          # 前台;建议 setsid nohup 后台跑,约 1.7h
set -e
CODE=/content/stage1_code
DATA=/content/stage1_data
WAV=/content/wav
COMMON="--platform colab --wav-root $WAV --data-root $DATA --dtype bf16 --batch 10 \
--steps 2000 --eval-every 500 --probe-every 500 --ckpt-every 100000"

for pair in "A:0" "B:1"; do
  name="${pair%%:*}"; lam="${pair##*:}"
  out="/content/smoke_${name}"
  mkdir -p "$out"
  echo "=== ARM $name | lambda_pred=$lam | 开始 $(date +%H:%M:%S) ==="
  python -u "$CODE/stage1_train.py" $COMMON --lambda-pred "$lam" --out "$out" 2>&1 \
    | tee "$out/train.log"
  echo "=== ARM $name 结束 $(date +%H:%M:%S) ==="
done

echo "================ 双臂汇总 ================"
for name in A B; do
  lam="0"; [ "$name" = "B" ] && lam="1"
  echo ""
  echo "--- ARM $name (lambda_pred=$lam) ---"
  grep -E 'battery @|条件贡献|探针 |GT:|ZERO:' "/content/smoke_${name}/train.log" | tail -30
  echo "  [末步]"
  grep -E 'step +[0-9]+:' "/content/smoke_${name}/train.log" | tail -3
done
echo "SMOKE_ALL_DONE"