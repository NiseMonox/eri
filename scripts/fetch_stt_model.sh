#!/usr/bin/env bash
# 下载语音识别进程 eri-stt 用的模型到 data/models/(data/ 已 gitignore,模型不进仓库)。可以重复执行,已下好的跳过。
# - SenseVoice-Small int8(约 230MB):用 2024-07-17 版,2025-09-09 版是粤语微调、不带标点
# - Silero VAD(约 0.6MB):按停顿切段,每段单独识别,长录音和中日交替才认得对
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
NAME=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
DEST="data/models/${NAME}"
mkdir -p data/models

if [ -f data/models/silero_vad.onnx ]; then
  echo "已存在:data/models/silero_vad.onnx"
else
  echo "→ 下载 silero_vad.onnx"
  curl -fL --retry 3 -o data/models/silero_vad.onnx.part "$BASE/silero_vad.onnx"
  mv data/models/silero_vad.onnx.part data/models/silero_vad.onnx
fi

if [ -f "$DEST/model.int8.onnx" ] && [ -f "$DEST/tokens.txt" ]; then
  echo "已存在:$DEST"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "→ 下载 $NAME"
curl -fL --retry 3 -o "$tmp/model.tar.bz2" "$BASE/${NAME}.tar.bz2"
tar --no-same-owner -xjf "$tmp/model.tar.bz2" -C "$tmp"
src="$(dirname "$(find "$tmp" -name model.int8.onnx | head -1)")"
[ -f "$src/tokens.txt" ] || { echo "压缩包里没找到 model.int8.onnx / tokens.txt" >&2; exit 1; }
rm -f "$src/model.onnx"          # 只用 int8;fp32 版就算有也不要
rm -rf "$DEST"
mv "$src" "$DEST"
echo "→ 完成:$DEST"
ls -la "$DEST"
