#!/usr/bin/env python3
"""Architecture lint: every LLM call must go through harness.run_llm_step.

Rule: outside the allowlist, no module under `backend/src/autoessay/`
may invoke `<expr>.chat_completion(...)`. The hub is `run_llm_step`
in `harness/runner.py`; the multi-provider fallback lives in
`llm_client.py`. Every other caller must funnel through the hub so
pre_llm / post_llm hooks fire and audit records are written.

Implementation: AST scan for `Attribute(attr='chat_completion')` call
expressions. grep is a fallback sanity-check only and is not the
source of truth (variable renames defeat string match).

Exit codes:
  0  no violations
  1  violations found
  2  internal error (failed to parse a file, etc.)

PR-D2 (2026-05-03): introduced as part of `run_llm_step` strict-mode
migration. After D2 lands, `pr.yml` runs this lint in the backend
job. Adding new callers fails CI until they're routed through the
hub.

Allowlist (src):
  - backend/src/autoessay/harness/runner.py
  - backend/src/autoessay/llm_client.py
  - backend/src/autoessay/safety/input_guard.py  (D2.5 AuditSink migration)
  - backend/src/autoessay/stop_slop/score.py  (D2.5 AuditSink migration)

Allowlist (tests; explicitly opt-in):
  - backend/tests/test_llm_client.py  (tests the client itself)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Source paths to scan. Default: just src/. Tests are scanned only
# with a positive opt-in list because some test-harness fakes
# legitimately do direct chat_completion to assert client behavior.
SRC_DIR = REPO_ROOT / "backend" / "src" / "autoessay"

ALLOWLIST_RELATIVE = {
    Path("backend/src/autoessay/harness/runner.py"),
    Path("backend/src/autoessay/llm_client.py"),
    # Temporary PR-D2.2 allowlist: these two standalone guards do not
    # have Run/session context yet. PR-D2.5 routes them through AuditSink.
    Path("backend/src/autoessay/safety/input_guard.py"),
    Path("backend/src/autoessay/stop_slop/score.py"),
    # NOTE: backend/tests/test_llm_client.py is allowed by virtue of
    # not being scanned. Add other test files here only if they are
    # also explicitly scanned.
}


class ChatCompletionVisitor(ast.NodeVisitor):
    """Collect `<anything>.chat_completion(...)` call sites."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.violations: list[tuple[int, int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 ast API
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "chat_completion":
            # Capture the expression that yields the attribute, e.g.
            # `client`, `self.client`, `LLMClient(...)`, etc. We don't
            # need to dereference further: any `.chat_completion(` call
            # outside the allowlist is a violation.
            try:
                source = ast.unparse(func)
            except Exception:  # pragma: no cover - defensive
                source = "<expr>.chat_completion"
            self.violations.append((node.lineno, node.col_offset, source))
        self.generic_visit(node)


def scan_file(path: Path) -> list[tuple[int, int, str]]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        print(
            f"lint: failed to parse {path} — {exc}",
            file=sys.stderr,
        )
        raise
    visitor = ChatCompletionVisitor(path)
    visitor.visit(tree)
    return visitor.violations


def main() -> int:
    if not SRC_DIR.is_dir():
        print(f"lint: source dir not found: {SRC_DIR}", file=sys.stderr)
        return 2

    py_files = sorted(SRC_DIR.rglob("*.py"))
    if not py_files:
        print(f"lint: no python files under {SRC_DIR}", file=sys.stderr)
        return 2

    total_violations: list[tuple[Path, int, int, str]] = []
    for path in py_files:
        rel = path.relative_to(REPO_ROOT)
        if rel in ALLOWLIST_RELATIVE:
            continue
        try:
            for line, col, expr in scan_file(path):
                total_violations.append((rel, line, col, expr))
        except SyntaxError:
            return 2

    if not total_violations:
        print(
            "lint: 0 chat_completion violations "
            "(all LLM calls flow through harness.run_llm_step)"
        )
        return 0

    print(
        f"lint: {len(total_violations)} chat_completion call(s) outside allowlist:",
        file=sys.stderr,
    )
    for rel, line, col, expr in total_violations:
        print(f"  ✗ {rel}:{line}:{col}  {expr}(...)", file=sys.stderr)
    print(
        "\nFix: route the call through `harness.runner.run_llm_step` and "
        "let the hub emit pre_llm / post_llm hooks + audit records.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
