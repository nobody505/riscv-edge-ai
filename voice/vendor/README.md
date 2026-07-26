# SpaceMIT NLP demo subset

`spacemit_audio/` and `spacemit_asr/` were recovered from the production board's SpaceMIT NLP demo at `/home/space/_tts_demo/examples/NLP`.

Production-specific changes are intentionally small:

- `ASRModel` is process-resident after first construction;
- ONNX sessions are configured for conservative single-threaded ASR use by the caller;
- the local model existence check uses `model_quant.onnx`, which is the file actually loaded when `quantize=True`;
- missing models fail closed instead of downloading and extracting an unverified runtime archive.

Models are restored only from the repository's checksummed files through `scripts/assemble-sensevoice.sh`.

These files and the SenseVoice model remain subject to their upstream licenses and notices.
