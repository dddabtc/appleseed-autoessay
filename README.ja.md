# Appleseed AutoEssay

**言語:** [English](README.md) | [中文](README.zh.md) | 日本語

Appleseed AutoEssay は、研究課題から査読可能な学術原稿を作るためのオープンソース workflow tool です。source selection、phase gate、audit notes、export files を残しながら原稿を生成します。

この repository は 2 つの生成モードを提供します。

- **ARS express:** 速い single-pass manuscript path。
- **13-phase deep:** proposal、source、synthesis、draft、review、integrity、export を含む reviewable pipeline。

この repository には hosted public service は紐づいておらず、default production account もありません。

## Quick Start

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install -e "backend[dev]"

( cd frontend && npm ci )
cp .env.example .env
DATABASE_URL=sqlite:///./autoessay.sqlite3 alembic -c backend/alembic.ini upgrade head
```

ローカル checks:

```bash
backend/scripts/ci-local.sh
```

Backend と frontend を別 shell で起動します。

```bash
source backend/.venv/bin/activate
uvicorn autoessay.main:app --app-dir backend/src --reload --host 127.0.0.1 --port 8017
```

```bash
cd frontend
npm run dev
```

Then open <http://127.0.0.1:3000>.

## Configuration

[.env.example](.env.example) から始めてください。example file は local addresses と placeholder values だけを使います。non-stub workflow では、OpenAI-compatible LLM gateway、Redis、database、optional originality providers を自分で設定してください。

local development と CI では external LLM/vendor calls の stub flags を使ってください。production deployment では secrets を deployment platform から注入し、初回管理者 account は deployment owner が作成してください。

first password user を bootstrap する場合は、local で bcrypt hash を生成し、private environment に `AUTOESSAY_INITIAL_ADMIN_USERNAME` と `AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH` を設定してください。hash が未設定の場合、bootstrap path は account を作りません。

## Documentation

- [Requirements](docs/REQUIREMENTS.md)
- [Design notes](docs/DESIGN.md)
- [System explanation](docs/explained/SYSTEM_EXPLAINED.en.md)
- [Methodology reference](references/methodology.md)
- [Security policy](SECURITY.md)
- [Contributing guide](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
