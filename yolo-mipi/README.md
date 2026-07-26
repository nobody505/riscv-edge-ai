# Dual-Camera YOLO Pipeline

面向 SpaceMIT K1 的双路 MIPI 采集与 NPU 推理实现。

`hook_simple.c` 通过 `LD_PRELOAD` 将 `cam-test` 帧写入共享内存；`consumer_final_new_gray.cpp` 读取两路帧、完成灰度预处理、YOLOv8 推理、后处理和方向告警。

```bash
make clean all verify
./start_dual.sh
./run_dual.sh
```

构建依赖 OpenCV、SpaceMIT ONNX Runtime 和 K1 摄像头 SDK。模型入口为 `best_6out.xq.onnx`。
