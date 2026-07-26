#!/bin/sh
set -eu

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
source_dir="$repo/models/sensevoice"
dest=${1:-/home/space/.cache/sensevoice}
owner=${2:-space:space}
tmp="$dest/.model_quant.onnx.tmp.$$"

mkdir -p "$dest"
(cd "$source_dir" && sha256sum -c SHA256SUMS.parts)
trap 'rm -f "$tmp"' EXIT HUP INT TERM
cat "$source_dir"/parts/model_quant.onnx.part-* > "$tmp"
actual=$(sha256sum "$tmp" | awk '{print $1}')
expected=e48d5da4a3cac65c09de6e926bea9ccc0e8c732a0d0dab84681bac5efba65d4e
[ "$actual" = "$expected" ] || {
  echo "SenseVoice model hash mismatch: $actual" >&2
  exit 1
}
mv -f "$tmp" "$dest/model_quant.onnx"
trap - EXIT HUP INT TERM

for name in am.mvn chn_jpn_yue_eng_ko_spectok.bpe.model config.yaml \
  configuration.json sensevoice_decoder_model.onnx silero_vad.onnx \
  tokenizer.vocab tokens.txt README.md SHA256SUMS; do
  install -m 0644 "$source_dir/$name" "$dest/$name"
done
if [ "$owner" != "-" ]; then
  chown -R "$owner" "$dest"
fi
(cd "$dest" && sha256sum -c "$source_dir/SHA256SUMS")
echo "SenseVoice restored at $dest"
