#!/usr/bin/env bash
# 生成白噪音与占位闹钟音。闹钟想换自己的音乐:直接替换 media/alarm.mp3。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p media

echo "→ 棕噪音 60 分钟 (opus, ~30MB 内)"
ffmpeg -y -loglevel error -f lavfi -i "anoisesrc=colour=brown:duration=3600:amplitude=0.6" \
  -c:a libopus -b:a 64k media/noise_brown.opus

echo "→ 粉噪音 60 分钟"
ffmpeg -y -loglevel error -f lavfi -i "anoisesrc=colour=pink:duration=3600:amplitude=0.5" \
  -c:a libopus -b:a 64k media/noise_pink.opus

echo "→ 占位闹钟音 60 秒 (beep 型;建议之后换成自己喜欢的 mp3)"
ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=880:beep_factor=4:duration=60" \
  -af "volume=0.7" media/alarm.mp3

ls -lh media/
