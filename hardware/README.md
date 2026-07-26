# Hardware Services

生产设备的传感器与灯光控制程序。

| 文件 | 作用 |
|---|---|
| `fall_detect_adxl345.py` | ADXL345 跌倒检测与告警流程 |
| `radar_led.c` | HC-SR04 距离检测和 WS2812B 状态仲裁 |
| `hc_sr04.c` | HC-SR04 独立诊断程序 |
| `ws2812.c` | WS2812B 独立控制工具 |

正式服务入口由 `systemd/` 管理。独立诊断工具不得与生产服务同时占用相同 GPIO、I²C 或灯带资源。
