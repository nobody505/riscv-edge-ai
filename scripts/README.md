# Deployment Tools

| 脚本 | 作用 |
|---|---|
| `install-board.sh` | 安装依赖、应用、模型与系统服务 |
| `verify-board.sh` | 执行只读部署验收 |
| `assemble-sensevoice.sh` | 重组并校验 SenseVoice 主模型 |
| `test-runtime-imports.py` | 验证 K1 Python 原生依赖 |
| `elder-hwctl` | 受 sudoers 限制的硬件操作代理 |

安装脚本面向同型号新板。对已有部署执行前，应先备份同名文件和 systemd 单元。
