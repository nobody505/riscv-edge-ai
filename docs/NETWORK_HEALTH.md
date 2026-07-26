# RNDIS 与 DNS 运行期健康守护

`network/k1-network-health.py` 是独立于业务服务的网络守护进程。它持续验证 ML307A RNDIS 链路，按故障类型执行有冷却时间的分级恢复，并在 RNDIS 不可用时临时接续已经健康的 WiFi。设计目标是恢复网络而不抢占 AT 串口、不重启业务进程、不改写持久网络配置。

## 监测模型

每 20 秒执行一次完整探测。RNDIS 只有同时满足以下条件才判定为健康：

1. 由 `rndis_host` 驱动的网络接口存在；
2. 接口拥有全局 IPv4 或 IPv6 地址；
3. 接口拥有默认路由；
4. 至少一个业务域名可经该接口解析；
5. 至少一个业务 TCP 端点可经 `SO_BINDTODEVICE` 从该接口建立连接。

DNS 探测使用 OneNET 和腾讯地图域名。TCP 探测覆盖 MQTT 与 HTTPS 端点。只要求任一独立端点成功，避免把单个云服务故障误判成本地链路损坏。

运行状态写入：

```text
/run/k1-network-health/status.json
/run/k1-network-health/state.json
```

状态文件由 systemd 的私有运行目录承载，重启后自动重建。

## 状态转换

```text
健康
  └─ 连续失败 3 次
       ├─ 接口不存在 ──> 仅重绑 ML307A 网络类接口
       ├─ TCP 正常但 DNS 异常 ──> 刷新 DNS + NetworkManager reapply
       └─ 接口存在但链路异常 ──> NetworkManager reapply

RNDIS 故障 + WiFi 已验证健康
  └─ 安装 proto 99 / metric 50 的临时默认路由

RNDIS 连续健康 2 次
  └─ 删除守护进程拥有的临时 WiFi 路由
```

所有恢复动作分别有 120 秒冷却，防止持续故障形成高频重绑或 reapply 循环。

## 恢复动作

### DNS 异常

仅在 RNDIS TCP 可达而 DNS 探测失败时依次执行：

```text
resolvectl flush-caches
resolvectl reset-server-features <interface>
nmcli device reapply <interface>
```

该路径不执行 connection down/up，因此不会主动中断正常业务套接字。

### RNDIS 接口存在但不健康

调用 `nmcli device reapply` 重新应用当前 NetworkManager 状态，不删除连接配置，也不修改持久 profile。

### RNDIS 接口消失

守护从 sysfs 验证 USB VID/PID 和接口类别，只解绑/重绑类别为 `e0`、`0a` 的 RNDIS 控制与数据接口。类别为 `ff` 的 ML307A 串口接口不会被触碰。重绑完成后仅对新出现的网络接口执行 managed/connect。

### WiFi 临时接续

WiFi 自身必须通过地址、路由、DNS 和 TCP 四项验证后才可作为备用链路。守护复制其当前网关，安装带 `proto 99` 和 `metric 50` 标记的运行期默认路由；停止服务或 RNDIS 恢复后只删除同一标记的路由。

## 安全与隔离边界

守护明确禁止以下行为：

- 打开 ML307A 串口或发送 AT 命令；
- 获取 `/run/elder-assistant/ml307a_at.lock`；
- 启停或重启语音、导航、定位、短信、摄像头和传感器服务；
- 修改持久 NetworkManager profile；
- 重绑 vendor-specific 串口接口；
- 删除不属于自己的默认路由；
- 在单次远端服务失败时立即修复本地链路。

systemd 单元以 root 运行以允许操作路由和 sysfs，但启用了 `NoNewPrivileges`、`PrivateTmp`、`ProtectHome`、`ProtectSystem` 和地址族限制。执行外部命令时全部使用固定 argv，不经过 shell。

## 部署

安装器将以下文件部署到板端：

```text
network/k1-network-health.py
  -> /usr/local/sbin/k1-network-health.py

systemd/k1-network-health.service
  -> /etc/systemd/system/k1-network-health.service

docs/NETWORK_HEALTH.md
  -> /usr/local/share/doc/k1-network-health/README.md
```

正常部署由 `scripts/install-board.sh` 完成。手工检查命令：

```bash
systemctl status k1-network-health.service --no-pager
journalctl -b -u k1-network-health.service --no-pager
python3 /usr/local/sbin/k1-network-health.py --print-build
python3 /usr/local/sbin/k1-network-health.py --once
sudo cat /run/k1-network-health/status.json
```

`--once` 会执行一次真实探测，并可能在满足连续失败和冷却条件时执行恢复；它不是纯只读命令。

## 验收标准

- 健康 RNDIS 不发生重绑或 connection down/up；
- 单次失败不会触发恢复；
- DNS-only 故障只走 DNS 修复路径；
- 接口缺失时不触碰 ML307A 串口接口；
- WiFi 不健康时不安装备用路由；
- 所有备用路由都带 `proto 99`，并可在停止服务后清理；
- 业务服务 PID、AT 锁和串口所有权不因守护动作改变；
- 状态 JSON 原子更新，异常不会使守护进程退出。

## 故障排查

```bash
ip -brief link
ip -4 route show default
ip -6 route show default
ip -4 route show default proto 99
ip -6 route show default proto 99
resolvectl status
journalctl -b -u k1-network-init -u k1-network-health -u lbs-service --no-pager
```

如果热点被人为关闭，WiFi 备用链路消失属于外部状态变化，不是守护故障；RNDIS 仍会按自身探测结果继续恢复。若要立即移除守护路由，可运行：

```bash
sudo /usr/local/sbin/k1-network-health.py --cleanup
```
