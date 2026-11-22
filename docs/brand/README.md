# 碧游宫 · 品牌标志

## 已接入项目（2026-11-22）

写实主视觉 `logo-master.png` 已定为正式 Logo 并接入桌面客户端：

- 图标事实源：`desktop/assets/brand/icon-photo.png`（本文件副本）；
- 应用图标：`desktop/assets/icon.png`（`npm run icon` 从写实源重生成）；
- 顶栏品牌位：`desktop/ui/shell/index.html` 显示 `assets/icon.png`（28px）；
- 关于面板：`desktop/electron/main.js` 的 `setAboutPanelOptions` 载入图标；
- ~~dsh 侧栏 24px 单色位保留几何副版（`desktop/assets/brand/mark.svg`）~~ → **2026-11-22 两次改决**：
  碧游宫插件不占用 dsh 侧栏品牌行；profile 显式启用 `ui-brand-official`，
  保留 deepseek 官方发行版字标（不是「DSH 本地构建」fallback）。`mark.svg` 暂留作素材，当前未接入。

详见 `desktop/README.md`「品牌 Logo 接入」节。

## 交付结构

```
docs/brand/
├── logo-master.png        ← 主视觉：水墨写意「青萍剑」，剑为主体、青莲为背景注脚
└── abstract/              ← 副版：极简几何标志（小尺寸专用）
    ├── mark.json          图元配置（可复现源）
    ├── spec/
    │   ├── mark.svg / mark-mono.svg / mark-reverse.svg
    │   ├── safe-zone.svg / scale-test.png / application-preview.png
    │   └── spec.html / spec.md
    ├── brand-brief.md     品牌简报
    └── motif-derivation.md 母题推导链条
```

## 主视觉（水墨写意版）

- 文件：`logo-master.png`，1024×1024。
- 内容：**一柄通天教主的青萍剑为绝对主体**——墨色剑身纵贯画面约 80% 高度，剑尖收束为清晰锐角、不圆钝也不细长突兀，指向莲心；淡墨烟雾呈 S 形缭绕；画面底部仅一朵极小的写意青莲与一两笔荷叶水波，作为背景注脚点出「青萍」意象，不喧宾夺主。浅象牙宣纸底。
- 基准：以用户指定的 `blyg-logo-qi-soft-a.png`（Qi 柔和版）为底稿，仅优化剑尖，其余不变。
- 剑上花纹（浓墨细线勾勒，清晰沉实；简笔过度会发虚不辨，已回退）：剑首如意云头纹、剑柄螺旋绳缠纹、剑格对称卷云纹＋中央圆形图案、剑身上部对称云纹雕刻，另保留中央剑脊线与两侧刃线。
- 风格：黑白水墨写意，浓淡干湿、飞白皴擦，空灵留白，与参考视觉同一语言。剑身以浓墨为底、干笔皴擦表现锋芒，**必须浓黑清晰不发虚**（过度简笔会看不清，已回退），**不设笔直白亮高光带**（过于刻意，已弱化）。剑身「青萍」本体不靠题字表达，而靠形制气质传达。
- 无任何文字署名、无水印。
- 用途：桌面端大尺寸展示——启动页、关于页、README 头图、官网首屏、宣传物料。
- **不适用于** 16–24px 图标位：水墨笔触与墨色渐变在该尺寸会糊成一团。

## 极简副版（几何版）

- 文件：`abstract/spec/mark.svg`（及单色／反白版）。
- 内容：同一母题的几何压缩——渐收竖条（剑）落入三瓣青莲（开口弧＋水平基线）。
- 6 个图元，16/24px 单色可辨，`--check-mono` 判定 PASS。
- 用途：应用图标、侧栏品牌位、favicon、名片、丝印等一切小尺寸或单色工艺场景。

## 色彩（两版共用，取自参考图）

| 角色 | HEX | 说明 |
|---|---|---|
| 剑身深墨绿 | `#1F4A47` | 结构色 |
| 主青绿 | `#3E8C87` | 主色 |
| 青莲浅青 | `#7FB5A8` | 点缀色 |
| 象牙底 | `#F3F0E8` | 面板背景 |

## 复现与修改

抽象副版由 `abstract/mark.json` 驱动。修改图元后，在 `abstract/` 目录运行：

```bash
python3 mark_spec_local.py --config mark.json --outdir ./spec --check-mono
```

`mark_spec_local.py` 是专家包脚本的本地副本，仅移除了应用预览中脚本硬编码的品牌名示意文字（用户要求无署名）。不要手改生成的 SVG。

写实主版为位图，如需调整构图或配色需重新生成；生成图必带「AI生成 / WORKBUDDY」水印，须经图像擦除处理后方可交付。
