# ADR-0003 Delta: Express Transport Moves From Codex CLI To HTTP

Date: 2026-05-19

Status: Accepted as production hotfix delta

## Context

ADR-0003 originally described express generation as ARS `academic-paper` single-call generation via Codex CLI pinned to `gpt-5.4`. That worked in local mirror because the host had a `codex` executable on `PATH`, but the production Docker image does not vendor that binary. Production express runs therefore failed before the ARS call with `express_transport_error`.

The failure is an environment-coupling bug: the product path depended on a host-local CLI that is not part of the deployed backend image.

## Delta Decision

Express production transport now uses the existing OpenAI-compatible `llm_client` HTTP provider chain, pinned to `AUTOESSAY_EXPRESS_MODEL` with default `gpt-5.4`.

The Codex CLI adapter remains available as an explicit adapter for experiments or targeted tests, but it is no longer the default express runner transport.

## Rationale

The HTTP provider chain is already a production dependency for other LLM-backed paths, including kernel suggestion. Reusing it avoids adding a Node/npm global CLI dependency to the Python API image, removes host `PATH` drift, and keeps provider credentials in the existing `ONE_API_*` / `AUTOESSAY_LLM_PROVIDERS` configuration surface.

This is not a silent fallback. Express does not try Codex CLI first and then hide a CLI failure behind HTTP. The default transport is HTTP from the start; provider-chain failover is the explicit behavior of `llm_client`, and express still fails as `express_transport_error` if that chain is unavailable or exhausted.

## Compatibility

`AUTOESSAY_EXPRESS_CODEX_MODEL` is still accepted as a legacy alias for the new `AUTOESSAY_EXPRESS_MODEL` setting so existing environment files keep pinning express to `gpt-5.4` during rollout.
