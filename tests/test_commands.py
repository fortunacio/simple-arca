from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from contador_ai.commands import (
    COMMAND_GROUPS,
    COMMAND_SPECS,
    INVOICE_COMMAND_NAMES,
    SERVICE_COMMAND_NAMES,
    CommandCatalog,
)
from contador_ai.db import Database


ALL_SOURCES = {
    "ARCA_PROFILE",
    "ARCA_DFE",
    "ARCA_CCMA",
    "ARCA_SETI",
    "ARCA_INVOICES",
    "SANTA_FE_IIBB",
}


class CommandCatalogTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.db = Database(Path(self.tempdir.name) / "commands.sqlite3")
        self.db.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def _verify(self, chat_id=1001):
        self.db.ensure_user(chat_id)
        self.db.set_pending_tax_identity(chat_id, "20383717419", "production")
        self.db.mark_tax_identity_verified(chat_id)

    def test_catalog_matches_every_command_handler_without_duplicates(self):
        names = [spec.name for spec in COMMAND_SPECS]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), SERVICE_COMMAND_NAMES | INVOICE_COMMAND_NAMES)
        self.assertEqual(
            next(spec for spec in COMMAND_SPECS if spec.name == "monotributo").aliases,
            ("deuda_arca",),
        )
        self.assertEqual(
            next(spec for spec in COMMAND_SPECS if spec.name == "ayuda").aliases,
            ("help",),
        )

    def test_disconnected_chat_gets_only_safe_entry_commands(self):
        catalog = CommandCatalog(
            self.db,
            enabled_sources=ALL_SOURCES,
            invoice_module_enabled=True,
            invoice_emission_chat_ids={1001},
            arca_enrollment_enabled=True,
        )

        self.assertEqual(
            [name for name, _description in catalog.menu_commands(1001)],
            ["resumen", "estado", "conectar", "ayuda"],
        )

        self.db.ensure_user(1001)
        self.db.set_pending_tax_identity(1001, "20383717419", "production")
        self.assertEqual(
            [name for name, _description in catalog.menu_commands(1001)],
            ["resumen", "estado", "conectar", "ayuda"],
        )

    def test_connected_chat_gets_the_complete_task_order(self):
        self._verify()
        catalog = CommandCatalog(
            self.db,
            enabled_sources=ALL_SOURCES,
            invoice_module_enabled=True,
            invoice_emission_chat_ids={1001},
            arca_enrollment_enabled=True,
        )

        menu = catalog.menu_commands(1001)

        self.assertEqual(
            [name for name, _description in menu],
            [
                "resumen",
                "estado",
                "vencimientos",
                "monotributo",
                "iibb",
                "pagos_arca",
                "categoria",
                "facturacion",
                "bandeja",
                "facturas",
                "nueva_factura",
                "nuevo_cliente",
                "configurar_arca",
                "desconectar",
                "ayuda",
            ],
        )
        self.assertTrue(all(" · " in description for _name, description in menu))

    def test_menu_hides_unconfigured_sources_and_invoice_capabilities(self):
        self._verify()
        catalog = CommandCatalog(self.db)

        self.assertEqual(
            [name for name, _description in catalog.menu_commands(1001)],
            ["resumen", "estado", "desconectar", "ayuda"],
        )

    def test_invoice_listing_and_emission_have_separate_capabilities(self):
        self._verify()
        unauthorized = CommandCatalog(self.db, invoice_module_enabled=True)
        authorized = CommandCatalog(
            self.db,
            invoice_module_enabled=True,
            invoice_emission_chat_ids={1001},
        )

        self.assertIn("facturas", dict(unauthorized.menu_commands(1001)))
        self.assertNotIn("nueva_factura", dict(unauthorized.menu_commands(1001)))
        self.assertIn("nueva_factura", dict(authorized.menu_commands(1001)))

    def test_help_lists_every_command_once_in_group_order_with_reasons(self):
        self._verify()
        catalog = CommandCatalog(self.db)

        help_text = catalog.render_help(1001)
        command_lines = [
            line.split(" — ", 1)[0]
            for line in help_text.splitlines()
            if line.startswith("/")
        ]

        self.assertEqual(command_lines, [f"/{spec.name}" for spec in COMMAND_SPECS])
        group_positions = [help_text.index(group.title) for group in COMMAND_GROUPS]
        self.assertEqual(group_positions, sorted(group_positions))
        self.assertIn("/iibb —", help_text)
        self.assertIn("fuente no está configurada", help_text)
        self.assertIn("módulo de comprobantes no está configurado", help_text)
        self.assertIn("/help — alias compatible de /ayuda", help_text)
        self.assertIn("/deuda_arca — alias compatible de /monotributo", help_text)
        self.assertIn("/cancelar —", help_text)

    def test_cancel_is_available_only_during_an_invoice_flow(self):
        self._verify()
        active = False
        catalog = CommandCatalog(
            self.db,
            invoice_module_enabled=True,
            invoice_flow_active=lambda _chat_id: active,
        )

        self.assertFalse(catalog.is_available("cancelar", 1001))
        self.assertIn(
            "disponible sólo durante una carga de factura",
            catalog.render_help(1001),
        )
        active = True
        self.assertTrue(catalog.is_available("cancelar", 1001))
