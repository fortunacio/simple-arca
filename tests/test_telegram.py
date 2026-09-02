from datetime import datetime, timezone
from unittest import TestCase

from contador_ai.bot_reply import BotReply
from contador_ai.notifications import NotificationBatch, NotificationDelivery
from contador_ai.telegram import TelegramClient


class FakeDatabase:
    def __init__(self):
        self.state = {}
        self.portal_notifications = []
        self.portal_notified = []
        self.connections = []
        self.identities = {}

    def get_bot_state(self, key, default=None):
        return self.state.get(key, default)

    def set_bot_state(self, key, value):
        self.state[key] = value

    def pending_portal_access_notifications(self, allowed_chat_ids):
        return [
            row for row in self.portal_notifications if row["chat_id"] in allowed_chat_ids
        ]

    def mark_portal_access_notified(self, chat_id, notified_at):
        self.portal_notified.append((chat_id, notified_at))

    def get_connections(self, _chat_id):
        now = datetime.now(timezone.utc).isoformat()
        return [
            {
                "last_success_at": now,
                "last_error": None,
                "updated_at": now,
                **connection,
            }
            for connection in self.connections
        ]

    def get_live_snapshot(self, _chat_id, _provider, _kind):
        now = datetime.now(timezone.utc).isoformat()
        if any(
            connection.get("provider") == _provider
            and connection.get("status") in {"CONNECTED", "PARTIAL"}
            for connection in self.connections
        ):
            return {
                "payload_json": "{}",
                "fetched_at": now,
                "expires_at": now,
            }
        return None

    def get_tax_identity(self, chat_id):
        return self.identities.get(chat_id)


class FakeService:
    def __init__(self):
        self.dispatched = []
        self.navigated = []
        self.live_synchronizers = {"SANTA_FE_IIBB": lambda _chat_id: None}

    def navigation(self, action, chat_id, replace=False):
        self.navigated.append((action, chat_id, replace))
        return BotReply(
            f"pantalla {action}",
            ((("Volver", "tax:summary"),),),
            replace,
        )

    def dispatch(self, text, chat_id):
        self.dispatched.append((text, chat_id))
        if text == "/configurar_arca":
            return (
                "Configuración de ARCA\n\n"
                "http://localhost:8090/arca/setup/token-seguro"
            )
        if text.startswith("/conectar"):
            return (
                "Listo, verifiqué tu CUIT.\n\n"
                "http://localhost:8090/arca/setup/token-conexion"
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


class FailingMenuTelegramClient(FakeTelegramClient):
    def request(self, method, payload):
        if method == "setMyCommands":
            raise RuntimeError("Telegram no respondió")
        return super().request(method, payload)


class FakeMonitor:
    def __init__(self):
        self.marked = []

    def check_chat(self, chat_id):
        return NotificationBatch(
            "Resumen tributario\n\nAlgo para revisar",
            (NotificationDelivery("event", "value"),),
        )

    def mark_sent(self, chat_id, batch):
        self.marked.append((chat_id, batch))


class FakePaymentStore:
    def __init__(self):
        self.notified = []

    def mark_notified(self, row_id):
        self.notified.append(row_id)


class FakePaymentBot:
    def __init__(self):
        self.callbacks = []
        self.store = FakePaymentStore()

    def handle_callback(self, data, chat_id):
        self.callbacks.append((data, chat_id))
        return BotReply("preparando pago", replace_message=True)

    def pending_replies(self):
        return [(123, "payment-1", BotReply("VEP listo"))]


class TelegramAccessTest(TestCase):
    def test_replaces_the_old_private_menu_with_a_chat_scoped_menu(self):
        client = FakeTelegramClient([], {123})

        client.set_commands()

        method, payload = client.sent[0]
        self.assertEqual(method, "deleteMyCommands")
        self.assertEqual(
            __import__("json").loads(payload["scope"]),
            {"type": "all_private_chats"},
        )
        method, payload = client.sent[1]
        self.assertEqual(method, "setMyCommands")
        commands = __import__("json").loads(payload["commands"])
        self.assertEqual(
            [item["command"] for item in commands],
            ["resumen", "estado", "conectar", "ayuda"],
        )
        self.assertEqual(
            __import__("json").loads(payload["scope"]),
            {"type": "chat", "chat_id": 123},
        )

    def test_sends_and_marks_an_automatic_summary(self):
        client = FakeTelegramClient([], {123})
        monitor = FakeMonitor()
        client.notification_monitor = monitor

        self.assertEqual(client.run_monitor_once(), 1)

        self.assertEqual(client.sent[0][0], "sendMessage")
        self.assertIn("Resumen tributario", client.sent[0][1]["text"])
        self.assertEqual(monitor.marked[0][0], 123)

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
                    "message": {"chat": {"id": 123}, "text": "/resumen"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)
        self.assertEqual(client.fake_service.navigated, [("summary", 123, False)])
        self.assertEqual(client.sent[0][0], "sendMessage")

    def test_estado_uses_the_status_navigation(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 31,
                    "message": {"chat": {"id": 123}, "text": "/estado"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)
        self.assertEqual(client.fake_service.navigated, [("status", 123, False)])

    def test_tax_callback_edits_the_existing_message(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 35,
                    "callback_query": {
                        "id": "callback",
                        "data": "tax:payments",
                        "message": {"message_id": 7, "chat": {"id": 123}},
                    },
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)
        self.assertEqual(client.fake_service.navigated, [("payments", 123, True)])
        self.assertEqual([method for method, _payload in client.sent], [
            "answerCallbackQuery",
            "editMessageText",
        ])

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

    def test_connect_sends_the_setup_as_part_of_the_same_flow(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 32,
                    "message": {
                        "chat": {"id": 123},
                        "text": "/conectar 20383717419",
                    },
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)
        payload = client.sent[0][1]
        button = __import__("json").loads(payload["reply_markup"])["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Completar conexión")
        self.assertEqual(
            button["url"],
            "http://localhost:8090/arca/setup/token-conexion",
        )
        self.assertEqual([method for method, _payload in client.sent], [
            "sendMessage",
            "setMyCommands",
        ])

    def test_disconnect_refreshes_the_chat_menu_after_replying(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 33,
                    "message": {"chat": {"id": 123}, "text": "/desconectar"},
                }
            ],
            {123},
        )

        self.assertEqual(client.poll_once(timeout=0), 1)

        self.assertEqual([method for method, _payload in client.sent], [
            "sendMessage",
            "setMyCommands",
        ])

    def test_menu_refresh_failure_never_blocks_the_user_reply(self):
        client = FailingMenuTelegramClient(
            [
                {
                    "update_id": 34,
                    "message": {
                        "chat": {"id": 123},
                        "text": "/conectar 20383717419",
                    },
                }
            ],
            {123},
        )

        with self.assertLogs("contador_ai.telegram", level="ERROR"):
            self.assertEqual(client.poll_once(timeout=0), 1)

        self.assertEqual(client.sent[0][0], "sendMessage")

    def test_notifies_when_the_initial_account_sync_is_ready(self):
        client = FakeTelegramClient([], {123})
        client.fake_db.portal_notifications = [{"chat_id": 123}]
        client.fake_db.connections = [
            {"provider": "SANTA_FE_IIBB", "status": "CONNECTED"}
        ]

        self.assertEqual(client.run_portal_access_notifications(), 1)

        self.assertIn("Tu acceso quedó guardado", client.sent[0][1]["text"])
        self.assertEqual(client.fake_db.portal_notified[0][0], 123)
        self.assertEqual(client.sent[1][0], "setMyCommands")

    def test_reports_external_iibb_outage_without_blaming_the_login(self):
        client = FakeTelegramClient([], {123})
        client.fake_db.portal_notifications = [{"chat_id": 123}]
        client.fake_db.connections = [
            {"provider": "SANTA_FE_IIBB", "status": "EXTERNAL_UNAVAILABLE"}
        ]

        self.assertEqual(client.run_portal_access_notifications(), 1)

        message = client.sent[0][1]["text"]
        self.assertIn("no está disponible en el organismo", message)
        self.assertNotIn("clave", message)

    def test_reports_a_partial_initial_sync_as_a_terminal_result(self):
        client = FakeTelegramClient([], {123})
        client.fake_db.portal_notifications = [{"chat_id": 123}]
        client.fake_db.connections = [
            {"provider": "SANTA_FE_IIBB", "status": "PARTIAL"}
        ]

        self.assertEqual(client.run_portal_access_notifications(), 1)

        message = client.sent[0][1]["text"]
        self.assertIn("Tu acceso quedó guardado", message)
        self.assertIn("se actualizó parcialmente", message)

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

    def test_iibb_payment_callback_is_not_sent_to_invoice_handler(self):
        client = FakeTelegramClient(
            [
                {
                    "update_id": 41,
                    "callback_query": {
                        "id": "callback",
                        "data": "iibbpay:start",
                        "message": {"message_id": 8, "chat": {"id": 123}},
                    },
                }
            ],
            {123},
        )
        payment_bot = FakePaymentBot()
        client.iibb_payment_bot = payment_bot

        self.assertEqual(client.poll_once(timeout=0), 1)
        self.assertEqual(payment_bot.callbacks, [("iibbpay:start", 123)])
        self.assertEqual([method for method, _ in client.sent], [
            "answerCallbackQuery",
            "editMessageText",
        ])

    def test_marks_payment_notification_only_after_delivery(self):
        client = FakeTelegramClient([], {123})
        payment_bot = FakePaymentBot()
        client.iibb_payment_bot = payment_bot

        self.assertEqual(client.run_iibb_payment_notifications(), 1)
        self.assertEqual(payment_bot.store.notified, ["payment-1"])
        self.assertEqual(client.sent[0][1]["text"], "VEP listo")
