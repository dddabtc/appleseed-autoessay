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

## GitHub Actions Secrets

The `deploy` workflow is generic and expects deployment details to be stored in
GitHub repository or `production` environment secrets. Configure these in
GitHub repo Settings -> Secrets and variables -> Actions:

- `PROD_HOST`: SSH host for the production deploy target.
- `PROD_USER`: SSH user used by the deploy workflow.
- `PROD_SSH_KEY`: Private SSH key that can connect to the deploy target.
- `PROD_DEPLOY_PATH`: Remote directory that contains the checked-out app and
  Docker Compose files.
- `GHCR_READ_TOKEN`: Optional token with package read access if the default
  `GITHUB_TOKEN` cannot pull from GHCR in your setup.
- `PROD_HEALTHCHECK_URL`: Optional API health URL reachable from the deploy
  target for post-deploy smoke checks.
- `PROD_FRONTEND_URL`: Optional frontend URL reachable from the deploy target
  for post-deploy smoke checks.

Do not put real hostnames, private IPs, credentials, private network names, or
provider-specific secrets in workflow YAML. Keep those values in GitHub Secrets
or in the deployment target's local environment files.
