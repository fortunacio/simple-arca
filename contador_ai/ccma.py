from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
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
    principal: Decimal | None = None
    principal_outstanding: Decimal | None = None
    interest: Decimal | None = None
    last_movement_description: str = ""
    last_movement_date: date | None = None

    def to_payload(self) -> dict[str, str]:
        payload = {
            "period": self.period,
            "tax": self.tax,
            "description": self.description,
            "due_date": self.due_date.isoformat(),
            "balance": str(self.balance),
        }
        if self.principal is not None:
            payload["principal"] = str(self.principal)
        if self.principal_outstanding is not None:
            payload["principal_outstanding"] = str(self.principal_outstanding)
        if self.interest is not None:
            payload["interest"] = str(self.interest)
        if self.last_movement_description:
            payload["last_movement_description"] = self.last_movement_description
        if self.last_movement_date is not None:
            payload["last_movement_date"] = self.last_movement_date.isoformat()
        return payload


@dataclass(frozen=True)
class CcmaPayment:
    period: str
    tax: str
    description: str
    paid_on: date
    amount: Decimal

    def to_payload(self) -> dict[str, str]:
        return {
            "period": self.period,
            "tax": self.tax,
            "description": self.description,
            "paid_on": self.paid_on.isoformat(),
            "amount": str(self.amount),
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


def _normalized_description(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    ).lower()


def _movement_amount(cells: Sequence[str]) -> Decimal | None:
    amount_text = next(
        (
            cell
            for cell in reversed(cells)
            if re.fullmatch(r"\(?[0-9,]+\.\d{2}\)?", cell)
        ),
        "",
    )
    return abs(_money(amount_text)) if amount_text else None


def _obligation_payloads(
    balances: Sequence[CcmaBalance],
    payments: Sequence[CcmaPayment],
    calculated_on: date,
) -> list[dict[str, str]]:
    grouped: dict[str, list[CcmaBalance]] = defaultdict(list)
    for balance in balances:
        if balance.balance > 0:
            grouped[balance.period].append(balance)

    payment_dates: dict[str, list[date]] = defaultdict(list)
    payment_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for payment in payments:
        payment_dates[payment.period].append(payment.paid_on)
        payment_totals[payment.period] += payment.amount

    result: list[dict[str, str]] = []
    for period, items in sorted(grouped.items()):
        due_date = min(item.due_date for item in items)
        balance_total = sum((item.balance for item in items), Decimal("0"))
        breakdown_known = all(item.principal is not None for item in items)
        principal = (
            sum((item.principal or Decimal("0") for item in items), Decimal("0"))
            if breakdown_known
            else None
        )
        principal_outstanding = (
            sum(
                (item.principal_outstanding or Decimal("0") for item in items),
                Decimal("0"),
            )
            if breakdown_known
            else None
        )
        interest = (
            sum((item.interest or Decimal("0") for item in items), Decimal("0"))
            if breakdown_known
            else None
        )
        latest_payment = max(payment_dates.get(period, []), default=None)
        if due_date >= calculated_on:
            status = "UPCOMING"
        elif (
            principal_outstanding is not None
            and principal_outstanding <= Decimal("0.02")
            and interest is not None
            and interest > Decimal("0.02")
        ):
            status = "INTEREST_ONLY"
        elif payment_totals.get(period, Decimal("0")) > 0:
            status = "PARTIALLY_POSTED"
        else:
            status = "OUTSTANDING"
        payload = {
            "period": period,
            "due_date": due_date.isoformat(),
            "balance": str(balance_total),
            "status": status,
            "latest_payment_date": latest_payment.isoformat() if latest_payment else "",
            "payments_total": str(payment_totals.get(period, Decimal("0"))),
        }
        if principal is not None:
            payload["principal"] = str(principal)
            payload["principal_outstanding"] = str(principal_outstanding)
            payload["interest"] = str(interest)
        result.append(payload)
    return result


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
    payments: list[CcmaPayment] = []
    current: tuple[str, str, str, date] | None = None
    obligations: dict[tuple[str, str], tuple[str, date, Decimal]] = {}
    principal_payments: dict[tuple[str, str], Decimal] = defaultdict(
        lambda: Decimal("0")
    )

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
                period_value, tax, description, movement_date = current
                obligation = obligations.get((period_value, tax))
                if obligation is None:
                    balances.append(
                        CcmaBalance(
                            period_value,
                            tax,
                            description,
                            movement_date,
                            balance,
                            last_movement_description=description,
                            last_movement_date=movement_date,
                        )
                    )
                else:
                    obligation_description, due_date, principal = obligation
                    principal_outstanding = max(
                        Decimal("0"),
                        principal - principal_payments[(period_value, tax)],
                    )
                    interest = (
                        max(Decimal("0"), balance - principal_outstanding)
                        if balance > 0
                        else Decimal("0")
                    )
                    balances.append(
                        CcmaBalance(
                            period_value,
                            tax,
                            obligation_description,
                            due_date,
                            balance,
                            principal,
                            principal_outstanding,
                            interest,
                            description,
                            movement_date,
                        )
                    )
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
        period_value = _period_iso(period)
        key = (period_value, tax)
        normalized_description = _normalized_description(description)
        amount = _movement_amount(tail[date_index + 1 :])
        if normalized_description.startswith("obligacion mensual") and amount is not None:
            previous = obligations.get(key)
            candidate = (description, _date(movement_date), amount)
            if previous is None or candidate[1] < previous[1]:
                obligations[key] = candidate
        if normalized_description.startswith("pago"):
            amount_text = next(
                (
                    cell
                    for cell in reversed(tail[date_index + 1 :])
                    if re.fullmatch(r"[0-9,]+\.\d{2}", cell)
                ),
                "",
            )
            if amount_text:
                payment_amount = abs(_money(f"({amount_text})"))
                payments.append(
                    CcmaPayment(
                        period_value,
                        tax,
                        description,
                        _date(movement_date),
                        payment_amount,
                    )
                )
                if "interes" not in normalized_description:
                    principal_payments[key] += payment_amount
        current = (period_value, tax, description, _date(movement_date))

    debt_bearing = tuple(
        balance
        for balance in balances
        if balance.balance > 0 and balance.due_date <= calculated_on
    )
    current_period = calculated_on.strftime("%Y-%m")
    overdue = tuple(
        balance
        for balance in debt_bearing
        if balance.due_date < calculated_on or balance.period < current_period
    )
    upcoming = tuple(
        balance
        for balance in balances
        if balance.balance > 0
        and (
            balance.due_date > calculated_on
            or (balance.due_date == calculated_on and balance.period >= current_period)
        )
    )
    credits = tuple(balance for balance in balances if balance.balance < 0)
    detailed_debt = sum((item.balance for item in debt_bearing), Decimal("0"))
    detailed_credit = -sum((item.balance for item in credits), Decimal("0"))
    if abs(detailed_debt - debtor_total) > Decimal("0.02"):
        raise CcmaError("El detalle de deuda no coincide con el total informado por CCMA")
    if abs(detailed_credit - creditor_total) > Decimal("0.02"):
        raise CcmaError("El detalle de crédito no coincide con el total informado por CCMA")

    recent_payments = sorted(
        payments,
        key=lambda item: (item.paid_on, item.period, item.tax, item.description),
        reverse=True,
    )[:30]
    latest_payment_date = max((item.paid_on for item in payments), default=None)
    latest_payment_total = sum(
        (item.amount for item in payments if item.paid_on == latest_payment_date),
        Decimal("0"),
    )
    overdue_periods = {item.period for item in debt_bearing}
    latest_periods = {
        item.period for item in payments if item.paid_on == latest_payment_date
    }
    payment_reconciliation = (
        "PARTIAL"
        if latest_payment_date is not None
        and debtor_total > 0
        and bool(overdue_periods & latest_periods)
        else "POSTED"
        if latest_payment_date is not None
        else "UNKNOWN"
    )
    return {
        "calculated_on": calculated_on.isoformat(),
        "window_from": _period_iso(period_match.group(1)),
        "window_to": _period_iso(period_match.group(2)),
        "debtor_total": str(debtor_total),
        "creditor_total": str(creditor_total),
        "overdue": [item.to_payload() for item in overdue],
        "upcoming": [item.to_payload() for item in upcoming],
        "credits": [item.to_payload() for item in credits],
        "payments": [item.to_payload() for item in recent_payments],
        "obligations": _obligation_payloads(balances, payments, calculated_on),
        "latest_payment_date": latest_payment_date.isoformat() if latest_payment_date else "",
        "latest_payment_total": str(latest_payment_total),
        "payment_reconciliation": payment_reconciliation,
    }
