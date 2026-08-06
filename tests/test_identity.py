from unittest import TestCase

from contador_ai.identity import is_valid_cuit, mask_cuit, normalize_cuit


class CuitTest(TestCase):
    def test_normalize_and_validate(self):
        self.assertEqual(normalize_cuit("20-12345678-6"), "20123456786")
        self.assertTrue(is_valid_cuit("20-12345678-6"))
        self.assertFalse(is_valid_cuit("20-12345678-9"))
        self.assertFalse(is_valid_cuit("11-11111111-1"))

    def test_mask(self):
        self.assertEqual(mask_cuit("20123456786"), "20-******78-6")
