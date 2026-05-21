# Appleseed AutoEssay — 前端切图资源包

按设计稿（移动端 + 桌面端）切出来的静态前端，原生 HTML/CSS/JS，无构建依赖，浏览器直接打开 `index.html` 就能看。

---

## 目录结构

```
appleseed-autoessay/
├── index.html               主页面
├── css/
│   ├── variables.css        设计 token（颜色/字体/间距/圆角/阴影）—— 改主题先看这里
│   ├── base.css             reset + 基础排印
│   ├── layout.css           栅格/响应式断点
│   └── components.css       组件样式（按钮/输入框/卡片/导航等）
├── js/
│   └── main.js              汉堡菜单、密码显隐、表单提交占位
├── assets/
│   ├── svg/                 装饰 SVG 占位（可逐张替换为原图）
│   └── images/              （留空）放原图位图用
├── README.md                本文档
└── assets-manifest.md       图片资源替换清单（每张图的尺寸/用途/出现位置）
```

---

## 响应式断点

| 档位 | 宽度 | 行为 |
|------|------|------|
| 移动 | < 768px | 单列，登录卡在 hero 下方，功能卡单列，顶部汉堡菜单 |
| 平板 | 768px – 1023px | 单列居中（最大 720px），功能卡 2 列 |
| 桌面 | ≥ 1024px | Hero 左右双栏（标题 / 登录卡），功能卡 4 列，顶部完整导航 |

断点定义集中在 `css/layout.css`，组件内的微调在 `css/components.css`。

---

## 改主题指南

1. 改颜色：编辑 `css/variables.css` 顶部的 `--color-*` 即可全站生效。
2. 改字体：替换 `index.html` 中的 Google Fonts 链接，并修改 `--font-serif` / `--font-sans`。
3. 改间距/圆角：调 `--space-*` / `--radius-*`。
4. 改装饰图：把 `assets/svg/` 下对应的 SVG 替换成真实图片即可（同名同位置最方便）。

---

## 接入真实业务

- 登录表单：`js/main.js` 里的 `form.submit` 处理是占位 `console.log`。换成 `fetch('/api/login', ...)`。
- 语言切换：`.lang-switch` 点击事件目前也是占位，按需挂菜单组件或路由。
- 路由：所有 `<a href="#">` 都是空锚点，按页面接进去。

---

## 已知限制

- 装饰图（山水、植物、印章）是 SVG 占位，比设计稿原图轻许多。要"水墨味"原图请按 `assets-manifest.md` 替换。
- 没做暗色模式。需要的话用 `prefers-color-scheme` + 一组对应 CSS 变量加。
- 没做无障碍完整性测试，关键 `aria-*` 已经标了，跟最终交互联动后再过一遍。
