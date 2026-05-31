from __future__ import annotations

import unittest

from app import (
    GatewayConfig,
    RecordStore,
    TelegramError,
    _format_duration,
    _grafana_key,
    _grafana_text,
    _is_not_modified,
    _is_uneditable,
    _strikethrough_html,
    create_app,
)


def not_modified_error() -> TelegramError:
    return TelegramError(400, {"ok": False, "description": "Bad Request: message is not modified"})


def uneditable_error() -> TelegramError:
    return TelegramError(400, {"ok": False, "description": "Bad Request: message can't be edited"})


class FakeTelegramClient:
    """Records sends/edits and mimics Telegram's edit semantics.

    Editing a message to the text it already shows raises the real
    "message is not modified" error, so the no-op path is exercised honestly.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.sent_reply_to: list[int | None] = []
        self.edited: list[tuple[int, str]] = []
        self.edit_parse_modes: list[str | None] = []
        self.send_error: Exception | None = None
        self.edit_error: Exception | None = None
        self._next_id = 100
        self._current: dict[int, str] = {}

    def send_message(self, text: str, reply_to_message_id: int | None = None) -> dict:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(text)
        self.sent_reply_to.append(reply_to_message_id)
        message_id = self._next_id
        self._next_id += 1
        self._current[message_id] = text
        return {"ok": True, "result": {"message_id": message_id}}

    def edit_message(self, message_id: int, text: str, parse_mode: str | None = None) -> dict:
        self.edited.append((message_id, text))
        self.edit_parse_modes.append(parse_mode)
        if self.edit_error is not None:
            raise self.edit_error
        if self._current.get(message_id) == text:
            raise not_modified_error()
        self._current[message_id] = text
        return {"ok": True, "result": {"message_id": message_id}}


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def grafana_config() -> GatewayConfig:
    return GatewayConfig(
        api_auth_token="secret",
        telegram_bot_token="bot-token",
        telegram_chat_id="@channel",
        record_db_path=":memory:",
        record_ttl_seconds=24 * 60 * 60,
        edit_window_seconds=47 * 60 * 60,
    )


def grafana_payload(status="firing", message="devdm down", group_key="g1", **labels):
    common = {"observer": "devhome", "device": "devdm", "service": "vps-network-monitoring"}
    common.update(labels)
    return {
        "status": status,
        "groupKey": group_key,
        "commonLabels": common,
        "message": message,
        "alerts": [{"fingerprint": "fp1", "labels": common}],
    }


class GrafanaEndpointTest(unittest.TestCase):
    def make_client(self, fake=None, clock=None, config=None):
        fake = fake or FakeTelegramClient()
        clock = clock or FakeClock()
        store = RecordStore(":memory:")
        app = create_app(config or grafana_config(), fake, store, clock)
        return app.test_client(), fake, clock, store

    def post(self, client, payload):
        return client.post(
            "/api/grafana",
            headers={"Authorization": "Bearer secret"},
            json=payload,
        )

    def test_requires_auth(self):
        client, fake, _clock, _store = self.make_client()

        response = client.post("/api/grafana", json=grafana_payload())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(fake.sent, [])

    def test_first_alert_sends_and_records(self):
        client, fake, _clock, store = self.make_client()

        response = self.post(client, grafana_payload(message="devdm down"))

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["action"], "sent")
        self.assertEqual(body["telegram_message_id"], 100)
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("devdm down", fake.sent[0])
        self.assertIn("duration: 0s", fake.sent[0])  # timeline appended
        self.assertEqual(fake.edited, [])
        record = store.get("g1")
        self.assertIsNotNone(record)
        self.assertEqual(record.message_id, 100)

    def test_repeat_edits_same_message(self):
        client, fake, _clock, _store = self.make_client()

        self.post(client, grafana_payload(message="devdm down"))
        response = self.post(client, grafana_payload(message="devdm degraded"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "edited")
        self.assertEqual(response.get_json()["telegram_message_id"], 100)
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("devdm down", fake.sent[0])
        self.assertEqual(len(fake.edited), 1)
        self.assertEqual(fake.edited[0][0], 100)
        self.assertIn("devdm degraded", fake.edited[0][1])

    def test_identical_text_is_a_no_op(self):
        client, fake, _clock, _store = self.make_client()

        self.post(client, grafana_payload(message="devdm down"))
        response = self.post(client, grafana_payload(message="devdm down"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "unchanged")
        # One send, one (rejected) edit attempt, no flood. The clock doesn't move
        # between posts, so the rendered text (timeline included) is identical.
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("devdm down", fake.sent[0])
        self.assertEqual(len(fake.edited), 1)
        self.assertEqual(fake.edited[0][0], 100)

    def test_uneditable_message_falls_back_to_send(self):
        client, fake, _clock, store = self.make_client()

        self.post(client, grafana_payload(message="devdm down"))
        fake.edit_error = uneditable_error()
        response = self.post(client, grafana_payload(message="devdm still down"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "sent")
        self.assertEqual(response.get_json()["telegram_message_id"], 101)
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("devdm still down", fake.sent[1])
        self.assertEqual(store.get("g1").message_id, 101)

    def test_expired_edit_window_sends_new(self):
        clock = FakeClock()
        client, fake, _clock, store = self.make_client(clock=clock)

        self.post(client, grafana_payload(message="devdm down"))
        clock.advance(48 * 60 * 60)
        response = self.post(client, grafana_payload(message="devdm down again"))

        self.assertEqual(response.get_json()["action"], "sent")
        self.assertEqual(fake.edited, [])  # never attempted an edit past the window
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("devdm down again", fake.sent[1])
        self.assertEqual(store.get("g1").message_id, 101)

    def test_resolved_sends_notifying_message_and_retires_record(self):
        client, fake, _clock, store = self.make_client()

        self.post(client, grafana_payload(status="firing", message="devdm down"))
        resolved = self.post(client, grafana_payload(status="resolved", message="devdm recovered"))

        # Recovery must push, so it sends a new message rather than silently editing.
        self.assertEqual(resolved.get_json()["action"], "resolved")
        self.assertEqual(resolved.get_json()["telegram_message_id"], 101)
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("devdm recovered", fake.sent[1])
        # The recovery replies to the firing message (tap-to-jump reference).
        self.assertEqual(fake.sent_reply_to, [None, 100])
        # The firing bubble is struck through (silent HTML edit) on the way out.
        self.assertEqual(len(fake.edited), 1)
        self.assertEqual(fake.edited[0][0], 100)
        self.assertTrue(fake.edited[0][1].startswith("<s>"))
        self.assertTrue(fake.edited[0][1].endswith("</s>"))
        self.assertIn("devdm down", fake.edited[0][1])
        self.assertEqual(fake.edit_parse_modes, ["HTML"])
        self.assertIsNone(store.get("g1"))  # record retired

        # A later firing starts a fresh message rather than editing the old bubble.
        refire = self.post(client, grafana_payload(status="firing", message="devdm down"))
        self.assertEqual(refire.get_json()["action"], "sent")
        self.assertEqual(refire.get_json()["telegram_message_id"], 102)

    def test_resolved_strikes_latest_firing_text(self):
        client, fake, _clock, _store = self.make_client()

        self.post(client, grafana_payload(status="firing", message="🟡 devdm degraded"))
        self.post(client, grafana_payload(status="firing", message="🔴 devdm down"))
        self.post(client, grafana_payload(status="resolved", message="🟢 devdm recovered"))

        # Strikes the most recently displayed firing text, not the original.
        self.assertEqual(fake.edited[-1][0], 100)
        self.assertIn("🔴 devdm down", fake.edited[-1][1])
        self.assertNotIn("degraded", fake.edited[-1][1])
        self.assertTrue(fake.edited[-1][1].startswith("<s>"))

    def test_resolved_without_record_just_sends(self):
        client, fake, _clock, _store = self.make_client()

        resolved = self.post(client, grafana_payload(status="resolved", message="devdm recovered"))

        self.assertEqual(resolved.get_json()["action"], "resolved")
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("devdm recovered", fake.sent[0])
        self.assertEqual(fake.sent_reply_to, [None])  # no original to reply to
        self.assertEqual(fake.edited, [])  # nothing to strike

    def test_resolved_push_survives_strike_failure(self):
        client, fake, _clock, store = self.make_client()

        self.post(client, grafana_payload(status="firing", message="devdm down"))
        fake.edit_error = uneditable_error()  # firing bubble can't be struck
        resolved = self.post(client, grafana_payload(status="resolved", message="devdm recovered"))

        # The recovery push still goes out even though the strike failed.
        self.assertEqual(resolved.get_json()["action"], "resolved")
        self.assertEqual(len(fake.sent), 2)
        self.assertIn("devdm recovered", fake.sent[1])
        self.assertIsNone(store.get("g1"))

    def test_escalation_edits_silently(self):
        client, fake, _clock, _store = self.make_client()

        self.post(client, grafana_payload(status="firing", message="🟡 devdm degraded"))
        escalated = self.post(client, grafana_payload(status="firing", message="🔴 devdm down"))

        # Severity change while firing is a silent edit, not a new push.
        self.assertEqual(escalated.get_json()["action"], "edited")
        self.assertEqual(len(fake.sent), 1)
        self.assertIn("🟡 devdm degraded", fake.sent[0])
        self.assertEqual(len(fake.edited), 1)
        self.assertEqual(fake.edited[0][0], 100)
        self.assertIn("🔴 devdm down", fake.edited[0][1])

    def test_duration_ticks_up_across_edits(self):
        clock = FakeClock()
        client, fake, _clock, _store = self.make_client(clock=clock)

        self.post(client, grafana_payload(message="devdm down"))
        self.assertIn("duration: 0s", fake.sent[0])

        clock.advance(125)  # 2m5s -> 2m
        self.post(client, grafana_payload(message="devdm down worse"))
        self.assertIn("duration: 2m", fake.edited[-1][1])

        clock.advance(2 * 60 * 60)  # +2h -> 2h2m total
        self.post(client, grafana_payload(message="devdm down still"))
        self.assertIn("duration: 2h2m", fake.edited[-1][1])

    def test_resolved_shows_total_duration_and_started_anchored(self):
        clock = FakeClock()
        client, fake, _clock, _store = self.make_client(clock=clock)

        self.post(client, grafana_payload(status="firing", message="devdm down"))
        clock.advance(90 * 60)  # 90 minutes -> 1h30m
        self.post(client, grafana_payload(status="resolved", message="devdm recovered"))

        recovery = fake.sent[-1]
        self.assertIn("devdm recovered", recovery)
        self.assertIn("duration: 1h30m", recovery)
        self.assertIn("started:", recovery)
        self.assertIn("updated:", recovery)

    def test_stale_records_are_swept(self):
        clock = FakeClock()
        client, _fake, _clock, store = self.make_client(clock=clock)
        store.save("old", 5, "stale", clock.now, clock.now)

        clock.advance(25 * 60 * 60)  # past the 24h TTL
        self.post(client, grafana_payload(group_key="fresh"))

        self.assertIsNone(store.get("old"))
        self.assertIsNotNone(store.get("fresh"))

    def test_dedicated_template_label_takes_precedence(self):
        client, fake, _clock, _store = self.make_client()

        # `template` label wins even when `service` says something else.
        payload = grafana_payload(message="devdm down", service="something-else")
        payload["commonLabels"]["template"] = "vps-network-monitoring"
        response = self.post(client, payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "sent")

    def test_unsupported_template_is_rejected(self):
        client, fake, _clock, _store = self.make_client()

        response = self.post(client, grafana_payload(message="devdm down", service="disk-usage"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "unsupported template")
        self.assertEqual(fake.sent, [])  # nothing sent for the wrong template

    def test_missing_template_is_rejected(self):
        client, fake, _clock, _store = self.make_client()

        payload = grafana_payload(message="devdm down")
        payload["commonLabels"].pop("service")  # no template/service label at all
        response = self.post(client, payload)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(fake.sent, [])

    def test_rejects_non_object_body(self):
        client, _fake, _clock, _store = self.make_client()

        response = client.post(
            "/api/grafana",
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            data="[]",
        )

        self.assertEqual(response.status_code, 400)

    def test_send_failure_returns_bad_gateway(self):
        fake = FakeTelegramClient()
        fake.send_error = TelegramError(400, {"ok": False, "description": "bad chat"})
        client, _fake, _clock, _store = self.make_client(fake=fake)

        response = self.post(client, grafana_payload())

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "telegram_send_failed")


class EditEndpointTest(unittest.TestCase):
    def make_client(self, fake=None):
        fake = fake or FakeTelegramClient()
        store = RecordStore(":memory:")
        app = create_app(grafana_config(), fake, store, FakeClock())
        return app.test_client(), fake

    def test_patch_edits_message(self):
        client, fake = self.make_client()

        response = client.patch(
            "/api/messages/42",
            headers={"Authorization": "Bearer secret"},
            json={"text": "updated"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "edited")
        self.assertEqual(response.get_json()["telegram_message_id"], 42)
        self.assertEqual(fake.edited, [(42, "updated")])

    def test_patch_not_modified_is_ok(self):
        fake = FakeTelegramClient()
        fake.edit_error = not_modified_error()
        client, _fake = self.make_client(fake=fake)

        response = client.patch(
            "/api/messages/42",
            headers={"Authorization": "Bearer secret"},
            json={"text": "same"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["action"], "unchanged")

    def test_patch_requires_auth(self):
        client, fake = self.make_client()

        response = client.patch("/api/messages/42", json={"text": "x"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(fake.edited, [])

    def test_patch_telegram_error_is_bad_gateway(self):
        fake = FakeTelegramClient()
        fake.edit_error = TelegramError(400, {"ok": False, "description": "chat not found"})
        client, _fake = self.make_client(fake=fake)

        response = client.patch(
            "/api/messages/42",
            headers={"Authorization": "Bearer secret"},
            json={"text": "x"},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"], "telegram_edit_failed")


class RecordStoreTest(unittest.TestCase):
    def test_save_and_get(self):
        store = RecordStore(":memory:")
        store.save("k", 7, "hello", 100.0, 100.0)

        record = store.get("k")
        self.assertEqual(record.message_id, 7)
        self.assertEqual(record.text, "hello")
        self.assertEqual(record.sent_at, 100.0)
        self.assertEqual(record.last_update, 100.0)

    def test_save_overwrites(self):
        store = RecordStore(":memory:")
        store.save("k", 7, "a", 100.0, 100.0)
        store.save("k", 9, "b", 200.0, 200.0)

        self.assertEqual(store.get("k").message_id, 9)

    def test_touch_updates_last_update_and_text_keeping_sent_at(self):
        store = RecordStore(":memory:")
        store.save("k", 7, "old", 100.0, 100.0)
        store.touch("k", 250.0, "new")

        record = store.get("k")
        self.assertEqual(record.sent_at, 100.0)  # unchanged: the 48h edit window anchor
        self.assertEqual(record.last_update, 250.0)
        self.assertEqual(record.text, "new")

    def test_delete(self):
        store = RecordStore(":memory:")
        store.save("k", 7, "x", 100.0, 100.0)
        store.delete("k")

        self.assertIsNone(store.get("k"))

    def test_sweep_removes_only_stale(self):
        store = RecordStore(":memory:")
        store.save("old", 1, "a", 10.0, 10.0)
        store.save("new", 2, "b", 10.0, 100.0)

        removed = store.sweep(older_than=50.0)

        self.assertEqual(removed, 1)
        self.assertIsNone(store.get("old"))
        self.assertIsNotNone(store.get("new"))


class HelperTest(unittest.TestCase):
    def test_key_prefers_group_key(self):
        self.assertEqual(_grafana_key(grafana_payload(group_key="g9")), "g9")

    def test_key_falls_back_to_labels(self):
        payload = grafana_payload()
        del payload["groupKey"]
        self.assertEqual(_grafana_key(payload), "devhome->devdm")

    def test_key_falls_back_to_fingerprint(self):
        payload = grafana_payload()
        del payload["groupKey"]
        payload["commonLabels"] = {}
        self.assertEqual(_grafana_key(payload), "fp1")

    def test_key_returns_none_when_unidentifiable(self):
        self.assertIsNone(_grafana_key({"status": "firing"}))

    def test_text_prefers_message_then_title(self):
        self.assertEqual(_grafana_text({"message": "m", "title": "t"}, 100), "m")
        self.assertEqual(_grafana_text({"title": "t"}, 100), "t")
        self.assertEqual(_grafana_text({}, 100), "")

    def test_not_modified_detection(self):
        self.assertTrue(_is_not_modified({"description": "Bad Request: message is not modified"}))
        self.assertFalse(_is_not_modified({"description": "chat not found"}))

    def test_uneditable_detection(self):
        self.assertTrue(_is_uneditable({"description": "Bad Request: message can't be edited"}))
        self.assertTrue(_is_uneditable({"description": "message to edit not found"}))
        self.assertFalse(_is_uneditable({"description": "message is not modified"}))

    def test_strikethrough_wraps_and_escapes_html(self):
        self.assertEqual(_strikethrough_html("devdm down"), "<s>devdm down</s>")
        self.assertEqual(_strikethrough_html("a<b>&c"), "<s>a&lt;b&gt;&amp;c</s>")

    def test_format_duration(self):
        self.assertEqual(_format_duration(0), "0s")
        self.assertEqual(_format_duration(5), "5s")
        self.assertEqual(_format_duration(59), "59s")
        self.assertEqual(_format_duration(60), "1m")
        self.assertEqual(_format_duration(125), "2m")       # seconds dropped
        self.assertEqual(_format_duration(3599), "59m")
        self.assertEqual(_format_duration(3600), "1h0m")
        self.assertEqual(_format_duration(3661), "1h1m")
        self.assertEqual(_format_duration(7325), "2h2m")
        self.assertEqual(_format_duration(86399), "23h59m")     # just under a day
        self.assertEqual(_format_duration(86400), "1d0h0m")     # exactly a day
        self.assertEqual(_format_duration(90061), "1d1h1m")
        self.assertEqual(_format_duration(183840), "2d3h4m")
        self.assertEqual(_format_duration(-10), "0s")           # clamped


if __name__ == "__main__":
    unittest.main()
