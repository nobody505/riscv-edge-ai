# 从零复刻到新 K1 板

本流程以 2026-07-26 的生产板为基线，目标是新板无需依赖旧板即可恢复同一套软件结构。建议先在桌面上逐模块验收，再装入设备外壳。

## 1. 基础条件

- SpaceMIT K1 MUSE Pi Pro；
- Bianbu 2.3.3（Linux 6.6.63 系列）；
- 默认用户和家目录为 `space`、`/home/space`；
- 系统镜像中存在 `cam-test`、K1 摄像头驱动和 `/dev/tcm`；
- 可用网络，用于 apt 安装系统库；仓库已自带生产板的 K1 ORT/Python 二进制备份；
- ML307A、SIM 卡、GPS 天线、USB 麦克风、TW-TTS/CH340、ASRPRO 和传感器按 [HARDWARE.md](HARDWARE.md) 接线。

安装前更新系统可能改变内核、摄像头驱动或 NPU ABI。为了复现生产状态，优先使用 Bianbu 2.3.3 原版镜像，不要先做发行版大升级。

## 2. 准备私密配置

```bash
git clone https://github.com/nobody505/riscv-edge-ai.git
cd riscv-edge-ai
cp config/elder.env.example elder.env
chmod 600 elder.env
nano elder.env
```

必须填写：

- OneNET 产品 ID、设备名、设备级 Key、产品级 Key；
- 腾讯地图 WebService Key；
- 接收摔倒短信和允许来信播报的手机号；
- 联系人播报名称。

设备级 Key 用于板端 MQTT，产品级 Key 用于 WiFi 定位查询，两者不要混用。不要把填写后的 `elder.env` 提交到公共仓库。

## 3. 安装板端软件

```bash
sudo bash scripts/install-board.sh --config "$PWD/elder.env"
```

安装器执行：

1. 校验板型、配置模板和 vendor SHA-256；
2. 安装 OpenCV、PortAudio、串口、MQTT、I2C 等系统依赖；
3. 恢复 Python 3.12 RISC-V 运行时和 SpaceMIT ORT；
4. 从六个分片重组生产 SenseVoice `model_quant.onnx`；
5. 安装语音、导航、LBS、短信、摔倒和网络守护源码；
6. 编译 YOLO 消费者、SHM hook、雷达、灯带和独立超声波诊断程序；
7. 安装 systemd、udev 和受限硬件权限代理；
8. mask `serial-getty@ttyS0`，避免占用 ASRPRO UART；
9. 启用并启动生产服务；
10. 执行不发短信、不触发告警的只读验收。

仅安装、不启动服务：

```bash
sudo bash scripts/install-board.sh --config "$PWD/elder.env" --no-start
```

已提前安装系统包时可加 `--skip-packages`。

## 4. 烧录 ASRPRO

权威文件：

```text
asrpro/ASRPRO-小空小空-完整固定命令版-r2.hd
asrpro/asr.cpp
asrpro/fw.bin
```

在 Windows 天问 Block 中可直接烧录 `fw.bin`，也可打开 `.hd` 工程重新生成模型并编译。最终词条和 UART 文本见 `asrpro/ASRPRO-完整固定命令版-r2-词条与串口协议.md`。

烧录时 ASRPRO Type-C 枚举为 CH340。实际流程是点击下载后按板上“手动下载”键约 2 秒再松开，等待工具完成。接入 K1 后，生产事件走 UART0，不走 Type-C。

## 5. 只读验收

```bash
sudo bash scripts/verify-board.sh
systemctl status elder-care.target lbs-service k1-network-health --no-pager
journalctl -b -u elder-voice -u elder-fall -u elder-incoming-sms -u lbs-service --no-pager
```

预期：

- 八个单元 active，`systemctl --failed` 为空；
- `serial-getty@ttyS0` 为 masked；
- `radar_led`、语音、摔倒和来信各只有一个进程；
- SenseVoice 与 vendor 哈希通过；
- ONNX Runtime 能列出可用 provider；
- ML307A `if02` 稳定 by-id 路径存在；
- TTS CH340 by-id 路径存在；
- `/dev/i2c-3`、`/dev/i2c-4`、`/dev/tcm` 存在。

## 6. 分功能现场验收

按风险从低到高进行：

1. 说“小空小空”，确认只播一次“我在”；
2. 测试打开/关闭灯带，不测试摔倒；
3. 打开行路模式，确认左右两路摄像头均能持续产帧并完成车辆检测；
4. 室外确认 GPS fix，再测试短距离导航；
5. 从白名单手机发送一条普通短短信，确认只播一次；
6. 最后在用户明确允许真实告警短信时，测试“摔倒触发—第一短信—保持警报—关闭警报—解除短信”。

不要为了验收降低车辆阈值、摔倒阈值或绕过 `/run/elder-assistant/ml307a_at.lock`。

## 7. 手机 App

`app/` 是当前 HBuilderX/uni-app 源码。先复制 `app/config.example.js` 为 Git 已忽略的 `app/config.js`，再填写本地参数；同时在 `app/manifest.json` 中配置自己的平台标识和腾讯地图 Key，然后由 HBuilderX 运行或打包。仓库不包含 `unpackage`、node_modules 或 APK 生成物。

当前 App 固定显示配置中的设备坐标和“WiFi定位”标签；在线状态仍来自 OneNET。板端导航则使用实时 GPS/WiFi 定位，两者是有意分离的所有权。

## 8. 备份与回退

安装到已有板之前，应先备份 `/home/space` 中同名生产文件和 `/etc/systemd/system/elder-*`。本安装器面向新板，不负责跨未知历史版本自动回退。

如果只需停止整套新服务：

```bash
sudo systemctl stop elder-care.target elder-incoming-sms.service
sudo systemctl stop lbs-service.service k1-network-health.service
```

不要使用 `git reset --hard`、删除整个 `/home/space` 或强杀 `cam-test`。摄像头必须先走 SIGINT 的受控退出，防止 ISP 卡死。
