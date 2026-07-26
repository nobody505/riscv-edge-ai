# ASRPRO Firmware

ASRPRO V2.0 的语音模型工程、生成源码、串口协议和可直接烧录的固件。

| 文件 | 用途 |
|---|---|
| `ASRPRO-小空小空-完整固定命令版-r2.hd` | 天问 Block 工程 |
| `asr.cpp` | 工程生成的识别与事件逻辑 |
| `fw.bin` | 生产固件 |
| `ASRPRO-完整固定命令版-r2-词条与串口协议.md` | 词条和 UART 事件定义 |

模块烧录完成后通过 UART0 向 K1 上报确定性事件；自由语音仍由 K1 上的 SenseVoice 处理。
