# Appleseed AutoEssay

**语言：** [English](README.md) | 中文 | [日本語](README.ja.md)

Appleseed AutoEssay 是一个开源学术稿件生成与审阅工作流。它把研究问题转成可审阅的论文草稿，并保留来源选择、阶段门禁、审计记录和导出文件。

本仓库支持两种生成模式：

- **ARS express：** 较快的单轮稿件路径，适合快速草稿。
- **13-phase deep：** 带 proposal、source、synthesis、draft、review、integrity、export 等阶段的可审阅流程。

本仓库不绑定任何公开托管服务，也没有默认生产账号。

## 快速开始

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install -e "backend[dev]"

( cd frontend && npm ci )
cp .env.example .env
DATABASE_URL=sqlite:///./autoessay.sqlite3 alembic -c backend/alembic.ini upgrade head
```

运行本地检查：

```bash
backend/scripts/ci-local.sh
```

分别启动后端和前端：

```bash
source backend/.venv/bin/activate
uvicorn autoessay.main:app --app-dir backend/src --reload --host 127.0.0.1 --port 8017
```

```bash
cd frontend
npm run dev
```

然后打开 <http://127.0.0.1:3000>。

## 配置

从 [.env.example](.env.example) 开始。示例文件只使用本地地址和占位值。非 stub 工作流需要你自行提供 OpenAI-compatible LLM gateway、Redis、数据库和可选原创性检测服务。

本地开发和 CI 推荐开启外部 LLM/vendor 调用的 stub。生产部署应通过部署平台注入密钥，并由部署方自行创建首个管理员账号，不要把凭据提交到仓库。

如需引导第一个密码用户，请在本地生成 bcrypt hash，并在私有环境中设置 `AUTOESSAY_INITIAL_ADMIN_USERNAME` 与 `AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH`。未设置 hash 时，引导逻辑不会创建账号。

## 文档

- [需求说明](docs/REQUIREMENTS.md)
- [设计说明](docs/DESIGN.md)
- [系统解释](docs/explained/SYSTEM_EXPLAINED.zh.md)
- [方法论参考](references/methodology.md)
- [安全策略](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
