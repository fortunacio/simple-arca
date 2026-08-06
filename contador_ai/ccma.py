from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence


class CcmaError(RuntimeError):
    pass


class CcmaSessionRequired(CcmaError):
    pass


class CcmaCredentialsRejected(CcmaError):
    pass


@dataclass(frozen=True)
class CcmaBalance:
    period: str
    tax: str
    description: str
    due_date: date
    balance: Decimal

    def to_payload(self) -> dict[str, str]:
        return {
            "period": self.period,
            "tax": self.tax,
            "description": self.description,
            "due_date": self.due_date.isoformat(),
            "balance": str(self.balance),
        }


def _money(value: str) -> Decimal:
    raw = value.strip()
    negative = raw.startswith("(") and raw.endswith(")")
    normalized = raw.strip("()").replace(",", "")
    try:
        amount = Decimal(normalized or "0")
    except InvalidOperation as exc:
        raise CcmaError(f"CCMA devolvió un importe inválido: {value!r}") from exc
    return amount if negative else -amount


def _date(value: str) -> date:
    try:
        day, month, year = (int(part) for part in value.split("/"))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise CcmaError(f"CCMA devolvió una fecha inválida: {value!r}") from exc


def _summary_amount(text: str, label: str) -> Decimal:
    match = re.search(rf"{re.escape(label)}\s*([0-9,]+\.\d{{2}})", text, re.IGNORECASE)
    if match is None:
        raise CcmaError(f"CCMA no informó {label.rstrip(':').lower()}")
    return abs(_money(f"({match.group(1)})"))


def _period_iso(value: str) -> str:
    month, year = value.split("/")
    return f"{year}-{month}"


def parse_ccma_account(
    summary_text: str,
    rows: Iterable[Sequence[str]],
) -> dict[str, object]:
    calculation_match = re.search(
        r"FECHA DE C[ÁA]LCULO:\s*(\d{1,2}/\d{1,2}/\d{4})",
        summary_text,
        re.IGNORECASE,
    )
    period_match = re.search(
        r"PER[ÍI]ODO TOTAL CALCULADO\s*(\d{2}/\d{4})\s*-\s*(\d{2}/\d{4})",
        summary_text,
        re.IGNORECASE,
    )
    if calculation_match is None or period_match is None:
        raise CcmaError("CCMA no informó el período y la fecha del cálculo")

    calculated_on = _date(calculation_match.group(1))
    debtor_total = _summary_amount(summary_text, "Total Saldo Deudor:")
    creditor_total = _summary_amount(summary_text, "Total Saldo Acreedor:")
    balances: list[CcmaBalance] = []
    current: tuple[str, str, str, date] | None = None

    for raw_cells in rows:
        cells = [" ".join(str(cell).split()) for cell in raw_cells]
        period_index = next(
            (index for index, cell in enumerate(cells) if re.fullmatch(r"\d{2}/\d{4}", cell)),
            None,
        )
        if period_index is None:
            continue
        period = cells[period_index]
        tail = cells[period_index + 1 :]
        if "Saldo" in tail and current is not None:
            amount_text = next(
                (cell for cell in reversed(tail) if re.fullmatch(r"\(?[0-9,]+\.\d{2}\)?", cell)),
                "0.00",
            )
            balance = _money(amount_text)
            if balance:
                balances.append(CcmaBalance(*current, balance))
            continue

        tax_index = next(
            (index for index, cell in enumerate(tail) if re.fullmatch(r"\d{3}", cell)),
            None,
        )
        movement_date = next(
            (cell for cell in tail if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", cell)),
            None,
        )
        if tax_index is None or movement_date is None:
            continue
        tax = tail[tax_index]
        date_index = tail.index(movement_date)
        description = tail[date_index - 1] if date_index else ""
        current = (_period_iso(period), tax, description, _date(movement_date))

    overdue = tuple(
        balance
        for balance in balances
        if balance.balance > 0 and balance.due_date <= calculated_on
    )
    upcoming = tuple(
        balance
        for balance in balances
        if balance.balance > 0 and balance.due_date > calculated_on
    )
    credits = tuple(balance for balance in balances if balance.balance < 0)
    detailed_debt = sum((item.balance for item in overdue), Decimal("0"))
    detailed_credit = -sum((item.balance for item in credits), Decimal("0"))
    if abs(detailed_debt - debtor_total) > Decimal("0.02"):
        raise CcmaError("El detalle de deuda no coincide con el total informado por CCMA")
    if abs(detailed_credit - creditor_total) > Decimal("0.02"):
        raise CcmaError("El detalle de crédito no coincide con el total informado por CCMA")

    return {
        "calculated_on": calculated_on.isoformat(),
        "window_from": _period_iso(period_match.group(1)),
        "window_to": _period_iso(period_match.group(2)),
        "debtor_total": str(debtor_total),
        "creditor_total": str(creditor_total),
        "overdue": [item.to_payload() for item in overdue],
        "upcoming": [item.to_payload() for item in upcoming],
        "credits": [item.to_payload() for item in credits],
    }
