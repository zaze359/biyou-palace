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

---

## 桌面端交接（2026-11-22 · S8 已就绪）

`11-21-desktop-client` 的 S8 已交付构建链，本任务桌面端部分可直接开工；移动端仍依赖
`11-21-mobile-client`（待启动）。

### 现成能力

- 构建入口：`cd desktop && npm run build -- --mac|--win|--linux`
  （`desktop/scripts/build.mjs`：版本自检 → 校验 runtime/Node → 调 electron-builder → 写清单）。
- 打包配置：`desktop/electron-builder.yml`。未签名（`identity: null`）；
  当前 mac target 为 `zip`（另产 `.app` 目录），dmg 因受限环境 ditto 权限失败暂关。
- 资源布局：asar 内 = 主进程 + preload + ui/shell + `@noble/hashes`；
  asar 外 = `Resources/runtime`（1.1 GB dsh 依赖）+ `Resources/node-runtime`（锁定 Node 24.22.3）。
- 产物清单：`dist/build-manifest.json`（版本、pinned 版本、commit、构建时间、每个产物 SHA-256）。
  目录产物（`.app`）的 SHA-256 是**真实内容哈希**（`scripts/artifact-hash.mjs`：相对路径排序 +
  逐文件内容流式哈希），内容变化必变、同 commit 重复稳定；`npm run test:build` 6/6 覆盖。
  可直接用于待做 #2 的「同 commit 两次构建校验和一致」校验。
- 前置依赖：`npm install`、`npm run dsh:install`、`npm run node:bootstrap`（缺一即报错，不产半成品）。
- Node 下载校验已落地（2026-11-24）：`bootstrap-node.mjs` 下载/复用 Node tarball 后、解压前，
  必按官方 `SHASUMS256.txt` 流式校验 sha256（`scripts/shasums.mjs` + `scripts/artifact-hash.mjs`）；
  不符则删除 archive 并报错，不解压入库。`npm run test:build` 14/14 覆盖解析/比对/篡改/CRLF。
  满足 Requirement #2（对应待做 #2 的 Node 包校验部分）。

### 本任务待做（桌面端）

1. CI 矩阵：macOS arm64/x64、Windows x64 上跑 `npm run build`，上传产物与 `build-manifest.json`。
2. 校验和一致性：同 commit 两次构建比对 SHA-256；Node 压缩包按 `SHASUMS256.txt` 校验。
3. 有权限的 CI 上按需开启 dmg target（`identity: null` 保持未签名）。
4. 打包后启动冒烟：真实桌面环境双击 `.app` → dsh 就绪 → 碧游宫插件上屏 → 退出无残留。
   本仓库沙箱内 Electron 报 `codesign_util` 失败，未完成此项，需 CI/真机补验。
5. 体积与依赖清单归档（`.app` 约 2.0 GB，主因是 dsh web bundle；S8 打包时裁剪留给后续）。

### 验收口径提醒

本任务原验收「打包客户端启动后本地任务闭环冒烟通过」需按 11-21-desktop-client 的
精简决策理解：V2.0-A 单机首版**不含本地任务板/任务闭环**（S4 后置）。
桌面端冒烟口径 = 可启动 + dsh 就绪 + 品牌/状态插件上屏 + 联阵发现 + 无残留进程。
