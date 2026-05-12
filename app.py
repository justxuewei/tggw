from __future__ import annotations

import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

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
        )


class TelegramClient:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def send_message(self, text: str) -> dict[str, Any]:
        url = f"{self._config.telegram_api_base}/bot{self._config.telegram_bot_token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": self._config.telegram_chat_id,
            "text": text,
        }
        if self._config.telegram_parse_mode:
            payload["parse_mode"] = self._config.telegram_parse_mode

        response = requests.post(url, json=payload, timeout=self._config.telegram_timeout_seconds)
        try:
            body = response.json()
        except ValueError:
            body = {"description": response.text}

        if not response.ok or not body.get("ok", False):
            raise TelegramError(response.status_code, body)

        return body


def create_app(
    config: GatewayConfig | None = None,
    telegram_client: TelegramClient | None = None,
) -> Flask:
    _configure_logging()
    gateway_config = config or GatewayConfig.from_env()
    client = telegram_client or TelegramClient(gateway_config)

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

    def send_message_response(message: str) -> tuple[Any, int]:
        try:
            telegram_result = client.send_message(_truncate(message, gateway_config.max_message_chars))
        except TelegramError as exc:
            logger.error(
                "telegram send failed: status=%s detail=%s", exc.status_code, exc.detail
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "telegram_send_failed",
                        "telegram_status": exc.status_code,
                        "telegram_detail": exc.detail,
                    }
                ),
                502,
            )
        except requests.RequestException as exc:
            logger.exception("telegram request failed: %s", exc)
            return jsonify({"ok": False, "error": "telegram_request_failed", "detail": str(exc)}), 502

        result = telegram_result.get("result", {})
        logger.info("telegram message sent: id=%s", result.get("message_id"))
        return (
            jsonify(
                {
                    "ok": True,
                    "telegram_message_id": result.get("message_id"),
                }
            ),
            202,
        )

    def post_message_handler() -> tuple[Any, int]:
        if not _is_authorized(gateway_config.api_auth_token):
            logger.warning("unauthorized POST to %s", request.path)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        try:
            message = _message_from_request(gateway_config.max_message_chars)
        except ValueError as exc:
            logger.warning("bad POST payload on %s: %s", request.path, exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

        return send_message_response(message)

    def get_message_handler() -> tuple[Any, int]:
        if not _is_query_authorized(gateway_config.api_auth_token):
            logger.warning("unauthorized GET to %s", request.path)
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        message = _message_from_query(request.args.get("message", ""))
        if not message:
            logger.warning("missing message query on %s", request.path)
            return jsonify({"ok": False, "error": "message query parameter is required"}), 400

        return send_message_response(message)

    app.add_url_rule("/api/messages", "post_message", post_message_handler, methods=["POST"])
    app.add_url_rule("/api/messages", "get_message", get_message_handler, methods=["GET"])
    if gateway_config.webhook_path != "/api/messages":
        app.add_url_rule(gateway_config.webhook_path, "post_webhook_message", post_message_handler, methods=["POST"])
        app.add_url_rule(gateway_config.webhook_path, "get_webhook_message", get_message_handler, methods=["GET"])

    return app


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
            details = {key: value for key, value in payload.items() if key != "title"}
            if details:
                return f"{title.strip()}\n\n{_json_dump(details)}"
            return title.strip()

    return _json_dump(payload)


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
