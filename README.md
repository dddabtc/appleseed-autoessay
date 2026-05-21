# Appleseed AutoEssay

**Languages:** English | [中文](README.zh.md) | [日本語](README.ja.md)

Appleseed AutoEssay is an open-source academic manuscript workflow tool. It turns a research question into a reviewable manuscript with source selection, phase gates, audit notes, and export files.

The project supports two generation modes:

- **ARS express:** a faster single-pass manuscript path for quick drafts.
- **13-phase deep:** a reviewable pipeline with proposal, source, synthesis, drafting, review, integrity, and export phases.

There is no hosted public service attached to this repository and there are no default production accounts.

## Quick Start

```bash
python3 -m venv backend/.venv
source backend/.venv/bin/activate
python -m pip install -e "backend[dev]"

( cd frontend && npm ci )
cp .env.example .env
DATABASE_URL=sqlite:///./autoessay.sqlite3 alembic -c backend/alembic.ini upgrade head
```

Run the local checks:

```bash
backend/scripts/ci-local.sh
```

Run the backend and frontend in separate shells:

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

Start from [.env.example](.env.example). The example file uses local addresses and placeholder values only. Provide your own OpenAI-compatible LLM gateway, Redis, database, and optional originality-check providers before running non-stubbed workflows.

For local development and CI, use stub flags for external LLM and vendor calls. Production deployments should supply their own account creation flow and secrets through the deployment platform, not through committed files.

To bootstrap the first password user, generate a bcrypt hash locally and set `AUTOESSAY_INITIAL_ADMIN_USERNAME` plus `AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH` in your private environment. The bootstrap path is disabled when the hash is unset.

## What It Does

- Creates academic manuscript runs from a title, research question, domain, paper mode, and notes.
- Supports manual review gates or auto-advance through eligible phases.
- Tracks source selection, material diagnosis, argument direction, draft, review findings, integrity findings, and exports.
- Exports Markdown, HTML, DOCX, LaTeX, BibTeX, CSL JSON, manifest, literature usage, and self-check files.
- Supports English, Chinese, and Japanese UI copy, with manuscript language configured per run.

## Documentation

- [Requirements](docs/REQUIREMENTS.md)
- [Design notes](docs/DESIGN.md)
- [System explanation](docs/explained/SYSTEM_EXPLAINED.en.md)
- [Methodology reference](references/methodology.md)
- [Security policy](SECURITY.md)
- [Contributing guide](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
