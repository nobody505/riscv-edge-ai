# 硬件与接线

所有改线必须整板断电。K1、外接 5V 电源、灯带、雷达、TTS、ASRPRO 和传感器必须可靠共地。

| 模块 | 当前连接/接口 | 关键限制 |
|---|---|---|
| ASRPRO V2.0 | 5V、GND；模块 UART0 TX → K1 Pin 10 / `ttyS0` RX | 115200 bps；K1 只接收事件；`serial-getty@ttyS0` 必须 masked |
| TW-TTS | 5V；CH340 USB 串口，9600 bps | 使用 `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`；不要按 ttyUSB 序号写死 |
| ADXL345 | I2C-3，地址 `0x53`；SDA/SCL 对应排针 27/28 | 当前摔倒阈值是安全基线，不得随意调整 |
| HC-SR04 | Trig Pin 13/GPIO72；Echo Pin 11/GPIO71 | 5V 供电时 Echo 必须分压到约 3.3V，禁止 5V 直入 GPIO |
| WS2812B | DATA Pin 40/GPIO37；60 颗；外接 5V | 当前亮度约 25%；外部电源须有足够电流，数据地必须共地 |
| 左摄 | P0 → sensor2 → CSI3 → OV5647 | 最后诊断正常，播报“左侧” |
| 右摄 | P1 → sensor0 → CSI1 → OV5647 | 当前工作正常，播报“右侧” |
| ML307A | 板载 USB 4G/GNSS，AT 使用 `if02` by-id | 短信、GPS、WiFi 扫描共享 `/run/elder-assistant/ml307a_at.lock` |
| USB 麦克风 | PortAudio 输入 | TTS 播放期间录音必须丢弃，防止自回声 |

## 灯带供电

60 颗 WS2812B 理论满白电流远高于 K1 小电源脚可安全提供的电流。使用独立、稳压、容量足够的 5V 电源，电源端就近加保险/限流更稳妥。正式安装建议在灯带电源入口加大电解电容，并确认线径、接头和连续运行温升。

K1 只输出 DATA，外部电源负责灯带功率；K1 GND 和灯带电源 GND 必须连接。不要从独立电源向 K1 5V 反灌。

## HC-SR04 电平

HC-SR04 以 5V 供电时，Echo 可能输出 5V。可用 Echo 串 1kΩ、GPIO 端对地下拉 2kΩ 的分压，将高电平降至约 3.3V；接入前用万用表/示波器确认。3.3V 供电可能使部分模块量程缩短或回波不稳定。
