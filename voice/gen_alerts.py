#!/usr/bin/env python3
"""生成告警 WAV 文件：alert_person.wav 和 alert_car.wav"""

import sys
import os

# 找到 melotts_onnx 模块
TTS_DEMO_DIR = "/home/space/_tts_demo"
MELOTTS_DIR = os.path.join(TTS_DEMO_DIR, "examples", "NLP", "spacemit_tts")
sys.path.insert(0, MELOTTS_DIR)

# melotts_onnx.py 里 import spacemit_ort 需要注释掉
# 如果之前没改过，运行时发现报错就手动改
from melotts.melotts_onnx import TTSModel

import shutil
import soundfile

ALERTS = [
    ("后方有人靠近", "alert_person.wav"),
]

def main():
    print("Loading TTS models...")
    tts = TTSModel(
        enc_model="encoder-zh.onnx",
        dec_model="decoder-zh.dynq.onnx"
    )

    for text, filename in ALERTS:
        print(f"Generating: {text} -> {filename}")
        wav_path = tts.ort_predict(text)
        dest = os.path.join("/home/space/audio", filename)
        shutil.copy(wav_path, dest)
        print(f"  Saved: {dest}")

        # 验证文件
        info = soundfile.info(dest)
        print(f"  Duration: {info.duration:.2f}s, SampleRate: {info.samplerate}, Channels: {info.channels}")

    print("\nDone! Generated:")
    for _, fn in ALERTS:
        p = os.path.join("/home/space/audio", fn)
        size_kb = os.path.getsize(p) / 1024
        print(f"  {p} ({size_kb:.1f} KB)")

if __name__ == "__main__":
    main()
