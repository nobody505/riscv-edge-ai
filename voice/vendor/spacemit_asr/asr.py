import wave
import os
import time
import warnings
import numpy as np
import threading

from .models.sensevoice_bin import SenseVoiceSmall
from .models.postprocess_utils import rich_transcription_postprocess

cache_dir = os.path.expanduser("~/.cache")
asr_model_dir = os.path.join(cache_dir, "sensevoice")
# SenseVoiceSmall(quantize=True) actually opens model_quant.onnx. Use the same
# file for the installation check so a restored board does not redownload an
# unused optimized duplicate on every first start.
asr_model_path = os.path.join(asr_model_dir, "model_quant.onnx")

_resident_model = None
_resident_model_lock = threading.Lock()

class ASRModel:
    def __new__(cls, *args, **kwargs):
        global _resident_model
        if _resident_model is None:
            with _resident_model_lock:
                if _resident_model is None:
                    _resident_model = super().__new__(cls)
        return _resident_model

    def __init__(self):
        if getattr(self, "_resident_initialized", False):
            return
        if not os.path.exists(asr_model_path):
            raise FileNotFoundError(
                "SenseVoice model is missing; restore it with "
                "scripts/assemble-sensevoice.sh and verify SHA-256"
            )

        self._model_path = asr_model_dir
        print(f"初始化ASR模型，路径: {self._model_path}")
        try:
            # 尝试更保守的配置以提高兼容性
            self._model = SenseVoiceSmall(
                self._model_path, 
                batch_size=1, 
                quantize=True,
                intra_op_num_threads=1,
            )
            print("ASR模型初始化成功")
        except Exception as e:
            print(f"ASR模型初始化失败: {e}")
            raise
        self._resident_initialized = True

    def generate(self, audio_file, sr=16000):
        if isinstance(audio_file, np.ndarray):
            # 将int16音频数据归一化为float32（-1到1范围）
            if audio_file.dtype == np.int16:
                audio_path = audio_file.astype(np.float32) / 32768.0
            else:
                audio_path = audio_file
            audio_dur = len(audio_file) / sr
        elif isinstance(audio_file, str):
            audio_path = [audio_file]
            audio_dur = wave.open(audio_file).getnframes() / sr
        else:
            warnings.warn(
                f"[ASR] Unsupported type {type(audio_file).__name__}; "
                "expect str or np.ndarray. Skip this turn."
            )
            return None
            audio_dur = len(audio_file) / 16000

        print(f"开始ASR推理，音频长度: {audio_dur:.3f}s")
        t0 = time.perf_counter()
        try:
            asr_res = self._model(audio_path, language='zh', use_itn=True)
            print(f"推理完成，结果类型: {type(asr_res)}")
            if hasattr(asr_res, '__len__'):
                print(f"结果长度: {len(asr_res)}")
            if len(asr_res) > 0 and hasattr(asr_res[0], '__len__'):
                print(f"第一层长度: {len(asr_res[0])}")
                if len(asr_res[0]) > 0:
                    print(f"实际内容: {asr_res[0][0]}")
        except Exception as e:
            print(f"[ASR] 推理错误详情: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return None
        infer_time = time.perf_counter() - t0
        rtf = infer_time / audio_dur if audio_dur > 0 else float("inf")
        print(f"infer_time: {infer_time:.3f}s, audio_dur: {audio_dur:.3f}s, RTF: {rtf:.2f}")
        # 后处理
        # Handle string output (SimpleTokenizer returns text directly)
        if isinstance(asr_res, list) and len(asr_res) > 0:
            text = asr_res[0]  # Already decoded text string
        else:
            text = ""
        return text
