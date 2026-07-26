#!/usr/bin/env python3
import kaldi_native_fbank
import numpy
import onnxruntime
import paho.mqtt.client
import pyaudio
import pypinyin
import scipy
import serial
import soundfile
import yaml

print("onnxruntime", onnxruntime.__version__)
print("providers", onnxruntime.get_available_providers())
print("numpy", numpy.__version__)
print("scipy", scipy.__version__)
print("runtime imports: OK")
