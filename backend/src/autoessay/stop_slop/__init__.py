"""Runtime adapter for the bundled stop-slop rule references."""

from autoessay.stop_slop.rules import (
    StopSlopRules,
    StructuralPattern,
    load_stop_slop_rules,
    resolve_stop_slop_dir,
)
from autoessay.stop_slop.score import score_text, score_text_static

__all__ = [
    "StopSlopRules",
    "StructuralPattern",
    "load_stop_slop_rules",
    "resolve_stop_slop_dir",
    "score_text",
    "score_text_static",
]
