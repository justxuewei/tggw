from __future__ import annotations

import hmac
import html
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
from flask import Flask, g, jsonify, request


logger = logging.getLogger("tggw")


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    logger.setLevel(logging.INFO)

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> bool:
        return False


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


class TelegramError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"telegram api returned {status_code}: {detail}")


@dataclass(frozen=True)
class GatewayConfig:
    api_auth_token: str
    telegram_bot_token: str
    telegram_chat_id: str
    webhook_path: str = "/api/messages"
    listen_host: str = "0.0.0.0"
    port: int = 8080
    telegram_api_base: str = "https://api.telegram.org"
    telegram_parse_mode: str | None = None
    telegram_timeout_seconds: float = 10.0
    max_message_chars: int = 3900
    grafana_path: str = "/api/grafana"
    grafana_template: str = "vps-network-monitoring"
    record_db_path: str = "tggw-records.db"
    record_ttl_seconds: float = 24 * 60 * 60
    edit_window_seconds: float = 47 * 60 * 60
    rollover_seconds: float = 24 * 60 * 60

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        load_dotenv()

        max_message_chars = _int_env("MAX_MESSAGE_CHARS", 3900)
        if max_message_chars < 1 or max_message_chars > 4096:
            raise ConfigError("MAX_MESSAGE_CHARS must be between 1 and 4096")

        return cls(
            api_auth_token=_required_env("API_AUTH_TOKEN"),
            telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_required_env("TELEGRAM_CHAT_ID"),
            webhook_path=_normalize_path(os.getenv("WEBHOOK_PATH", "/api/messages")),
            listen_host=os.getenv("LISTEN_HOST", "0.0.0.0"),
            port=_int_env("PORT", 8080),
            telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            telegram_parse_mode=_optional_env("TELEGRAM_PARSE_MODE"),
            telegram_timeout_seconds=_float_env("TELEGRAM_TIMEOUT_SECONDS", 10.0),
            max_message_chars=max_message_chars,
            grafana_path=_normalize_path(os.getenv("GRAFANA_PATH", "/api/grafana")),
            grafana_template=os.getenv("GRAFANA_TEMPLATE", "vps-network-monitoring"),
            record_db_path=os.getenv("RECORD_DB_PATH", "tggw-records.db"),
            record_ttl_seconds=_float_env("RECORD_TTL_HOURS", 24.0) * 60 * 60,
            edit_window_seconds=_float_env("EDIT_WINDOW_HOURS", 47.0) * 60 * 60,
            rollover_seconds=_float_env("ROLLOVER_HOURS", 24.0) * 60 * 60,
        )


# Sentinel for "use the configured TELEGRAM_PARSE_MODE". Passing parse_mode=None
# explicitly forces a plain-text message regardless of the global setting, which
# the Grafana path relies on so its already-rendered text is never reinterpreted
# as HTML/Markdown (and only the strikethrough opts into HTML, with escaping).
_USE_GLOBAL_PARSE_MODE = object()


class TelegramClient:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def send_message(
        self,
        text: str,
        reply_to_message_id: int | None = None,
        parse_mode: str | None | object = _USE_GLOBAL_PARSE_MODE,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._config.telegram_chat_id,
            "text": text,
        }
        if reply_to_message_id is not None:
            # allow_sending_without_reply: still deliver if the original was deleted.
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        self._apply_parse_mode(payload, parse_mode)
        return self._call("sendMessage", payload)

    def edit_message(
        self,
        message_id: int,
        text: str,
        parse_mode: str | None | object = _USE_GLOBAL_PARSE_MODE,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": self._config.telegram_chat_id,
            "message_id": message_id,
            "text": text,
        }
        self._apply_parse_mode(payload, parse_mode)
        return self._call("editMessageText", payload)

    def _apply_parse_mode(self, payload: dict[str, Any], parse_mode: str | None | object) -> None:
        if parse_mode is _USE_GLOBAL_PARSE_MODE:
            parse_mode = self._config.telegram_parse_mode
        if parse_mode:
            payload["parse_mode"] = parse_mode

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._config.telegram_api_base}/bot{self._config.telegram_bot_token}/{method}"
        response = requests.post(url, json=payload, timeout=self._config.telegram_timeout_seconds)
        try:
            body = response.json()
        except ValueError:
            body = {"description": response.text}

        if not response.ok or not body.get("ok", False):
            raise TelegramError(response.status_code, body)

        return body


@dataclass(frozen=True)
class MessageRecord:
    key: str
    message_id: int
    text: str
    started: float      # true incident start, preserved across daily rollovers
    sent_at: float      # when the *current* message was sent (rollover/edit-window anchor)
    last_update: float


class RecordStore:
    """Maps an alert identity to the Telegram message it owns.

    A single sqlite connection guarded by a lock: enough for the gunicorn
    single-worker deployment, and sqlite takes care of durable writes so the
    message ids survive restarts.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                " key TEXT PRIMARY KEY,"
                " message_id INTEGER NOT NULL,"
                " text TEXT NOT NULL DEFAULT '',"
                " sent_at REAL NOT NULL,"
                " last_update REAL NOT NULL)"
            )
            # Migrate tables created before the text/started columns existed.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)")}
            if "text" not in columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN text TEXT NOT NULL DEFAULT ''")
            if "started" not in columns:
                self._conn.execute("ALTER TABLE messages ADD COLUMN started REAL NOT NULL DEFAULT 0")
                # Pre-rollover rows: seed started from sent_at (their original send).
                self._conn.execute("UPDATE messages SET started = sent_at WHERE started = 0")
            self._conn.commit()

    def get(self, key: str) -> MessageRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, message_id, text, started, sent_at, last_update"
                " FROM messages WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return MessageRecord(
            key=row["key"],
            message_id=row["message_id"],
            text=row["text"],
            started=row["started"],
            sent_at=row["sent_at"],
            last_update=row["last_update"],
        )

    def save(
        self,
        key: str,
        message_id: int,
        text: str,
        started: float,
        sent_at: float,
        last_update: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (key, message_id, text, started, sent_at, last_update)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " message_id = excluded.message_id,"
                " text = excluded.text,"
                " started = excluded.started,"
                " sent_at = excluded.sent_at,"
                " last_update = excluded.last_update",
                (key, message_id, text, started, sent_at, last_update),
            )
            self._conn.commit()

    def touch(self, key: str, last_update: float, text: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET last_update = ?, text = ? WHERE key = ?",
                (last_update, text, key),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE key = ?", (key,))
            self._conn.commit()

    def sweep(self, older_than: float) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE last_update < ?", (older_than,)
            )
            self._conn.commit()
            return cursor.rowcount

    def count(self) -> int:
        """Return the number of tracked records, exercising the DB connection."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return int(row[0])


def create_app(
    config: GatewayConfig | None = None,
    telegram_client: TelegramClient | None = None,
    record_store: RecordStore | None = None,
    clock: Callable[[], float] | None = None,
) -> Flask:
    _configure_logging()
    gateway_config = config or GatewayConfig.from_env()
    client = telegram_client or TelegramClient(gateway_config)
    store = record_store or RecordStore(gateway_config.record_db_path)
    now = clock or time.time
    started_at = now()

    app = Flask(__name__)

    @app.before_request
    def _record_start() -> None:
        g._start_ns = time.perf_counter_ns()

    @app.after_request
    def _log_access(response):
        start = getattr(g, "_start_ns", None)
        duration_ms = (time.perf_counter_ns() - start) / 1_000_000 if start else -1
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "-").split(",")[0].strip()
        logger.info(
            "access %s %s %s -> %d %.1fms",
            client_ip,
            request.method,
            request.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.get("/health")
    def health() -> tuple[Any, int]:
        return jsonify({"ok": True}), 200

    @app.get("/healthz")
    def healthz() -> tuple[Any, int]:
        current = now()
        try:
            tracked = store.count()
            db_ok = True
        except Exception:  # noqa: BLE001 - report any DB failure as unhealthy
            logger.exception("healthz: record store check failed")
            tracked = None
            db_ok = False
        ok = db_ok
        page = _render_healthz(
            ok=ok,
            db_ok=db_ok,
            tracked=tracked,
            started_at=started_at,
            uptime_seconds=current - started_at,
        )
        return page, (200 if ok else 503), {"Content-Type": "text/html; charset=utf-8"}

    def send_message_response(message: str, parse_mode: str | None = None) -> tuple[Any, int]:
        try:
            telegram_result = client.send_message(
                _truncate(message, gateway_config.max_message_chars), parse_mode=parse_mode
            )
        except TelegramError as exc:
            logger.error("telegram send failed: status=%s detail=%s", exc.status_code, exc.detail)
            return _telegram_error_response("telegram_send_failed", exc)
        except requests.RequestException as exc:
            logger.exception("telegram request failed: %s", exc)
            return _telegram_request_error_response(exc)

        message_id = _message_id(telegram_result)
        logger.info("telegram message sent: id=%s", message_id)
        return jsonify({"ok": True, "telegram_message_id": message_id}), 202

    def post_message_handler() -> tuple[Any, int]:
        if not _is_authorized(gateway_config.api_auth_token):
            logger.warning("unauthorized POST to %s", request.path)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        try:
            message = _message_from_request(gateway_config.max_message_chars)
            parse_mode = _parse_mode_from_request()
        except ValueError as exc:
            logger.warning("bad POST payload on %s: %s", request.path, exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

        return send_message_response(message, parse_mode)

    def get_message_handler() -> tuple[Any, int]:
        if not _is_query_authorized(gateway_config.api_auth_token):
            logger.warning("unauthorized GET to %s", request.path)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        message = _message_from_query(request.args.get("message", ""))
        if not message:
            logger.warning("missing message query on %s", request.path)
            return jsonify({"ok": False, "error": "message query parameter is required"}), 400

        try:
            parse_mode = _parse_mode_from_request()
        except ValueError as exc:
            logger.warning("bad parse_mode on %s: %s", request.path, exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

        return send_message_response(message, parse_mode)

    def patch_message_handler(message_id: int) -> tuple[Any, int]:
        if not _is_authorized(gateway_config.api_auth_token):
            logger.warning("unauthorized PATCH to %s", request.path)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        try:
            message = _message_from_request(gateway_config.max_message_chars)
            parse_mode = _parse_mode_from_request()
        except ValueError as exc:
            logger.warning("bad PATCH payload on %s: %s", request.path, exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

        try:
            client.edit_message(message_id, message, parse_mode=parse_mode)
        except TelegramError as exc:
            if _is_not_modified(exc.detail):
                logger.info("telegram message %s unchanged", message_id)
                return jsonify({"ok": True, "telegram_message_id": message_id, "action": "unchanged"}), 200
            logger.error("telegram edit failed: status=%s detail=%s", exc.status_code, exc.detail)
            return _telegram_error_response("telegram_edit_failed", exc)
        except requests.RequestException as exc:
            logger.exception("telegram request failed: %s", exc)
            return _telegram_request_error_response(exc)

        logger.info("telegram message edited: id=%s", message_id)
        return jsonify({"ok": True, "telegram_message_id": message_id, "action": "edited"}), 200

    def dispatch_grafana(key: str, text: str, resolved: bool) -> tuple[int, str]:
        """Pick send-vs-edit for an alert. Sends notify; edits are silent.

        Firing: send (push) the first time we see a key, then silently edit that
        message on follow-ups (escalation, changing loss). Resolved: always send
        a fresh notifying message and retire the record, since a silent edit
        would let the recovery slip by unnoticed.

        Returns (message_id, action). Raises TelegramError/RequestException so the
        caller can map transport failures to 502.
        """
        moment = now()
        max_chars = gateway_config.max_message_chars
        # Grafana text is already rendered, so force plain (parse_mode=None) and
        # keep it immune to a global TELEGRAM_PARSE_MODE; only the strikethrough
        # opts into HTML, and it escapes its content.
        if resolved:
            record = store.get(key)
            # Reply to the firing message so the recovery quotes it (tap-to-jump),
            # which also references the original where message links don't exist
            # (e.g. private chats). Replying has no time limit, unlike editing.
            reply_to = record.message_id if record is not None else None
            started = record.started if record is not None else moment
            rendered = _with_timeline(text, started, moment, max_chars)
            # Send the recovery first: it's the notification that matters. If it
            # raises we return 502 without having struck the firing bubble or
            # dropped the record, so Grafana's retry finds consistent state
            # (rather than a struck "inactive" bubble with no recovery sent).
            result = client.send_message(rendered, reply_to_message_id=reply_to, parse_mode=None)
            if record is not None and (moment - record.sent_at) <= gateway_config.edit_window_seconds:
                # Best effort: strike the firing bubble so it reads as no longer active.
                try:
                    client.edit_message(
                        record.message_id, _strikethrough_html(record.text), parse_mode="HTML"
                    )
                except (TelegramError, requests.RequestException) as exc:
                    logger.warning("could not strike firing message %s: %s", record.message_id, exc)
            store.delete(key)
            return _message_id(result), "resolved"

        record = store.get(key)
        # Edit the current bubble in place until it ages past the rollover window
        # (also capped below Telegram's edit limit). started stays anchored to the
        # incident start so the duration keeps climbing across rollovers.
        rollover_after = min(gateway_config.rollover_seconds, gateway_config.edit_window_seconds)
        if record is not None and (moment - record.sent_at) < rollover_after:
            rendered = _with_timeline(text, record.started, moment, max_chars)
            try:
                client.edit_message(record.message_id, rendered, parse_mode=None)
                store.touch(key, moment, rendered)
                return record.message_id, "edited"
            except TelegramError as exc:
                if _is_not_modified(exc.detail):
                    store.touch(key, moment, rendered)
                    return record.message_id, "unchanged"
                if not _is_uneditable(exc.detail):
                    raise
                logger.info(
                    "telegram message %s no longer editable (%s); rolling over",
                    record.message_id,
                    _error_description(exc.detail),
                )

        # Roll over: a long-running incident has outlived the current bubble (or
        # it became uneditable). Strike the old message, then push a NEW one that
        # replies to it, so the chat shows a daily chain and pings once a day.
        started = record.started if record is not None else moment
        reply_to = record.message_id if record is not None else None
        if record is not None:
            try:
                client.edit_message(
                    record.message_id, _strikethrough_html(record.text), parse_mode="HTML"
                )
            except (TelegramError, requests.RequestException) as exc:
                logger.warning("could not strike rolled-over message %s: %s", record.message_id, exc)
        rendered = _with_timeline(text, started, moment, max_chars)
        result = client.send_message(rendered, reply_to_message_id=reply_to, parse_mode=None)
        message_id = _message_id(result)
        if message_id is None:
            # ok=true but no id (malformed/proxied response): don't persist a
            # null record; the next alert will simply send fresh.
            logger.warning("telegram send for key=%s returned no message_id; not recording", key)
        else:
            store.save(key, message_id, rendered, started, moment, moment)
        return message_id, ("rolled" if record is not None else "sent")

    def grafana_handler() -> tuple[Any, int]:
        if not _is_authorized(gateway_config.api_auth_token):
            logger.warning("unauthorized POST to %s", request.path)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            logger.warning("bad grafana payload on %s", request.path)
            return jsonify({"ok": False, "error": "json object body is required"}), 400

        # This endpoint is scoped to one template, carried in the payload as the
        # `template` (or `service`) alert label; reject anything else so
        # misrouting is loud.
        template = _grafana_template(payload)
        if template != gateway_config.grafana_template:
            logger.warning("rejected grafana template %r on %s", template, request.path)
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "unsupported template",
                        "template": template,
                        "supported": gateway_config.grafana_template,
                    }
                ),
                400,
            )

        store.sweep(now() - gateway_config.record_ttl_seconds)

        key = _grafana_key(payload)
        text = _grafana_text(payload)
        if _grafana_nodata(payload) and payload.get("status") != "resolved":
            # Grafana renders no-data alerts with the frozen last-data summary
            # (e.g. "down (loss 90%)") because the contact-point template can't
            # see the no-data state at render time. The state labels ARE in the
            # payload though, so detect it here and say what's actually wrong.
            # Resolution follows the normal recovery path (no special text).
            observer = (payload.get("commonLabels") or {}).get("observer") or "observer"
            text = f"🔴 Grafana Alert\nmessage: {observer} not reporting (down?)\nobserver: {observer}"
        if not key:
            return jsonify({"ok": False, "error": "could not derive an alert key from payload"}), 400
        if not text:
            return jsonify({"ok": False, "error": "could not derive message text from payload"}), 400

        resolved = payload.get("status") == "resolved"
        try:
            message_id, action = dispatch_grafana(key, text, resolved)
        except TelegramError as exc:
            logger.error("telegram grafana failed: status=%s detail=%s", exc.status_code, exc.detail)
            return _telegram_error_response("telegram_send_failed", exc)
        except requests.RequestException as exc:
            logger.exception("telegram request failed: %s", exc)
            return _telegram_request_error_response(exc)

        logger.info("grafana alert %s: key=%s id=%s", action, key, message_id)
        return (
            jsonify({"ok": True, "action": action, "telegram_message_id": message_id, "key": key}),
            200,
        )

    app.add_url_rule("/api/messages", "post_message", post_message_handler, methods=["POST"])
    app.add_url_rule("/api/messages", "get_message", get_message_handler, methods=["GET"])
    app.add_url_rule(
        "/api/messages/<int:message_id>", "patch_message", patch_message_handler, methods=["PATCH"]
    )
    app.add_url_rule(gateway_config.grafana_path, "grafana", grafana_handler, methods=["POST"])
    if gateway_config.webhook_path != "/api/messages":
        app.add_url_rule(gateway_config.webhook_path, "post_webhook_message", post_message_handler, methods=["POST"])
        app.add_url_rule(gateway_config.webhook_path, "get_webhook_message", get_message_handler, methods=["GET"])

    return app


def _message_id(result: dict[str, Any]) -> int | None:
    inner = result.get("result")
    return inner.get("message_id") if isinstance(inner, dict) else None


def _telegram_error_response(error_code: str, exc: "TelegramError") -> tuple[Any, int]:
    return (
        jsonify(
            {
                "ok": False,
                "error": error_code,
                "telegram_status": exc.status_code,
                "telegram_detail": exc.detail,
            }
        ),
        502,
    )


def _telegram_request_error_response(exc: Exception) -> tuple[Any, int]:
    return jsonify({"ok": False, "error": "telegram_request_failed", "detail": str(exc)}), 502


def _is_authorized(expected_token: str) -> bool:
    supplied = _auth_token_from_request()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected_token)


def _is_query_authorized(expected_token: str) -> bool:
    supplied = request.args.get("token", "")
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected_token)


def _auth_token_from_request() -> str | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token.strip()

    api_key = request.headers.get("X-API-Key")
    if api_key:
        return api_key.strip()

    return None


def _message_from_request(max_chars: int) -> str:
    if not request.data:
        raise ValueError("request body is required")

    payload = request.get_json(silent=True)
    if payload is None:
        message = request.get_data(as_text=True).strip()
    else:
        message = _message_from_payload(payload)

    if not message:
        raise ValueError("message text is required")

    return _truncate(message, max_chars)


def _message_from_query(message: str) -> str:
    return (
        message.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .strip()
    )


def _message_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("text", "message", "body"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            details = {
                key: value
                for key, value in payload.items()
                if key != "title" and key not in _PAYLOAD_CONTROL_FIELDS
            }
            if details:
                return f"{title.strip()}\n\n{_json_dump(details)}"
            return title.strip()

        if any(key in payload for key in _PAYLOAD_CONTROL_FIELDS):
            forwarded = {
                key: value
                for key, value in payload.items()
                if key not in _PAYLOAD_CONTROL_FIELDS
            }
            if not forwarded:
                return ""
            return _json_dump(forwarded)

    return _json_dump(payload)


# Request-level parse modes. Omitted/null/PLAINTEXT means "send plain text";
# HTML opts one message into Telegram HTML rendering.
_REQUEST_PARSE_MODE_ALIASES = {
    "html": "HTML",
    "plaintext": None,
    "plain": None,
    "text": None,
    "": None,
}
_PAYLOAD_CONTROL_FIELDS = {"parse_mode"}


def _normalize_parse_mode(raw: Any) -> str | None:
    """Validate a request-supplied parse_mode."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("parse_mode must be a string")
    text = raw.strip()
    key = text.lower()
    if key not in _REQUEST_PARSE_MODE_ALIASES:
        raise ValueError(f"unsupported parse_mode: {raw!r} (use HTML or PLAINTEXT)")
    return _REQUEST_PARSE_MODE_ALIASES[key]


def _parse_mode_from_request() -> str | None:
    """Per-message parse_mode from the JSON body, or query string for GET."""
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and "parse_mode" in payload:
        return _normalize_parse_mode(payload.get("parse_mode"))
    if "parse_mode" in request.args:
        return _normalize_parse_mode(request.args.get("parse_mode"))
    return None


def _error_description(detail: Any) -> str:
    if isinstance(detail, dict):
        description = detail.get("description")
        if isinstance(description, str):
            return description
    return str(detail)


def _is_not_modified(detail: Any) -> bool:
    return "message is not modified" in _error_description(detail).lower()


def _is_uneditable(detail: Any) -> bool:
    # Telegram refuses to edit a message that is too old (~48h), was deleted, or
    # never existed. In those cases the caller should fall back to sending anew.
    description = _error_description(detail).lower()
    return any(
        marker in description
        for marker in (
            "message can't be edited",
            "message to edit not found",
            "message_id_invalid",
            "message identifier is not specified",
        )
    )


def _strikethrough_html(text: str) -> str:
    return f"<s>{html.escape(text, quote=False)}</s>"


def _format_time(epoch: float) -> str:
    # Local time, honouring the container's TZ (set via the TZ env var).
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h{minutes}m"


def _render_healthz(
    *,
    ok: bool,
    db_ok: bool,
    tracked: int | None,
    started_at: float,
    uptime_seconds: float,
) -> str:
    """Render a small self-contained HTML status page for the gateway."""
    status_label = "Healthy" if ok else "Unhealthy"
    accent = "#16a34a" if ok else "#dc2626"
    db_label = "ok" if db_ok else "error"
    tracked_label = "—" if tracked is None else str(tracked)
    rows = (
        ("Status", status_label),
        ("Database", db_label),
        ("Tracked records", tracked_label),
        ("Uptime", _format_duration(uptime_seconds)),
        ("Started", _format_time(started_at)),
    )
    rows_html = "\n".join(
        f'      <tr><th>{html.escape(name)}</th>'
        f"<td>{html.escape(value)}</td></tr>"
        for name, value in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>tggw health</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
      font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif;
      background: #0b0f17; color: #e5e7eb; }}
    .card {{ background: #111827; border: 1px solid #1f2937; border-radius: 12px;
      padding: 28px 32px; min-width: 320px; box-shadow: 0 10px 30px rgba(0,0,0,.35); }}
    h1 {{ margin: 0 0 4px; font-size: 18px; display: flex; align-items: center; gap: 10px; }}
    .dot {{ width: 12px; height: 12px; border-radius: 50%; background: {accent};
      box-shadow: 0 0 0 4px {accent}22; }}
    .sub {{ margin: 0 0 20px; color: #9ca3af; font-size: 13px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; padding: 8px 0; border-bottom: 1px solid #1f2937; }}
    th {{ color: #9ca3af; font-weight: 500; }}
    td {{ text-align: right; font-variant-numeric: tabular-nums; }}
    tr:last-child th, tr:last-child td {{ border-bottom: 0; }}
  </style>
</head>
<body>
  <main class="card">
    <h1><span class="dot"></span>tggw — {html.escape(status_label)}</h1>
    <p class="sub">Telegram gateway health check</p>
    <table>
{rows_html}
    </table>
  </main>
</body>
</html>
"""


def _with_timeline(text: str, started: float, updated: float, max_chars: int) -> str:
    # Build the footer first and truncate only the body, so the timeline this
    # feature exists to show always survives — never the part that gets chopped.
    footer = (
        f"started: {_format_time(started)}\n"
        f"updated: {_format_time(updated)}\n"
        f"duration: {_format_duration(updated - started)}"
    )
    body = _truncate(text, max(1, max_chars - len(footer) - 1))
    return f"{body}\n{footer}"


def _grafana_nodata(payload: dict[str, Any]) -> bool:
    # Grafana marks no-data alerts with grafana_state_reason="NoData" (plus
    # datasource_uid/ref_id), but NOT inside commonLabels/labels — it places
    # them elsewhere in the webhook. Scan the whole payload so detection doesn't
    # depend on the exact location.
    def has_marker(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("grafana_state_reason") == "NoData" or node.get("datasource_uid"):
                return True
            return any(has_marker(value) for value in node.values())
        if isinstance(node, list):
            return any(has_marker(value) for value in node)
        return False

    return has_marker(payload)


def _grafana_template(payload: dict[str, Any]) -> str | None:
    # Carried in the JSON payload as an alert label (Grafana surfaces labels under
    # commonLabels). Prefer a dedicated `template` label, else the `service` label.
    labels = payload.get("commonLabels")
    if isinstance(labels, dict):
        for label in ("template", "service"):
            value = labels.get(label)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _grafana_key(payload: dict[str, Any]) -> str | None:
    # groupKey identifies the notification stream that maps 1:1 to a rendered
    # message; fall back to the observer/device labels, then a fingerprint.
    group_key = payload.get("groupKey")
    if isinstance(group_key, str) and group_key.strip():
        return group_key.strip()

    labels = payload.get("commonLabels")
    if isinstance(labels, dict):
        observer = labels.get("observer")
        device = labels.get("device")
        if observer and device:
            return f"{observer}->{device}"

    alerts = payload.get("alerts")
    if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict):
        fingerprint = alerts[0].get("fingerprint")
        if fingerprint:
            return str(fingerprint)

    return None


def _grafana_text(payload: dict[str, Any]) -> str:
    # Returned untruncated; _with_timeline truncates the body once, with room
    # reserved for the appended timeline footer.
    for key in ("message", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _truncate(message: str, max_chars: int) -> str:
    if len(message) <= max_chars:
        return message
    suffix = "\n\n[truncated]"
    if max_chars <= len(suffix):
        return message[:max_chars]
    return message[: max_chars - len(suffix)] + suffix


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _required_env(name: str) -> str:
    value = _optional_env(name)
    if value is None:
        raise ConfigError(f"{name} is required")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0")
    return value


def _normalize_path(path: str) -> str:
    normalized = "/" + path.strip("/")
    if normalized == "/":
        raise ConfigError("WEBHOOK_PATH must not be /")
    return normalized


if __name__ == "__main__":
    runtime_config = GatewayConfig.from_env()
    create_app(runtime_config).run(host=runtime_config.listen_host, port=runtime_config.port)
