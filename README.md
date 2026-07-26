# RISC-V Edge AI Elder Assistant

面向 **SpaceMIT K1 MUSE Pi Pro** 的端侧智能辅助系统，在单板上集成语音交互、双目视觉、步行导航、跌倒告警、环境感知与蜂窝网络自愈能力。

本仓库提供可部署的应用源码、模型、RISC-V 运行时、ASRPRO 固件、系统服务及自动化安装工具。目标平台为 **Bianbu 2.3.3 / Python 3.12 / RISC-V 64**。

## 核心能力

| 子系统 | 实现 |
|---|---|
| 语音交互 | ASRPRO 常驻唤醒与固定指令；SenseVoice 处理自由语音 |
| 语音播报 | TW-TTS 硬件合成、统一优先级队列、自回声抑制 |
| 视觉感知 | 双路 MIPI 摄像头、YOLOv8 12 类交通模型、SpaceMIT NPU 推理 |
| 导航定位 | GNSS 优先、OneNET WiFi 定位回退、腾讯地图步行路线 |
| 安全告警 | ADXL345 跌倒检测、短信告警、语音解除与联系人通知 |
| 环境反馈 | HC-SR04 距离检测、WS2812B 状态与告警灯效 |
| 网络可靠性 | ML307A RNDIS/DNS 健康检查、分级恢复与 WiFi 自动接续 |
| 移动端 | 基于 uni-app 的设备状态、位置与告警查看界面 |

## 系统架构

```text
ASRPRO ─┐
USB Mic ├─> Voice Service ─> Command Router ─> Navigation / Vision / TTS
Camera ─┘                         │
                                  └─> YOLOv8 + SpaceMIT NPU

ADXL345 ─> Fall Detection ─┐
Incoming SMS ──────────────┼─> ML307A ─> SMS / GNSS / OneNET
WiFi LBS ──────────────────┘

HC-SR04 ─> Radar Service ─> WS2812B
Network Guard ─> RNDIS / DNS Health Recovery
```

短信、GNSS 与 WiFi 扫描通过 `/run/elder-assistant/ml307a_at.lock` 串行访问 ML307A；所有语音输出进入统一 TTS 队列，避免串口竞争和播报覆盖。

## 平台要求

- SpaceMIT K1 MUSE Pi Pro；
- Bianbu 2.3.3；
- 默认用户 `space`，家目录 `/home/space`；
- 系统镜像提供 `cam-test`、摄像头驱动和 `/dev/tcm`；
- 外设按照 [硬件接线文档](docs/HARDWARE.md) 连接。

## 快速部署

```bash
git clone https://github.com/nobody505/riscv-edge-ai.git
cd riscv-edge-ai

cp config/elder.env.example elder.env
chmod 600 elder.env
nano elder.env

sudo bash scripts/install-board.sh --config "$PWD/elder.env"
sudo bash scripts/verify-board.sh
```

安装器会完成运行时校验、SenseVoice 模型重组、本地程序编译、systemd/udev/sudoers 安装及服务启动。验收脚本只读取状态，不发送测试短信、不模拟跌倒，也不重启整板。

ASRPRO 需单独烧录 [asrpro/fw.bin](asrpro/fw.bin)，随后通过 UART0 接入 K1。完整流程见 [新板部署指南](docs/REPRODUCTION.md)。

## 私密配置

`config/elder.env.example` 定义以下运行参数：

| 配置项 | 用途 |
|---|---|
| `ONENET_PRODUCT_ID` | OneNET 产品标识 |
| `ONENET_DEVICE_NAME` | OneNET 设备名称 |
| `ONENET_DEVICE_KEY_B64` | MQTT 设备级密钥 |
| `ONENET_PRODUCT_KEY_B64` | WiFi 定位查询密钥 |
| `TENCENT_MAP_KEY` | 腾讯地图 WebService Key |
| `ELDER_SMS_PHONE` | 告警联系人号码 |
| `ELDER_SMS_CONTACT_NAME` | 联系人显示名称 |

填写后的配置文件不得提交到仓库。安装后凭据保存在 `/etc/elder-assistant/elder.env`，权限为 `0600`。

## 服务组成

```text
elder-care.target
├── elder-radar.service
├── elder-fall.service
├── elder-voice.service
└── elder-incoming-sms.service

lbs-service.service
k1-network-init.service
k1-network-health.service
```

## 仓库结构

| 路径 | 内容 |
|---|---|
| `app/` | uni-app 移动端工程 |
| `asrpro/` | ASRPRO 工程、生成源码、固件与串口协议 |
| `audio/` | 备用提示音资源 |
| `config/` | 设备、凭据模板、udev 与 sudoers 配置 |
| `docs/` | 架构、部署、硬件、安全与故障排查文档 |
| `hardware/` | 跌倒、雷达与灯带相关程序 |
| `models/` | SenseVoice 模型、词表、校验文件与分片 |
| `network/` | LBS、网络初始化与运行期健康守护 |
| `scripts/` | 安装、模型重组及验收工具 |
| `systemd/` | 生产服务单元和统一 target |
| `vendor/` | K1 RISC-V Python/ORT 运行时归档 |
| `voice/` | 语音助手、导航和短信播报服务 |
| `yolo-mipi/` | 双摄采集桥接、YOLO 消费者与 NPU 模型 |

## 文档

- [部署与复刻](docs/REPRODUCTION.md)
- [系统架构](docs/ARCHITECTURE.md)
- [硬件接线](docs/HARDWARE.md)
- [故障排查](docs/TROUBLESHOOTING.md)
- [安全说明](docs/SECURITY.md)
- [版本基线](docs/PRODUCTION_BASELINE.md)

## 安全边界

本项目属于辅助设备原型，不是医疗器械，也未经过道路功能安全认证。跌倒、障碍和导航提示不能替代人工判断或紧急救援服务。

## License

项目代码采用 [Apache License 2.0](LICENSE)。第三方模型、SpaceMIT 运行时及上游组件遵循各自许可证。
