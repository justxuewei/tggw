from __future__ import annotations

import unittest

from app import GatewayConfig, TelegramError, create_app


class FakeTelegramClient:
    def __init__(self, result=None, error=None):
        self.messages = []
        self.result = result or {"ok": True, "result": {"message_id": 42}}
        self.error = error

    def send_message(self, text):
        self.messages.append(text)
        if self.error:
            raise self.error
        return self.result


def test_config(webhook_path="/api/messages"):
    return GatewayConfig(
        api_auth_token="secret",
        telegram_bot_token="bot-token",
        telegram_chat_id="@channel",
        webhook_path=webhook_path,
    )


class TelegramGatewayTest(unittest.TestCase):
    def make_client(self, fake=None, webhook_path="/api/messages"):
        fake = fake or FakeTelegramClient()
        app = create_app(test_config(webhook_path), fake)
        return app.test_client(), fake

    def test_health_is_public(self):
        client, _fake = self.make_client()

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_missing_auth_is_rejected(self):
        client, fake = self.make_client()

        response = client.post("/api/messages", json={"text": "hello"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(fake.messages, [])

    def test_bearer_auth_sends_text_field(self):
        client, fake = self.make_client()

        response = client.post(
            "/api/messages",
            headers={"Authorization": "Bearer secret"},
            json={"text": "deployment finished"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["telegram_message_id"], 42)
        self.assertEqual(fake.messages, ["deployment finished"])

    def test_api_key_auth_sends_plain_text_body(self):
        client, fake = self.make_client()

        response = client.post(
            "/api/messages",
            headers={"X-API-Key": "secret", "Content-Type": "text/plain"},
            data="plain message",
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["plain message"])

    def test_get_with_token_sends_quick_message(self):
        client, fake = self.make_client()

        response = client.get("/api/messages?token=secret&message=quick%20hello")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["quick hello"])

    def test_get_converts_escaped_newlines(self):
        client, fake = self.make_client()

        response = client.get("/api/messages?token=secret&message=first\\n\\nsecond")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["first\n\nsecond"])

    def test_get_accepts_url_encoded_newlines(self):
        client, fake = self.make_client()

        response = client.get("/api/messages?token=secret&message=first%0A%0Asecond")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["first\n\nsecond"])

    def test_get_with_bad_token_is_rejected(self):
        client, fake = self.make_client()

        response = client.get("/api/messages?token=bad&message=quick%20hello")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(fake.messages, [])

    def test_get_requires_message(self):
        client, fake = self.make_client()

        response = client.get("/api/messages?token=secret")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "message query parameter is required")
        self.assertEqual(fake.messages, [])

    def test_webhook_path_is_registered(self):
        client, fake = self.make_client(webhook_path="/webhook/alerts")

        response = client.post(
            "/webhook/alerts",
            headers={"Authorization": "Bearer secret"},
            json={"message": "alert fired"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["alert fired"])

    def test_get_webhook_path_is_registered(self):
        client, fake = self.make_client(webhook_path="/webhook/alerts")

        response = client.get("/webhook/alerts?token=secret&message=quick%20alert")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["quick alert"])

    def test_payload_without_message_field_is_formatted(self):
        client, fake = self.make_client()

        response = client.post(
            "/api/messages",
            headers={"Authorization": "Bearer secret"},
            json={"service": "api", "status": "ok"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertIn('"service": "api"', fake.messages[0])
        self.assertIn('"status": "ok"', fake.messages[0])

    def test_message_truncates_to_limit(self):
        fake = FakeTelegramClient()
        app = create_app(
            GatewayConfig(
                api_auth_token="secret",
                telegram_bot_token="bot-token",
                telegram_chat_id="@channel",
                max_message_chars=5,
            ),
            fake,
        )
        client = app.test_client()

        response = client.post(
            "/api/messages",
            headers={"Authorization": "Bearer secret"},
            json={"text": "1234567890"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(fake.messages, ["12345"])

    def test_telegram_error_returns_bad_gateway(self):
        fake = FakeTelegramClient(error=TelegramError(400, {"ok": False, "description": "bad chat"}))
        client, _fake = self.make_client(fake=fake)

        response = client.post(
            "/api/messages",
            headers={"Authorization": "Bearer secret"},
            json={"text": "hello"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "telegram_send_failed")


if __name__ == "__main__":
    unittest.main()
