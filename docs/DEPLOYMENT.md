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

## Webhook-based Local Deploy

The default production deploy path is local to the production host:

```text
GitHub push to main -> HTTPS webhook -> local receiver -> git reset -> docker compose build -> docker compose up -d
```

This does not require GHCR package writes or production SSH keys in GitHub
Actions secrets. The only shared credential is the GitHub webhook secret, stored
on the production host and in the GitHub webhook settings.

The examples below use `adnanh/webhook`. The public URL is:

```text
https://example.com/_github_webhook
```

The local receiver listens only on `127.0.0.1:9000`; the reverse proxy maps the
public path to the internal webhook hook path.

### Install receiver files

1. Install prerequisites on the production host:

```bash
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose-plugin webhook
```

2. Create a deploy user and directories:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin autoessay
sudo usermod -aG docker autoessay
sudo install -d -o autoessay -g autoessay /opt/appleseed-autoessay/source
sudo install -d -o autoessay -g autoessay /opt/appleseed-autoessay/webhook-receiver
sudo install -d -o autoessay -g autoessay /var/log/appleseed-autoessay
```

3. Check out the repository on the production host:

```bash
sudo -u autoessay git clone https://github.com/OWNER/REPO.git /opt/appleseed-autoessay/source
cd /opt/appleseed-autoessay/source
sudo -u autoessay git checkout main
```

4. Copy the receiver examples into the local runtime directory:

```bash
sudo install -m 0755 -o autoessay -g autoessay \
  ops/webhook-receiver/deploy.sh \
  /opt/appleseed-autoessay/webhook-receiver/deploy.sh
sudo install -m 0640 -o autoessay -g autoessay \
  ops/webhook-receiver/webhook.yaml.example \
  /opt/appleseed-autoessay/webhook-receiver/webhook.yaml
sudo install -m 0644 \
  ops/webhook-receiver/appleseed-webhook.service.example \
  /etc/systemd/system/appleseed-webhook.service
```

5. Generate a high-entropy webhook secret and store it only on the production
   host and in GitHub's webhook UI:

```bash
openssl rand -hex 32
sudo install -m 0640 -o root -g autoessay /dev/null /etc/autoessay-webhook.env
sudoedit /etc/autoessay-webhook.env
```

Use this environment file shape:

```bash
AUTOESSAY_GITHUB_WEBHOOK_SECRET=replace-with-generated-secret
AUTOESSAY_SOURCE_DIR=/opt/appleseed-autoessay/source
AUTOESSAY_DEPLOY_LOG=/var/log/appleseed-autoessay/deploy.log
```

6. Enable the receiver:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now appleseed-webhook.service
sudo systemctl status --no-pager appleseed-webhook.service
```

Unsigned or incorrectly signed requests must not deploy:

```bash
curl -i -X POST http://127.0.0.1:9000/hooks/appleseed-autoessay-main \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: push' \
  -d '{"ref":"refs/heads/main"}'
```

Expect a rejected response. A signed `push` event to `refs/heads/main` starts
`ops/webhook-receiver/deploy.sh`, which fetches `origin/main`, hard-resets the
local checkout, and then calls the existing `ops/deploy.sh`. If `git fetch` or
`git reset` fails, the script exits before any Docker build starts.

### Reverse proxy

Terminate TLS in the main reverse proxy and expose only the public webhook path.
For nginx:

```nginx
location = /_github_webhook {
    limit_except POST { deny all; }
    proxy_pass http://127.0.0.1:9000/hooks/appleseed-autoessay-main;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Optional nginx rate limiting:

```nginx
limit_req_zone $binary_remote_addr zone=github_webhook:10m rate=5r/m;

location = /_github_webhook {
    limit_req zone=github_webhook burst=5 nodelay;
    proxy_pass http://127.0.0.1:9000/hooks/appleseed-autoessay-main;
}
```

If you restrict by GitHub source IP, do it in the reverse proxy or firewall. The
receiver sees the proxy address when it runs behind nginx or caddy.

### Create the GitHub webhook

In GitHub:

1. Open the repository.
2. Go to Settings -> Webhooks -> Add webhook.
3. Set Payload URL to `https://example.com/_github_webhook`.
4. Set Content type to `application/json`.
5. Paste the same generated secret into Secret.
6. Choose "Let me select individual events" and select only Pushes.
7. Keep Active enabled and save.

GitHub sends an initial `ping` event after creation. This receiver rejects
non-`push` events, so the ping should not deploy. The first real push to `main`
should return `202 accepted` and then continue deployment in the receiver logs.

### Logs and operations

Use systemd and the deploy log for operational checks:

```bash
journalctl -u appleseed-webhook.service -f
tail -f /var/log/appleseed-autoessay/deploy.log
```

The deploy script uses a non-blocking `flock` lock. If a second webhook arrives
while a deployment is already running, the second deployment exits without
starting another Docker build.
