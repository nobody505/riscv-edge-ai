# 可复刻生产基线

## 目标平台

- SpaceMIT K1 MUSE Pi Pro；
- Bianbu 2.3.3；
- Linux 6.6.63 系列；
- Python 3.12 / RISC-V 64；
- 系统镜像提供 `cam-test`、摄像头驱动和 `/dev/tcm`。

安装前升级内核、摄像头栈或 NPU 运行时可能改变 ABI。为了复现当前状态，应先使用目标 Bianbu 镜像完成安装和验收，再单独评估系统升级。

## 固定制品

| 制品 | 仓库路径 | SHA-256 |
|---|---|---|
| SenseVoice 量化模型 | `models/sensevoice/parts/` 重组 | `e48d5da4a3cac65c09de6e926bea9ccc0e8c732a0d0dab84681bac5efba65d4e` |
| YOLO 交通模型 | `yolo-mipi/best_6out.xq.onnx` | `6c4528ad04cc4d8d2644241ba605d89ebcc4ead7fb7263d4411737ccfb2adbc9` |
| ASRPRO 固件 | `asrpro/fw.bin` | `165ac7b2337374a515bf7a4e60e6db9080514c6e416e7c0985532e0da977a30d` |
| K1 Python ORT 包 | `vendor/k1-python-ort-20260726.tar.gz` | `40053b037327a01921bddfad615ec5c24de35847cd66356cd969512b4b210001` |
| K1 Python 原生依赖 | `vendor/k1-python-runtime-deps-20260726.tar.gz` | `33cfcd3086256dcf00fc76b15a31b9fb39ab2c246e81f9ffd5faf8648ace15f0` |
| K1 Python 纯 Python 依赖 | `vendor/k1-python-pure-deps-20260726-r2.tar.gz` | `a934e2ca9a1a823cb59f7765e6448ff939cfc2b36b535843c7cfba5087865c57` |
| SpaceMIT ORT | `vendor/spacemit-ort.riscv64.2.0.1.tar.gz` | `8a15035aca34d5fd95f24444d4c7843265c1a81f49d84ec6fe9c6d0fdf5b55cf` |

权威校验文件为 `vendor/SHA256SUMS`、`models/sensevoice/SHA256SUMS.parts`、`asrpro/SHA256SUMS` 和 `yolo-mipi/SHA256SUMS`。安装器在解压前同时验证哈希与 tar 成员路径。

## 部署布局

| 权限域 | 部署位置 | 内容 |
|---|---|---|
| root-only 配置 | `/etc/elder-assistant` | 私密环境文件、摄像头配置 |
| root-owned 可执行文件 | `/usr/local/libexec/elder-assistant` | 摔倒、雷达、灯带、摄像头启动器、网络初始化 |
| root-owned 动态库 | `/usr/local/lib/elder-assistant` | 摄像头 SHM hook |
| 非特权应用 | `/home/space` | 语音、导航、LBS、来信、YOLO 消费者、模型与虚拟环境 |
| 共享运行状态 | `/run/elder-assistant` | AT 锁、定位快照、告警和灯效 IPC |
| 网络守护状态 | `/run/k1-network-health` | 运行状态与恢复状态机 |

root 服务不得从 `/home/space` 加载脚本、摄像头 hook 或启动配置。用户进程需要硬件动作时，只能调用 sudoers 精确允许的 `/usr/local/sbin/elder-hwctl` 参数。

## Build 标识

- 语音：`20260724-asrpro-fixed-command-events-r2`；
- 导航：`20260724-dynamic-location-safe-route-r6`；
- LBS：`20260718-indoor-wifi-r2`；
- 摔倒：`20260717-voice-user-sms-fall-window-r7`；
- 来信：`20260717-incoming-sms-tts-r3`；
- 雷达：`20260716-listening-yellow-r2`；
- 网络守护：`20260726-rndis-dns-health-r2`。

源码的权威版本由 Git commit 标识，不在本文重复容易过期的源码哈希。业务阈值、命令状态机、导航规则、双摄方向、YOLO 类别、TTS 优先级和摔倒判定只应通过经过审查的源码提交修改。

## 私密配置

公开仓库不包含真实 OneNET Key、腾讯地图 Key、手机号、固定坐标或系统密码。新板必须从 `config/elder.env.example` 和 `app/config.example.js` 创建本地配置，并使用新生成、最小权限的凭据。详细要求见 `docs/SECURITY.md`。
