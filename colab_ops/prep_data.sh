#!/usr/bin/env bash
# 数据准备:stage1-data(898MB) + wav 数据集(19.4GB 多线程) → /content,与 launch_stage1.sh 同口径。
# 幂等:已存在则跳过。用法(VM):bash prep_data.sh [--force]
set -e
DATA=/content/stage1_data
WAV=/content/wav
export KAGGLE_CONFIG_DIR=/root/.kaggle

if [ "$1" = "--force" ]; then rm -rf "$DATA" "$WAV"; fi
mkdir -p "$DATA" "$WAV"

if [ ! -f "$DATA/audio_codes.bin" ]; then
  echo "[prep] 拉 stage1-data (898MB,多线程)..."
  python3 /content/stage1_code/parallel_kaggle_dl.py \
    watershed117/qwen3tts-stage1-data /tmp/d1.zip 8
  mkdir -p /tmp/d1 && unzip -q -o /tmp/d1.zip -d /tmp/d1
  mv /tmp/d1/* "$DATA"/
  rm -rf /tmp/d1 /tmp/d1.zip
else
  echo "[prep] stage1_data 已存在,跳过"
fi

if [ -z "$(ls -A "$WAV" 2>/dev/null)" ]; then
  echo "[prep] 拉 wav 数据集 (19.4GB,多线程)..."
  python3 /content/stage1_code/parallel_kaggle_dl.py \
    watershed117/qwen3tts-msr-vocoder-wav /tmp/d2.zip 8
  echo "[prep] 流式解包 zip→tar→$WAV (约 3 分钟静默属正常)..."
  unzip -p /tmp/d2.zip wav.tar.bin | tar -xf - -C "$WAV"
  rm -f /tmp/d2.zip
else
  echo "[prep] wav 已存在,跳过"
fi

echo "[prep] 完成: stage1_data $(ls -1 "$DATA" | wc -l) 项 | wav $(find "$WAV" -type f | wc -l) 文件"
echo "PREP_DONE"