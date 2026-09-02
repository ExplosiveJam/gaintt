#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 CLIENT_A.webm CLIENT_B.webm OUTPUT_PREFIX" >&2
  exit 2
fi

command -v ffmpeg >/dev/null || {
  echo "ffmpeg is required" >&2
  exit 1
}

client_a=$1
client_b=$2
output_prefix=$3
start_seconds=${GAINTT_DEMO_START:-0}
duration_args=()
if [[ -n ${GAINTT_DEMO_DURATION:-} ]]; then
  duration_args=(-t "$GAINTT_DEMO_DURATION")
fi

ffmpeg -y \
  -ss "$start_seconds" -i "$client_a" \
  -ss "$start_seconds" -i "$client_b" \
  -filter_complex \
    '[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]hstack=inputs=2,scale=1920:-2:flags=lanczos,fps=25,format=yuv420p[v]' \
  -map '[v]' "${duration_args[@]}" -an -c:v libx264 -preset medium -crf 20 \
  -movflags +faststart "${output_prefix}.mp4"

ffmpeg -y -i "${output_prefix}.mp4" \
  -filter_complex \
    'fps=10,scale=1280:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle' \
  "${output_prefix}.gif"
