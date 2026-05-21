"""Input safety gate.

LLM-backed validator that decides whether free-text input from users is
safe to propagate into the writing pipeline. Catches off-topic content
and prompt-injection attempts before they reach agent prompts.
"""

from autoessay.safety.input_guard import (
    SafetyCheckResult,
    SafetyGateError,
    SafetyVerdict,
    validate_user_input,
)

__all__ = [
    "SafetyCheckResult",
    "SafetyGateError",
    "SafetyVerdict",
    "validate_user_input",
]
