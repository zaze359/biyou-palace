# 碧游宫 V2 · 双端打包与 CI（macOS/Windows/Android）

## Goal

为桌面客户端与移动端建立可复现的构建、打包与 CI 流程，产出未签名安装包；V2 不做签名、公证与自动更新。

## Scope

- 桌面端：Electron + dsh + 本地运行时；macOS arm64/x64、Windows x64。
- 移动端：Android APK；iOS 构建脚本预留。
- 运行时：Node 版本固定与 SHA256 校验；Python/模型运行时按需打包。
- CI：macOS/Windows/Ubuntu 矩阵，产物上传与基本冒烟。

## Requirements

1. 桌面端构建必须区分开发模式（仓库源码）与打包模式（resources 内运行时）。
2. Node 压缩包必须校验 `SHASUMS256.txt`；不得把密钥写入产物。
3. macOS 产出 `.dmg`/`.zip`；Windows 产出 NSIS `.exe`；均未签名。
4. Android 产出 APK；iOS 不要求 V2 产物。
5. CI 产物包含版本、提交号、构建时间与校验和。
6. 构建失败必须保留日志；不得静默产出半成品。
7. 打包后的客户端可启动并完成健康检查；安装包体积与依赖清单记录在案。

## Acceptance Criteria

- [ ] macOS 本地可产出未签名 `.app`/`.dmg`。
- [ ] Windows CI 可产出未签名 `.exe`。
- [ ] Android 可产出 APK。
- [ ] 产物无密钥、无仓库路径硬编码。
- [ ] CI 矩阵可重复执行，产物校验和一致。
- [ ] 打包客户端启动后本地任务闭环冒烟通过。

## Dependencies

- `11-21-desktop-client`
- `11-21-mobile-client`

## Out of Scope

- 代码签名、公证、MSIX、自动更新、应用商店发布。
