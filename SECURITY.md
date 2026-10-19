# Security Policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private
vulnerability reporting (Repository → Security → Advisories → Report a
vulnerability) or contact the repository owner directly.

Include:

- affected version/commit;
- reproduction steps or a proof of concept;
- impact (what data/actions are exposed);
- whether the issue is already public.

## Deployment checklist

- `ALLOW_DEV_INIT_DATA=false`
- `ENABLE_API_DOCS=false`
- `BOT_MODE=webhook`: `WEBHOOK_SECRET` is random and at least 32 characters
- HTTPS reverse proxy in front of the app
- `.env` is never committed; rotate `BOT_TOKEN`/`WEBHOOK_SECRET` if leaked
- regular `make backup`
- `docker compose ps` shows `healthy` (`/readyz` checks DB and Bot API)
