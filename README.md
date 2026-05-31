# Telegram Gateway

Small authenticated HTTP gateway that sends incoming messages to a Telegram channel through a bot.

The implementation follows the same shape as `techprober/telegram-webhook-forwarder-bot`: an HTTP endpoint receives a webhook-style payload, formats it, and calls Telegram `sendMessage`.

## API

`POST /api/messages` sends one Telegram message. If `WEBHOOK_PATH` is set to a different path, that path is also registered.

Authenticate with either header:

```bash
Authorization: Bearer $API_AUTH_TOKEN
X-API-Key: $API_AUTH_TOKEN
```

Send JSON with `text`, `message`, or `body`:

```bash
curl -X POST https://tggw.example.com/api/messages \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"deployment finished"}'
```

For multiple paragraphs with `POST`, put `\n\n` in the JSON string:

```bash
curl -X POST https://tggw.example.com/api/messages \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"first paragraph\n\nsecond paragraph"}'
```

If none of those fields are present, the gateway forwards the whole JSON payload as pretty-printed text. Plain text request bodies are also accepted.

For quick manual sends, `GET /api/messages?token=...&message=...` is also available:

```bash
curl 'https://tggw.example.com/api/messages?token=replace-with-a-long-random-secret&message=deployment%20finished'
```

For multiple paragraphs with `GET`, use URL-encoded newlines (`%0A`) or escaped newlines (`\n`):

```bash
curl 'https://tggw.example.com/api/messages?token=replace-with-a-long-random-secret&message=first%20paragraph%0A%0Asecond%20paragraph'
```

## Editing Messages

`PATCH /api/messages/<message_id>` edits an existing Telegram message in place. The body is read the same way as `POST` (`text`, `message`, `body`, or raw text), and the same auth headers apply:

```bash
curl -X PATCH https://tggw.example.com/api/messages/4242 \
  -H "Authorization: Bearer $API_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"deployment finished (updated)"}'
```

Editing to the exact text Telegram already shows is a no-op: the gateway returns `200` with `"action": "unchanged"` rather than an error.

## Grafana Alerts

`POST /api/grafana` consumes Grafana's native webhook payload and keeps one Telegram message per alert stream, so repeat notifications and the firing→resolved transition edit that single message instead of flooding the channel.

This endpoint is scoped to a single template (default `vps-network-monitoring`, set by `GRAFANA_TEMPLATE`). The template travels in the payload as an alert label — a dedicated `template` label if present, otherwise the `service` label — which Grafana surfaces under `commonLabels`. Requests for any other template are rejected with `400`. (Grafana webhooks can't carry arbitrary custom JSON fields, so a label is the JSON-native way to pass this.)

The gateway derives the message key from the payload, preferring `groupKey`, then the `observer`/`device` `commonLabels` (`observer->device`), then the first alert's `fingerprint`. Message text comes from the rendered `message` (or `title`) field, to which the gateway appends a timeline:

```
started:  <when this message was first sent>
updated:  <now>
duration: <elapsed, e.g. 45s / 12m / 1h30m>
```

`started` is anchored to the first send and `duration` ticks up on each edit; on resolve it shows the total incident length.

Telegram pushes a notification only for *new* messages; edits are silent. So the gateway sends (notifies) on the events worth a ping and edits silently for the rest:

- **first firing** for a key → sends a new message (push) and records the message id on disk;
- **follow-up firings** for the same key (escalation, changing loss) → silently edit that message in place, so repeats never add a new message (the `updated`/`duration` lines advance on each, so a repeat is a real but silent edit);
- **resolved** → strikes through the firing bubble (a silent `HTML` edit, so it reads as no longer active) and sends a fresh message (push), then retires the record, since a silent edit alone would let the recovery slip by unnoticed;
- if the original can no longer be edited (older than Telegram's ~48h limit, or deleted) → falls back to sending a new message.

Point a Grafana webhook contact point at this path with `httpMethod: POST` and a `Bearer` token. Records untouched for `RECORD_TTL_HOURS` (default 24) are swept automatically.

`GET /health` returns a simple health response and does not require auth.

## Telegram Setup

1. Create a bot with BotFather and set `TELEGRAM_BOT_TOKEN`.
2. Add the bot to the target channel as an admin with permission to post messages.
3. Set `TELEGRAM_CHAT_ID` to the channel username, such as `@your_channel_username`, or the numeric channel id.
4. Set `API_AUTH_TOKEN` to a long random secret.

Copy `.env.example` to `.env` and update the values.

## Local Run

```bash
uv venv .venv
uv pip install -r requirements.txt
uv run python app.py
```

## Docker

```bash
make build
docker run --rm -p 8080:8080 --env-file .env xavierniu/tggw:latest
```

Or pass secrets directly with `-e`:

```bash
docker run --rm -p 8080:8080 \
  -e API_AUTH_TOKEN='replace-with-a-long-random-secret' \
  -e TELEGRAM_BOT_TOKEN='123456789:replace-with-bot-token' \
  -e TELEGRAM_CHAT_ID='@your_channel_username' \
  xavierniu/tggw:latest
```

## HTTPS With Automatic Certificates

The included Compose stack runs the gateway behind Caddy:

```bash
docker compose up -d
```

Set `DOMAIN` in `.env` to a public DNS name pointing at this host. Caddy listens on ports `80` and `443`, obtains and renews TLS certificates automatically for the configured domain, and reverse proxies HTTPS traffic to the gateway container.

## Push To Docker Hub

The Makefile mirrors the neighboring `token-exporter` project:

```bash
make push
```

Override the Docker Hub repository or tag when needed:

```bash
make push IMAGE=xavierniu/tggw TAG=v1.0.0
```

## Configuration

| Env var | Default | Description |
|---|---:|---|
| `API_AUTH_TOKEN` | required | Bearer token or `X-API-Key` value required by message endpoints. |
| `TELEGRAM_BOT_TOKEN` | required | Telegram bot token from BotFather. |
| `TELEGRAM_CHAT_ID` | required | Target channel username or numeric chat id. |
| `WEBHOOK_PATH` | `/api/messages` | Optional extra POST path for webhook integrations. |
| `GRAFANA_PATH` | `/api/grafana` | Path for the Grafana alert endpoint that de-duplicates by editing. |
| `GRAFANA_TEMPLATE` | `vps-network-monitoring` | Only this template is served; requests for others are rejected. |
| `RECORD_DB_PATH` | `tggw-records.db` | SQLite file mapping alert keys to Telegram message ids. Mount a volume in Docker. |
| `RECORD_TTL_HOURS` | `24` | Drop alert records untouched for this many hours. |
| `EDIT_WINDOW_HOURS` | `47` | Send a fresh message instead of editing once the original is older than this. |
| `TZ` | `UTC` | Timezone for the `started`/`updated` timestamps. The image ships `tzdata`. |
| `PORT` | `8080` | Local Flask dev server port. Gunicorn in Docker listens on `8080`. |
| `DOMAIN` | required for Compose TLS | Public hostname used by Caddy for HTTPS. |
| `TELEGRAM_PARSE_MODE` | empty | Optional Telegram parse mode, for example `HTML` or `MarkdownV2`. |
| `TELEGRAM_TIMEOUT_SECONDS` | `10` | Telegram API request timeout. |
| `MAX_MESSAGE_CHARS` | `3900` | Maximum message length before truncation. Must be no more than Telegram's 4096 character limit. |
