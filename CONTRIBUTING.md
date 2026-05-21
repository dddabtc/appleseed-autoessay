# Contributing

Thanks for helping improve Appleseed AutoEssay.

## Development Checks

Install backend and frontend dependencies, then run:

```bash
backend/scripts/ci-local.sh
```

The script runs backend formatting, lint, typecheck, tests, frontend typecheck, lint, and unit tests.

## Pull Requests

- Keep changes focused and include tests for behavior changes.
- Do not commit `.env` files, local databases, generated manuscripts, experiment outputs, private prompt payloads, screenshots from real runs, or deployment-specific notes.
- Use placeholders such as `example.invalid`, `127.0.0.1`, and `replace-with-token` in docs and examples.
- Document user-visible behavior in `README.md` or `docs/` when appropriate.

## Local Data

Treat manuscript outputs, run artifacts, and uploaded PDFs as user data. Keep them outside git and scrub synthetic fixtures before committing them.
