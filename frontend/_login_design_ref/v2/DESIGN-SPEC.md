# Login page mobile design spec — v2

源：用户在 2026-05-05 12:xx UTC 发的 3 张设计稿截图（zh / en / ja, mobile portrait, ~960×1920 retina）。截图本身没存进 repo（用户没传 PNG，让我从视觉读 spec）。

**设计稿 vs 当前 prod 的关键差异 → 这次重做的 scope**：

| 元素 | 当前 prod (PR-A 后) | 设计稿目标 |
|---|---|---|
| Hero 标题 mobile 字号 | `clamp(1.5rem,5vw,2rem)` (~32px) — PR-A 缩了 | **回大** ~`clamp(3rem,9vw,4.5rem)` (~52-72px serif bold)，2 行 |
| Hero 装饰金线 mobile | `w-12` | 保留 ~16-24px width |
| Calligraphy 呈现 | absolute 漂浮在右上 | **白色卡片**（rounded `~24px` + subtle shadow + bg `#fffefb` 微暖白） inline 在 hero 右侧；mobile 上 hero 跟 calligraphy 是 **2 列并排** (hero 占 `~60%`，calligraphy 占 `~36%`) |
| Calligraphy seal | rectangular 红章在卡片右上角，bg `#b3322d`，white 字 | 跟当前一样，但**位置在卡顶部右上对齐 + 略压在卡片边界上** |
| Calligraphy 底部 | 无 | 卡底加一个**金色小 sprig 装饰**（跟 hero 上的 LeafSprig 同色 `#b59b66`，水平居中）|
| Header lang switcher | 3 个并排小按钮 EN/中/日 | **pill dropdown**："中文 ▾" / "EN ▾" / "日本語 ▾"，绿色细边 (`border-[var(--color-primary)]`)，圆角 pill，点击展开 menu |
| Header hamburger | 现状（3 条横线绿色） | 同 ✓ |
| Mountain backdrop | mobile `opacity-30`（PR-C 改的） | **回到满 opacity，但用更浅的水墨色**（应该已经够浅，可能不需要改 SVG） |
| Sprout icon (登录卡顶) | 11x11 圆形 badge + 内嵌 sprout SVG | 略大些 `~14x14` ≈ 56px，更显 |
| OAuth tiles 颜色 | 真品牌彩色（Google 4 色 / MS 4 色 / 院校绿） | **全部统一深绿** `var(--color-primary)`，monochrome |
| OAuth tiles layout | 3 个并排，只 icon | **不变**（无文字标签）|
| 文案 (calligraphy) | 三语全是 `為學日益 / 為道日損 / 智 / 學`（中文字符）| zh 不变；en/ja 翻译老子 48（见 i18n 段）|
| 飞鸟装饰 | 当前没有 | 设计稿里也**没有**，不加 |

## 1. 视觉描述（精确）

### 1.1 整体 layout (mobile portrait, viewport 360-430px)

```
┌──────────────────────────────────────────────┐
│ [logo+wordmark]    [pill lang ▾] [hamburger] │ ← header 高度 ~64px
├──────────────────────────────────────────────┤
│  ✦ 金线                  ┌─────────────┐    │
│                          │ 學  為  [智 │    │
│  古典智慧               │ 而  道  學]│    │ ← 标题 + 卡片 2 列
│                         │ 不  日       │    │   hero 占 ~58%
│  现代写作               │ 厭  損       │    │   calligraphy 占 ~36%
│  ━━ (gold underline)    │     ━ (sprig)│    │
│                          └─────────────┘    │
│  Appleseed AutoEssay 帮助                    │
│  学者与学生在 AI 协助下思考                 │ ← tagline
│  更深、写作更好、产出更多                    │
│                                              │
│        🌿  ← (mountain backdrop here, faint) │
│                                              │
│              ╭─────────────╮                │
│              │  [sprout icon] │             │
│              │              │              │
│              │  欢迎回来    │              │
│              │  Sign in...  │              │
│              │ ┌──────────┐ │              │
│              │ │ 用户名    │ │              │
│              │ ├──────────┤ │              │
│              │ │ 密码      │ │              │
│              │ │  ☐ 记住我 │ │              │
│              │ │ [登录]    │ │              │
│              │ │ ─ or ─    │ │              │
│              │ │ [G][▣][🏛] │ │              │
│              │ ╰──────────╯ │              │
│              ╰─────────────╯                │
└──────────────────────────────────────────────┘
```

### 1.2 Calligraphy 卡片细节

- 容器：`<div role="presentation" class="calligraphy-card">`
- 大小：`width ~36% of hero block grid`，`height ~auto`（约 200-260px tall，看文字行数）
- 背景：`#fffefb`（暖白偏米黄，跟主背景 `#f6f3eb` 区分但和谐）
- border-radius：`24px`
- shadow：`0 8px 32px rgba(46,64,48,0.08)` 较浅 + 大半径
- padding：内 `~28px 22px 32px 22px`
- 内部 layout：vertical writing mode 2 列文字 (右起读)
  - 列 1（右）：行 1 字（4-8 字符竖排）
  - 列 2（左）：行 2 字
  - 列与列间一条很细的金色竖分隔线 `1px #b59b66 opacity 0.4`
  - 行内字符之间：`letter-spacing` 适中，`line-height ~1.6em`
- Seal（红章）：
  - 位置：absolute，卡片右上角，`top: -10px right: -10px` 微微伸出卡片
  - 大小：`~46x46px`
  - 背景：`#b3322d`（深红朱砂）
  - 字：white，serif，~14px，2 字（zh "智學"，en "Wisdom"，ja "知學"）
  - border-radius：`6px`
- 底部金色 sprig：水平居中，距离卡底 ~12px，颜色 `#b59b66`，宽 ~32px

### 1.3 Hero 标题

- `font-family: serif`（已配置）
- mobile 字号：**`clamp(3rem, 9vw, 4.5rem)`** (~48-72px)，回到大字
- `font-weight: 700`
- `line-height: 1.15`（CJK 略宽 `1.25`）
- 颜色：行 1 + 行 2 前半段 `var(--color-text)` (`#1f2a24` 深墨绿)
- 行 2 末段（`写作` / `Writing` / `文章作成`）：`var(--color-primary)` 深绿
- mobile 永久左对齐 ✓
- 标题下加一条**短金线** `~64px wide × 3px thick`，颜色 `#b59b66`，与 tagline 间距 `~16px`

### 1.4 Tagline

- `font-family: sans`（默认）
- mobile 字号：`text-[0.95rem]` (~15px) 或 `1rem` (~16px)
- `line-height: 1.55`
- 颜色：`var(--color-text)`
- max-width: `~30ch`（设计稿里看 3 行）
- mobile 左对齐

### 1.5 Header lang switcher（pill dropdown）

- 不再是 3 个并排按钮
- 一颗 button：`<button>当前语言 ▾</button>`
  - 当前显示：zh 显示 `中文`，en 显示 `EN`，ja 显示 `日本語`
  - 圆形 pill：`border: 1.5px solid var(--color-primary)`、`border-radius: 9999px`
  - padding: `~6px 14px`
  - font: `text-[0.9rem] font-medium` 绿色
  - chevron `▾` 在文字右侧
- 点击展开 menu（dropdown）：3 个选项 EN / 中文 / 日本語
- menu 样式：白底圆角 + shadow，宽度跟 button 对齐或略宽
- click 外部关闭

### 1.6 Login card

- 跟现状基本一致，2 处微调：
  - **Sprout icon badge 略大**：从 `h-11 w-11` → `h-14 w-14` (~56px)
  - **OAuth icons 全部 monochrome 深绿**：删 GoogleIcon 的 4 色，删 MicrosoftIcon 的 4 色，全部用 `var(--color-primary)` 单色 stroke/fill

### 1.7 Mountain backdrop

- 撤回 PR-C 的 `opacity-30`
- 设计稿里 mountain 颜色已经够浅（设计稿用浅墨绿水彩），不需要再降 opacity
- 如果原 SVG 颜色太重，考虑改成 `opacity 0.5` (a middle ground)

## 2. i18n 改动（基于"老子 48 章不改 zh，翻译 en/ja"决策）

```ts
"login.decor.calligraphy_line_1": {
  en: "Learning grows day by day",  // 為學日益
  zh: "為學日益",                     // unchanged
  ja: "学を為すは日に益し",            // 為學日益
},
"login.decor.calligraphy_line_2": {
  en: "The Way pares day by day",   // 為道日損
  zh: "為道日損",                     // unchanged
  ja: "道を為すは日に損ず",            // 為道日損
},
"login.decor.seal_top": { en: "Wis", zh: "智", ja: "知" },
"login.decor.seal_bottom": { en: "dom", zh: "學", ja: "学" },
```

注：seal 是 2 字纵向方块，所以 en 拆 "Wis/dom" 看起来怪 — 设计稿截图里 en 模式 seal 写 "Wisdom" 一个字纵向但可能转 90°。

更合理：seal 字段保持 1 字符 + 旋转：
```ts
"login.decor.seal_top":    { en: "W", zh: "智", ja: "知" },
"login.decor.seal_bottom": { en: "isdom", zh: "學", ja: "学" },  // 全角字符撑满
```
或者干脆 seal 设计成 3-language sprite：
- zh: 上 "智" 下 "學"
- en: 整个 seal 旋转 90° 写 "Wisdom"
- ja: 上 "知" 下 "学"

实现层：把 seal 拆成 `(top, bottom)` 两 row 还是 `single rotated`，留 implementer 决定，i18n 提供两个 value 即可。

## 3. 实现 checklist

- [ ] Calligraphy 卡片化：从 absolute 浮动改成 inline 网格列
- [ ] Hero + Calligraphy mobile 2-column grid（hero 58% / calligraphy 36%）
- [ ] Hero 标题字号 mobile 回大 `clamp(3rem,9vw,4.5rem)`
- [ ] 标题底加短金线
- [ ] Lang switcher 改 pill dropdown（替换 3 button 并排）
- [ ] OAuth icons 全部 monochrome 深绿（不留品牌色）
- [ ] Sprout badge 放大到 `h-14 w-14`
- [ ] Calligraphy 卡内：竖排 2 列 + 列间金色细线 + 卡底金色 sprig + 红印章
- [ ] Mountain backdrop opacity 回 0.5 或满
- [ ] i18n 更新（en/ja calligraphy 翻译 + seal 处理）
- [ ] 所有新 element 加 `data-testid` 给 Playwright spec
- [ ] 新 e2e spec `frontend/e2e/login-design-fidelity.spec.ts` 截 mobile 375×812 viewport（zh/en/ja）+ 桌面 1280×800 各 1 张

## 4. 不在 scope（确认）

- 飞鸟装饰：设计稿没有，不加
- OAuth 文字标签 "Google/Microsoft/机构账号"：上一版我看错了，设计稿里 OAuth 还是只 icon
- desktop layout (lg+)：不在这一轮 scope，保持 PR-A 后的 2-column 布局
- About / Features section：不在这一轮 scope
