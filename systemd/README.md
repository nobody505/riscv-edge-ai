# systemd Units

`elder-care.target` 是语音、跌倒、雷达和来信服务的统一生命周期入口。LBS、网络初始化与网络健康守护保持独立故障域。

所有单元由 `scripts/install-board.sh` 安装到 `/etc/systemd/system/`。修改依赖关系后应运行：

```bash
systemd-analyze verify systemd/*.service systemd/*.target
```
