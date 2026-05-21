# ADR 0002: ABC Experiment Execution Revisions

Date: 2026-05-16

Status: Accepted

## Context

The original ABC protocol pinned generation to one provider/model and disabled provider fallback. The current execution directive changes two operational constraints:

- The smoke and subsequent experiment runs must not touch the production stack.
- ABC generation should use the configured rightcode -> apiport -> minimax provider fallback chain, with concurrency controlled.

This changes what the experiment estimates. It no longer isolates a strict same-model comparison. It estimates behavior closer to production provider fallback, including the possibility that different arms land on different provider/model pairs.

## Decision

Use the A1 test stack for execution:

- local backend dev server
- isolated SQLite database
- isolated `AUTOESSAY_DATA_DIR`
- production provider environment copied into process env only
- no production stack restart, migration, database write, or file mutation

Generation configuration is revised to:

```python
GENERATION_MODEL_ID = "provider-configured-fallback-chain"
PROVIDER_FALLBACK_ALLOWED = True
PROVIDER_FALLBACK_CHAIN = ("rightcode", "apiport", "minimax")
DEFAULT_MAX_CONCURRENCY = 1
MAX_ALLOWED_CONCURRENCY = 3
```

`GENERATION_MODEL_ID` is now an abstract audit label. The actual model is each provider's configured model:

- rightcode: `gpt-5.4-mini`
- apiport: `gpt-5.4-mini`
- minimax: `MiniMax-M2.7`

The B/B'/C generator must pass the full provider chain to `LLMClient` and must not override the provider model. Provenance for every generated arm records:

- requested abstract model id
- actual provider
- actual provider model
- provider fallback allowed
- token usage

The aggregator adds provider/model distribution sensitivity fields. If B and B' use different provider/model pairs for the same kernel, that kernel's B vs B' comparison is downgraded to "self-critique with provider/model confound" for interpretation.

Concurrency is kernel-level only:

- default `N=1`
- hard maximum `N=3`
- smoke runs use `N=1`
- each kernel still runs A -> B -> B' -> C serially

## Consequences

The experiment estimate changes from strict same-model control to production-like provider fallback behavior. This is intentional for the revised execution directive, but conclusions must explicitly account for provider/model distribution and B/B' mismatches.

No production stack is modified by this revision. Production remains the source of provider credentials only, copied through environment variables and never written to repository files.
