"""Run-level manuscript generation mode registry.

``generation_mode`` is deliberately separate from ``paper_mode``:
paper_mode describes article shape, while generation_mode selects the
backend architecture that will produce the manuscript.
"""

from __future__ import annotations

from typing import Literal

GenerationMode = Literal["express", "deep"]

EXPRESS_MODE: GenerationMode = "express"
DEEP_MODE: GenerationMode = "deep"
VALID_GENERATION_MODES: frozenset[str] = frozenset({EXPRESS_MODE, DEEP_MODE})


def normalize_generation_mode(value: str | None) -> GenerationMode:
    cleaned = (value or "").strip()
    if cleaned not in VALID_GENERATION_MODES:
        raise ValueError("mode must be one of: express, deep")
    return cleaned  # type: ignore[return-value]
