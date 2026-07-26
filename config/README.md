# Configuration

板端部署所需的公开配置模板与系统集成文件。

- `elder.env.example`：OneNET、地图和联系人配置模板；
- `onenet-mqtt-ca.pem`：OneNET 8883 自签名 MQTT 服务证书；
- `dual_ov5647.json`：双路摄像头配置；
- `99-asrpro-uart0.rules`：ASRPRO UART 权限规则；
- `99-elder-hardware.rules`：NPU 设备的受控组权限；
- `elder-assistant.tmpfiles`：跨进程运行目录定义；
- `elder-assistant.sudoers`：固定参数的硬件操作授权。

填写后的 `elder.env` 属于私密文件，只应安装到 `/etc/elder-assistant/elder.env`，不得提交到仓库。
