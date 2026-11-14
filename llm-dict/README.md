# 大模型字典

离线的知识点字典，内容来自《从零到大模型研发》白话版教材。忘了哪个知识点时用来查：每条分「义」要点、「例」案例、「误」容易出错三块。

- `app/src/main/assets/www/`：字典本体（网页，数据在 `data/` 里）
- `app/src/main/java/`：安卓外壳，只有一个全屏 WebView
- 推送后 GitHub Actions 会自动打包，APK 在仓库 Releases 里的“大模型字典（最新版）”

`debug.keystore` 是固定的调试签名（密码就是安卓默认的 `android`），不是保密的密钥；用它是为了每次打出来的包签名一致，可以直接覆盖安装。
