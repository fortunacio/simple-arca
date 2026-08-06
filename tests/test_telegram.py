from unittest import TestCase

from contador_ai.telegram import TelegramClient


class FakeDatabase:
    def __init__(self):
        self.state = {}

    def get_bot_state(self, key, default=None):
        return self.state.get(key, default)

    def set_bot_state(self, key, value):
        self.state[key] = value


class FakeService:
    def __init__(self):
        self.dispatched = []

    def dispatch(self, text, chat_id):
        self.dispatched.append((text, chat_id))
        if text == "/configurar_arca":
            return (
                "Configuración de ARCA\n\n"
                "http://localhost:8090/arca/setup/token-seguro"
            )
        return "respuesta"


class FakeTelegramClient(TelegramClient):
    def __init__(self, updates, allowed_chat_ids):
        self.updates = updates
        self.sent = []
        self.fake_db = FakeDatabase()
        self.fake_service = FakeService()
        super().__init__(
            "test-token",
            self.fake_db,
            self.fake_service,
            allowed_chat_ids=frozenset(allowed_chat_ids),
        )

    def request(self, method, payload):
        if method == "getUpdates":
            return self.updates
        self.sent.append((method, payload))
        return {}


class TelegramAccessTest(TestCase):
    def test_unauthorized_private_chat_is_silently_ignored(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 10,
                    "message": {"chat": {"id": 999}, "text": "/estado"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 0)
        self.assertEqual(client.fake_service.dispatched, [])
        self.assertEqual(client.sent, [])
        self.assertEqual(client.fake_db.state["telegram_offset"], "11")

    def test_group_chat_is_ignored(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 20,
                    "message": {"chat": {"id": -100123}, "text": "/start"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 0)
        self.assertEqual(client.fake_service.dispatched, [])

    def test_allowed_chat_is_processed(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 30,
                    "message": {"chat": {"id": 123}, "text": "/estado"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)
        self.assertEqual(client.fake_service.dispatched, [("/estado", 123)])
        self.assertEqual(client.sent[0][0], "sendMessage")

    def test_arca_setup_is_sent_as_a_clickable_url_button(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 31,
                    "message": {"chat": {"id": 123}, "text": "/configurar_arca"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)
        payload = client.sent[0][1]
        markup = __import__("json").loads(payload["reply_markup"])
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Abrir configuración de ARCA")
        self.assertEqual(
            button["url"],
            "http://localhost:8090/arca/setup/token-seguro",
        )
        self.assertNotIn("callback_data", button)

    def test_unauthorized_callback_is_silently_ignored(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 40,
                    "callback_query": {
                        "id": "callback",
                        "data": "invoice:list:0",
                        "message": {"message_id": 1, "chat": {"id": 999}},
                    },
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 0)
        self.assertEqual(client.sent, [])
