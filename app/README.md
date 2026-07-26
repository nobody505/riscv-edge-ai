# Mobile Application

基于 uni-app 的移动端监控界面，用于查看设备在线状态、位置与告警信息。

## 配置

复制 `config.example.js` 为 Git 已忽略的 `config.js` 并填写本地参数，同时在 `manifest.json` 配置自己的 DCloud、微信和地图标识。私密配置不得提交到公共仓库。

```bash
cp config.example.js config.js
```

正式发布时应由受控后端代理 OneNET 查询，不要把长期产品密钥固化进 APK。

## 开发

推荐使用 HBuilderX 导入本目录进行运行与打包。依赖定义位于 `package.json`，仓库不提交 `node_modules`、`unpackage` 或 APK 构建产物。
