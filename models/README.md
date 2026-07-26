# Models

端侧推理模型及其完整性校验文件。

SenseVoice 主模型因 GitHub 单文件限制拆分保存在 `sensevoice/parts/`。部署时由 `scripts/assemble-sensevoice.sh` 按顺序重组，并使用 `SHA256SUMS.parts` 和 `SHA256SUMS` 分别校验分片及最终模型。

模型文件不可经过文本工具、自动格式化或换行转换。
