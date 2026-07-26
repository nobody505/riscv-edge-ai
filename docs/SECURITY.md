# 安全策略与凭据处理

## 报告漏洞

请通过 GitHub 仓库的 **Security → Advisories → Report a vulnerability** 私密报告漏洞。不要在公开 Issue、日志、截图或提交中粘贴密钥、手机号、精确位置、设备序列号或账户信息。

报告应包含受影响版本、复现条件、影响范围和最小化日志。涉及人身告警、导航或硬件供电的问题，请明确说明是否可能造成错误告警、漏报或设备损坏。

## 凭据边界

仓库只保留占位模板。板端真实配置安装到：

```text
/etc/elder-assistant/elder.env
owner root:root
mode 0600
```

安装器只接受 `config/elder.env.example` 中定义的 7 个字段，拒绝未知、重复、空白或格式异常的变量，避免通过 `LD_PRELOAD`、`PYTHONPATH` 等环境变量影响高权限服务。

本地 App 配置 `app/config.js` 被 Git 忽略，公开模板为 `app/config.example.js`。微信 App ID、DCloud App ID 和地图 Key 必须由使用者自己的平台项目提供。客户端 App 无法安全保存长期产品密钥；正式部署应让受控后端代理 OneNET 查询，并向 App 发放短期、最小权限凭证。

下列数据不得提交：

- OneNET 产品级或设备级 Key；
- 腾讯地图及其他云 API Secret；
- Linux/SSH/WiFi 密码和私钥；
- 联系人姓名、手机号、短信正文；
- 固定家庭位置、精确坐标、局域网拓扑和设备唯一标识；
- 填写后的 `elder.env`、`app/config.js`、`.env`、token 或构建产物。

## 已泄露凭据的处置

从最新 Git 历史删除字符串只能阻止后续直接读取，无法撤销别人已经获得的副本。任何曾进入公开提交的 Key 或密码都必须在对应平台轮换或吊销，并同步更新设备私密配置。旧 clone、fork、缓存和制品也应按已泄露处理。

微信 App ID 是客户端公开标识，不等同于 App Secret；即便如此，本仓库仍使用占位符，避免错误关联到特定个人项目。微信 App Secret 绝不能进入客户端或仓库。

## 最小权限设计

- 语音、定位和来信服务以非 root 用户运行；
- root 服务只执行 `/usr/local/libexec/elder-assistant` 中 root-owned 文件；
- 摄像头 `LD_PRELOAD` hook 和配置由 root 持有，普通用户不可替换；
- `/usr/local/sbin/elder-hwctl` 只接受固定动作，sudoers 不使用通配参数；
- 跨进程文件位于 setgid+sticky 的 `/run/elder-assistant`，目录权限为 `3770`，文件权限不超过 `0660`；AT 锁由 root 预创建且拒绝符号链接；
- `/dev/tcm` 通过 udev 归组 `elder-assistant`，权限为 `0660`；
- ML307A 串口使用稳定 by-id 路径和同一把受控锁；
- systemd 单元使用只读文件系统、私有临时目录和按需的 `NoNewPrivileges`；需要调用受限 sudo 代理的语音服务除外。

不要把 `/dev/mem`、全部 ttyUSB、运行目录或整个用户家目录改成全局可写。任何网络、语音或 App 输入都不得拼接进 shell 命令。

## 供应链校验

`vendor/SHA256SUMS` 与 SenseVoice 校验文件固定大型制品内容。安装前先验证 SHA-256，再检查 tar 成员路径，拒绝绝对路径、`..`、设备节点和越界链接。

板端 MQTT 使用 OneNET 8883/TLS。由于该端点提供的是 CN 不匹配 DNS 名称的自签名证书，仓库只信任 `config/onenet-mqtt-ca.pem`，并在握手后再次核对服务端 DER 证书 SHA-256 指纹，而不是关闭证书校验。证书更新必须同时复核来源、有效期、运行时代码中的指纹和 `config/SHA256SUMS`。

## 发布前检查

1. 确认模板仍只有 `REPLACE_WITH_*` 占位符；
2. 扫描当前树和完整 Git 历史；
3. 复核 GitHub Secret Scanning；
4. 确认没有个人路径、固定坐标、手机号、主机名或私网拓扑；
5. 验证依赖、制品哈希、systemd、sudoers 和 udev 规则；
6. 在新板部署时使用新生成的凭据，不复用已公开值。
