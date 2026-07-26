# Voice and Navigation

K1 端语音交互主链路。

| 文件 | 作用 |
|---|---|
| `voice_assistant_v4.py` | 唤醒事件、自由语音、命令状态机和 TTS 调度 |
| `nav_engine.py` | 定位选择、路线规划、到达判断和导航播报 |
| `incoming_sms_to_tts.py` | 白名单来信接收、去重与排队播报 |
| `vendor/` | SpaceMIT ASR/VAD 适配代码 |

所有可听输出必须经过语音助手的统一队列；其他进程不得直接并发写入 TW-TTS 串口。
