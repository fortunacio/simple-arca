from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from contador_ai.monotributo import (
    ArcaCategoryLimitsProvider,
    CATEGORY_LIMITS_CACHE_KEY,
    CATEGORY_LIMITS_LAST_ATTEMPT_KEY,
    CategoryLimitsError,
    expected_limits_effective_from,
    next_recategorization,
    parse_arca_category_limits,
    projected_category,
    projection_window,
)


OFFICIAL_AMOUNTS = (
    "12.009.410,45",
    "17.595.182,74",
    "24.670.494,31",
    "30.628.651,43",
    "36.028.231,33",
    "45.151.659,41",
    "53.995.798,87",
    "81.924.660,37",
    "91.699.761,90",
    "105.012.519,20",
    "126.610.838,75",
)


def arca_html(effective_from: str = "1/08/2026", amounts=OFFICIAL_AMOUNTS) -> str:
    rows = "".join(
        f'<tr><th data-title="Categoría" scope="row">{category}</th>'
        f'<td data-title="Ingresos brutos (*)">${amount}</td>'
        f'<td data-title="Otro dato">otro dato</td></tr>'
        for category, amount in zip("ABCDEFGHIJK", amounts)
    )
    return (
        "<html><body>"
        f"<h3><span>Valores de aplicación desde el {effective_from}</span></h3>"
        f"<table><tbody>{rows}</tbody></table>"
        "</body></html>"
    )


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get_bot_state(self, key, default=None):
        return self.values.get(key, default)

    def set_bot_state(self, key, value):
        self.values[key] = value


class MonotributoProjectionTest(TestCase):
    def test_parses_current_official_table(self):
        verified_at = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        limits = parse_arca_category_limits(arca_html(), verified_at=verified_at)

        self.assertEqual(limits.effective_from, date(2026, 8, 1))
        self.assertEqual(limits.verified_at, verified_at)
        self.assertEqual(limits.limits[0], ("A", Decimal("12009410.45")))
        self.assertEqual(limits.limits[-1], ("K", Decimal("126610838.75")))
        self.assertEqual(projected_category(Decimal("36000000"), limits), "E")
        self.assertEqual(projected_category(Decimal("36028231.34"), limits), "F")

    def test_rejects_an_incomplete_or_non_increasing_table(self):
        with self.assertRaisesRegex(CategoryLimitsError, "todas las categorías"):
            parse_arca_category_limits(arca_html(amounts=OFFICIAL_AMOUNTS[:-1]))

        invalid = list(OFFICIAL_AMOUNTS)
        invalid[5] = invalid[4]
        with self.assertRaisesRegex(CategoryLimitsError, "positivos y crecientes"):
            parse_arca_category_limits(arca_html(amounts=tuple(invalid)))

    def test_provider_downloads_and_persists_verified_table(self):
        cache = MemoryCache()
        now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        provider = ArcaCategoryLimitsProvider(
            cache,
            fetch_html=lambda _url: arca_html(),
            now=lambda: now,
        )

        result = provider.current(date(2026, 8, 5))

        self.assertEqual(result.effective_from, date(2026, 8, 1))
        self.assertFalse(result.verification_pending)
        self.assertIn(CATEGORY_LIMITS_CACHE_KEY, cache.values)
        self.assertIn(CATEGORY_LIMITS_LAST_ATTEMPT_KEY, cache.values)

    def test_provider_uses_current_cache_and_marks_failed_refresh(self):
        cache = MemoryCache()
        first_check = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        ArcaCategoryLimitsProvider(
            cache,
            fetch_html=lambda _url: arca_html(),
            now=lambda: first_check,
        ).current(date(2026, 8, 5))

        def fail(_url):
            raise CategoryLimitsError("ARCA no disponible")

        result = ArcaCategoryLimitsProvider(
            cache,
            fetch_html=fail,
            now=lambda: first_check + timedelta(days=2),
        ).current(date(2026, 8, 7))

        self.assertTrue(result.verification_pending)
        self.assertEqual(result.effective_from, date(2026, 8, 1))

    def test_provider_never_uses_a_previous_semester(self):
        provider = ArcaCategoryLimitsProvider(
            MemoryCache(),
            fetch_html=lambda _url: arca_html("1/02/2026"),
            now=lambda: datetime(2026, 8, 5, tzinfo=timezone.utc),
        )

        with self.assertRaisesRegex(CategoryLimitsError, "semestre vigente"):
            provider.current(date(2026, 8, 5))

    def test_provider_does_not_retry_a_failed_refresh_on_every_alert_check(self):
        cache = MemoryCache()
        first_check = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        ArcaCategoryLimitsProvider(
            cache,
            fetch_html=lambda _url: arca_html(),
            now=lambda: first_check,
        ).current(date(2026, 8, 5))
        attempts = 0

        def fail(_url):
            nonlocal attempts
            attempts += 1
            raise CategoryLimitsError("ARCA no disponible")

        failed_at = first_check + timedelta(days=2)
        provider = ArcaCategoryLimitsProvider(
            cache,
            fetch_html=fail,
            now=lambda: failed_at,
        )
        self.assertTrue(provider.current(date(2026, 8, 7)).verification_pending)
        self.assertTrue(provider.current(date(2026, 8, 7)).verification_pending)
        self.assertEqual(attempts, 1)

    def test_expected_table_changes_in_february_and_august(self):
        self.assertEqual(expected_limits_effective_from(date(2026, 1, 31)), date(2025, 8, 1))
        self.assertEqual(expected_limits_effective_from(date(2026, 2, 1)), date(2026, 2, 1))
        self.assertEqual(expected_limits_effective_from(date(2026, 8, 1)), date(2026, 8, 1))

    def test_recategorization_uses_the_official_semester_cutoffs(self):
        self.assertEqual(
            projection_window(date(2026, 8, 5)),
            (date(2025, 7, 1), date(2026, 6, 30)),
        )
        recategorization = next_recategorization(date(2026, 8, 5))
        self.assertEqual(recategorization, date(2027, 2, 5))
        self.assertEqual(
            projection_window(recategorization),
            (date(2026, 1, 1), date(2026, 12, 31)),
        )
