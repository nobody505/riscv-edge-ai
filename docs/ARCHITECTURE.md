# 运行架构与所有权

## 进程关系

```text
ASRPRO UART0 ─┐
USB Mic ──────┼→ elder-voice → SenseVoice/命令状态机 → TTS统一队列
双摄 SHM ─────┘          │                         │
                         └→ nav_engine             └→ TW-TTS CH340

ADXL345 → elder-fall ──共享AT锁──┐
incoming SMS ─────────共享AT锁───┼→ ML307A if02
lbs-service GPS/WiFi ─共享AT锁───┘        │
                                         └→ OneNET MQTT

HC-SR04 → elder-radar → WS2812B
k1-network-health → RNDIS/DNS检测 → 必要时WiFi runtime-only兜底
```

## 语音分层

- IDLE：SenseVoice 模型常驻内存，但不录音、不推理；ASRPRO 监听“小空小空”。
- ASRPRO 固定命令：出行模式、灯带和关闭警报通过确定性 UART 事件执行。
- 自由语音：任意导航地点、短信正文和多轮确认由 K1 SenseVoice 处理。
- 所有可听输出必须进入同一个 TTS 队列；禁止另起进程直接写 TW-TTS 串口。

## 灯带所有权

从高到低：黄色聆听反馈（短时）→ 摔倒红闪 → 行路模式近障蓝闪 → 独立绿灯 → 熄灭。黄色结束后重新计算并恢复原状态，不清除其他标记。

## 导航定位

1. 20 秒内的 GPS 原子快照；
2. 5 分钟内的 OneNET WiFi 定位；
3. 两者都无效则拒绝开始路线。

到达判断只能由新鲜 GPS 且距真实目的地小于 30 米触发。WiFi 位置不能宣布到达。

## 双摄与 YOLO

`cam-test` 经 `LD_PRELOAD=hook_simple.so` 把两路 640×480 NV12 写入 `/dev/shm/pipe0_frame` 和 `pipe1_frame`。消费者只取 Y 平面生成灰度 320×320 输入，调用 xquant 12 类模型。

- 当前画面最高置信度 `<0.75`：最短 1 秒切摄像头；
- `>=0.75`：当前摄像头最多保持 3 秒；
- 车辆告警阈值 `>=0.90`；
- 每 5 帧输出隐藏心跳；启动 15 秒无首帧或运行 3 秒无进度则重试；
- 重试退避 2、4、8、16、30 秒后保持 30 秒，无固定次数上限。

## 网络守护边界

`k1-network-health` 不打开 ML307A AT 串口、不获取共享 AT 锁，也不重启业务服务。它只检查 RNDIS netdev、地址、DNS、路由和 TCP；RNDIS TCP 不通且 WiFi 健康时，增加 runtime-only metric 50 的 WiFi 兜底路由。RNDIS 恢复后清理兜底。
