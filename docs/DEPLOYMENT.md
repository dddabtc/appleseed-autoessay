# Deployment

This guide is intentionally generic. Do not commit hostnames, private IPs, credentials, private network names, or operator notes from a real deployment.

## Local Docker Compose

1. Copy `.env.example` to `.env`.
2. Fill in provider tokens and service URLs in `.env`.
3. Start the stack:

```bash
docker compose -f docker-compose.yml up --build
```

The default compose file binds the API to `127.0.0.1:8017` and the frontend to `127.0.0.1:3017`.

## First User Setup

The app does not create a default production account.

To bootstrap the first password user, generate a bcrypt hash locally and provide both:

```bash
AUTOESSAY_INITIAL_ADMIN_USERNAME=replace-with-username
AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH=replace-with-bcrypt-hash
```

Leave `AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH` unset when you use a separate account-management process.

## Reverse Proxy

Put your own TLS and reverse proxy in front of the two local services:

- frontend: `http://127.0.0.1:3017`
- API: `http://127.0.0.1:8017`

Route `/api/`, `/sse/`, `/healthz`, `/readyz`, `/version`, `/docs`, and `/openapi.json` to the API. Route all other paths to the frontend.

Use your deployment platform's secret store for `.env` values. Never commit real `.env` files.
