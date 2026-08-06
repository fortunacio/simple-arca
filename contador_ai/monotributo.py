from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Callable, Protocol

import httpx


CATEGORY_LIMITS_SOURCE = "https://www.arca.gob.ar/monotributo/categorias.asp"
CATEGORY_LIMITS_CACHE_KEY = "monotributo_category_limits_v1"
CATEGORIES = tuple("ABCDEFGHIJK")


class CategoryLimitsError(RuntimeError):
    pass


class StateCache(Protocol):
    def get_bot_state(self, key: str, default: str | None = None) -> str | None: ...

    def set_bot_state(self, key: str, value: str) -> None: ...


@dataclass(frozen=True)
class CategoryLimits:
    effective_from: date
    limits: tuple[tuple[str, Decimal], ...]
    verified_at: datetime
    source: str = CATEGORY_LIMITS_SOURCE
    verification_pending: bool = False

    def __post_init__(self) -> None:
        categories = tuple(category for category, _limit in self.limits)
        if categories != CATEGORIES:
            raise CategoryLimitsError("ARCA no devolvió todas las categorías de A a K")
        values = tuple(limit for _category, limit in self.limits)
        if any(value <= 0 for value in values) or any(
            current >= following for current, following in zip(values, values[1:])
        ):
            raise CategoryLimitsError("Los topes de ARCA no son positivos y crecientes")

    def to_json(self) -> str:
        return json.dumps(
            {
                "effective_from": self.effective_from.isoformat(),
                "limits": [[category, str(limit)] for category, limit in self.limits],
                "verified_at": self.verified_at.isoformat(),
                "source": self.source,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str) -> CategoryLimits:
        try:
            data = json.loads(payload)
            verified_at = datetime.fromisoformat(data["verified_at"])
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=timezone.utc)
            return cls(
                effective_from=date.fromisoformat(data["effective_from"]),
                limits=tuple(
                    (str(category), Decimal(str(limit)))
                    for category, limit in data["limits"]
                ),
                verified_at=verified_at,
                source=str(data["source"]),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
            raise CategoryLimitsError("La tabla de topes guardada es inválida") from exc


class _ArcaCategoryTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.rows: list[list[tuple[str, str]]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell: list[str] | None = None
        self._cell_title = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []
            self._cell_title = dict(attrs).get("data-title", "")

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._row is not None and self._cell is not None:
            self._row.append(
                (" ".join("".join(self._cell).split()), self._cell_title)
            )
            self._cell = None
            self._cell_title = ""
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None


def _parse_argentine_money(value: str) -> Decimal:
    normalized = re.sub(r"[^0-9,.]", "", value).replace(".", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise CategoryLimitsError(f"ARCA devolvió un importe inválido: {value!r}") from exc
    if amount.as_tuple().exponent < -2:
        raise CategoryLimitsError(f"ARCA devolvió un importe con decimales inválidos: {value!r}")
    return amount


def parse_arca_category_limits(
    html: str,
    *,
    verified_at: datetime | None = None,
) -> CategoryLimits:
    parser = _ArcaCategoryTableParser()
    parser.feed(html)
    visible_text = " ".join(" ".join(parser.text).split())
    date_match = re.search(
        r"Valores de aplicaci[oó]n desde el (\d{1,2})/(\d{1,2})/(\d{4})",
        visible_text,
        flags=re.IGNORECASE,
    )
    if date_match is None:
        raise CategoryLimitsError("No encontré la fecha de vigencia en la página de ARCA")
    day, month, year = (int(part) for part in date_match.groups())
    try:
        effective_from = date(year, month, day)
    except ValueError as exc:
        raise CategoryLimitsError("ARCA devolvió una fecha de vigencia inválida") from exc

    parsed: dict[str, Decimal] = {}
    for row in parser.rows:
        category_cells = [
            text
            for text, title in row
            if "categoría" in title.lower() or "categoria" in title.lower()
        ]
        income_cells = [
            text for text, title in row if "ingresos brutos" in title.lower()
        ]
        if len(category_cells) != 1 or len(income_cells) != 1:
            continue
        category = category_cells[0].strip().upper()
        if category not in CATEGORIES:
            continue
        if category in parsed:
            raise CategoryLimitsError(f"ARCA devolvió la categoría {category} más de una vez")
        parsed[category] = _parse_argentine_money(income_cells[0])

    timestamp = verified_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return CategoryLimits(
        effective_from=effective_from,
        limits=tuple((category, parsed[category]) for category in CATEGORIES if category in parsed),
        verified_at=timestamp,
    )


def expected_limits_effective_from(reference: date) -> date:
    if reference.month >= 8:
        return date(reference.year, 8, 1)
    if reference.month >= 2:
        return date(reference.year, 2, 1)
    return date(reference.year - 1, 8, 1)


def _fetch_arca_category_limits(url: str) -> str:
    try:
        response = httpx.get(
            url,
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Contador-AI/1.0 (+monitor tributario personal)"},
        )
        response.raise_for_status()
        return response.text
    except httpx.HTTPError as exc:
        raise CategoryLimitsError("No pude consultar los topes de monotributo en ARCA") from exc


class ArcaCategoryLimitsProvider:
    def __init__(
        self,
        cache: StateCache,
        *,
        fetch_html: Callable[[str], str] = _fetch_arca_category_limits,
        now: Callable[[], datetime] | None = None,
        refresh_after: timedelta = timedelta(days=1),
    ):
        self.cache = cache
        self.fetch_html = fetch_html
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.refresh_after = refresh_after

    def current(self, reference: date) -> CategoryLimits:
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        expected = expected_limits_effective_from(reference)
        cached = self._load_cached()
        if (
            cached is not None
            and cached.effective_from == expected
            and cached.verified_at <= now
            and now - cached.verified_at <= self.refresh_after
        ):
            return cached

        try:
            downloaded = parse_arca_category_limits(
                self.fetch_html(CATEGORY_LIMITS_SOURCE),
                verified_at=now,
            )
            if downloaded.effective_from != expected:
                raise CategoryLimitsError(
                    "ARCA todavía no publicó topes correspondientes al semestre vigente"
                )
        except CategoryLimitsError:
            if cached is not None and cached.effective_from == expected:
                return replace(cached, verification_pending=True)
            raise

        self.cache.set_bot_state(CATEGORY_LIMITS_CACHE_KEY, downloaded.to_json())
        return downloaded

    def _load_cached(self) -> CategoryLimits | None:
        payload = self.cache.get_bot_state(CATEGORY_LIMITS_CACHE_KEY)
        if not payload:
            return None
        try:
            cached = CategoryLimits.from_json(payload)
        except CategoryLimitsError:
            return None
        if cached.source != CATEGORY_LIMITS_SOURCE:
            return None
        return cached


def projected_category(annual_revenue: Decimal, category_limits: CategoryLimits) -> str | None:
    for category, limit in category_limits.limits:
        if annual_revenue <= limit:
            return category
    return None


def month_start(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def last_complete_month(reference: date) -> tuple[date, date]:
    end = month_start(reference) - timedelta(days=1)
    start = add_months(month_start(reference), -12)
    return start, end


def next_recategorization(reference: date) -> date:
    february = date(reference.year, 2, 5)
    august = date(reference.year, 8, 5)
    if reference < february:
        return february
    if reference < august:
        return august
    return date(reference.year + 1, 2, 5)


def projection_window(recategorization: date) -> tuple[date, date]:
    end = add_months(month_start(recategorization), -1) - timedelta(days=1)
    start = add_months(month_start(recategorization), -13)
    return start, end


def completed_months_between(start: date, end: date) -> int:
    if end < start:
        return 0
    return (end.year - start.year) * 12 + end.month - start.month + 1
