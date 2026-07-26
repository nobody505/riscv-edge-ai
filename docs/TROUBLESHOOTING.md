# 故障排查

## 服务未启动

```bash
systemctl --failed --no-pager
systemctl status elder-care.target elder-voice elder-fall elder-radar --no-pager
journalctl -b -u elder-voice -u elder-fall -u elder-radar --no-pager
```

三个 `elder-*` 服务单独显示 disabled 是正常的，它们由已启用的 `elder-care.target` 拉起。判断自启动要看 target 和 active 状态。

## ASRPRO 无事件

```bash
systemctl is-enabled serial-getty@ttyS0.service
stat -c '%U:%G %a' /dev/ttyS0
journalctl -b -u elder-voice | grep ASRPRO
```

预期 getty 为 masked，设备 `root:dialout 660`。不要在语音服务运行时另开第二个程序读取 `/dev/ttyS0`。

## TW-TTS 播报不全或静音

```bash
ls -l /dev/serial/by-id/*USB_Serial*
journalctl -b -u elder-voice | grep -E 'TTS|CH340|audio'
```

确认 5V 稳定、公共地、CH340 by-id 没变。灯带改外供后可以单独提高 TTS 音量测试，但要观察长句完整性、模块复位和接头温升。不要同时改变 TTS 供电、音量和业务队列逻辑。

## 双摄无帧

```bash
sudo i2cdetect -y 1
journalctl -b -u elder-voice | grep -E 'FRAME|cam-test|P0|P1|watchdog'
pgrep -a cam-test
```

任一路无帧时，先核对对应 sensor、排线、供电、I²C 和共享内存帧计数，再检查 `cam-test` 日志。不要反复 SIGKILL `cam-test`；正常退出用：

```bash
sudo /usr/local/sbin/elder-hwctl camera-stop
```

## OneNET 离线或导航无位置

```bash
journalctl -b -u k1-network-init -u lbs-service -u k1-network-health --no-pager
ip -brief link
ip route
resolvectl status
```

RNDIS 地址、DNS 和默认路由存在不等于公网 TCP 一定可达。网络守护可能让 OneNET 经 WiFi IPv6/IPv4 备用路径运行，这是预期降级，不代表 ML307A 已恢复。

WiFi 扫描 0 AP 或只有一个 AP 时，OneNET 往往无法定位；不要把它误判为导航代码丢失坐标。

## 短信不发送/不播报

```bash
ls -l /dev/serial/by-id/*ML307A*if02*
ls -l /run/elder-assistant/ml307a_at.lock
journalctl -b -u elder-fall -u elder-incoming-sms -u lbs-service --no-pager
```

三个服务必须使用同一把锁。不要停掉 `lbs-service` 来抢串口，也不要按 `/dev/ttyUSB0`～`ttyUSB3` 猜 AT 口。来信只有白名单号码匹配、TTS 报告完成后才删除。

## 摔倒误触发或漏检

当前门限：低加速度 `<0.65g`，120ms 窗口累计 60ms，冲击 `>=1.80g`，jerk `>=40g/s`，ARM 后 0.80s 内冲击，250ms 内释放到 `<=1.30g`，冷却 30s。

先采集真实佩戴数据再讨论调参。短信同步事务期间暂停 ADXL345 采样是已知窗口，不能通过随意放宽阈值“修复”。
