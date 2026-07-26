# Network Services

ML307A 蜂窝链路、定位上报与运行期网络恢复组件。

| 文件 | 作用 |
|---|---|
| `k1_bootfix.py` | 启动阶段的 ML307A/RNDIS 初始化 |
| `lbs_service.py` | GNSS、WiFi 定位与 OneNET 上报 |
| `k1-network-health.py` | RNDIS、路由、DNS 和公网连通性守护 |

健康守护不直接操作 AT 串口，也不重启语音、导航或告警服务；详细恢复状态机见 `docs/NETWORK_HEALTH.md`。
