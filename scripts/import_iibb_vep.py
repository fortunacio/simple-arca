"""One-off, offline receipt import. JSON on stdin; read-only unless apply=true.

Run in the deployed application context, using its existing SIAT PDF validator.
No portal requests, schema changes, generation jobs or Telegram messages.
"""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from contador_ai.iibb_payment import ACTIVE_STATES, NETWORKS
from contador_ai.siat import parse_vep_pdf


class ImportRefused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ImportRefused(message)


def account_key(value):
    value = str(value)
    require(bool(re.fullmatch(r"[0-9-]+", value)), "Invalid account")
    return value.replace("-", "")


def connect(path, mode):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + f"?mode={mode}",
                                 uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def fresh_snapshot(connection, chat_id, provider, now):
    row = connection.execute(
        "SELECT * FROM live_snapshots WHERE chat_id=? AND provider=? AND kind=?",
        (chat_id, provider, "account_status" if provider == "SANTA_FE_IIBB" else "payment_status"),
    ).fetchone()
    require(row is not None, f"Missing {provider} snapshot")
    fetched = datetime.fromisoformat(row["fetched_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    require(fetched.tzinfo is not None and expires.tzinfo is not None,
            "Snapshot timestamps must have timezone")
    require(now - timedelta(hours=1) <= fetched <= now and expires > now,
            f"Stale {provider} snapshot; refresh before import")
    return json.loads(row["payload_json"])


def validate(connection, spec, now):
    require(type(spec["chat_id"]) is int, "Invalid chat id")
    quote = spec["quote"]
    require(bool(re.fullmatch(r"[0-9]{6,12}", spec["vep_number"])), "Invalid VEP number")
    require(spec["network_id"] == "1002", "This importer supports the verified BANELCO receipt only")
    require(bool(re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", quote["period"])), "Invalid period")
    for name in ("principal", "interest", "total"):
        require(bool(re.fullmatch(r"[0-9]+\.[0-9]{2}", quote[name])), "Invalid amount")
    require(Decimal(quote["total"]) > 0 and
            Decimal(quote["principal"]) + Decimal(quote["interest"]) == Decimal(quote["total"]),
            "Invalid amount breakdown")
    for name in ("payment_date", "due_date", "vep_expires_on"):
        date.fromisoformat(quote[name])
    require(quote["payment_date"] <= quote["vep_expires_on"], "Invalid receipt dates")
    document = base64.b64decode(spec["pdf_base64"], validate=True)
    checksum = hashlib.sha256(document).hexdigest()
    require(checksum == spec["sha256"], "Receipt checksum mismatch")
    identity = connection.execute("SELECT * FROM tax_identities WHERE chat_id=?",
                                  (spec["chat_id"],)).fetchone()
    require(identity is not None and identity["status"] == "VERIFIED" and
            identity["environment"] == "production", "Missing verified production identity")
    result = parse_vep_pdf(document, identity["represented_cuit"], quote)
    require(result["vep_number"] == spec["vep_number"], "Receipt VEP mismatch")
    # Check all representations, not only the current literal account string.
    rows = connection.execute(
        "SELECT * FROM iibb_payment_drafts WHERE vep_number=? OR (chat_id=? AND period=?)",
        (spec["vep_number"], spec["chat_id"], quote["period"]),
    ).fetchall()
    same = [row for row in rows if row["vep_number"] == spec["vep_number"]]
    require(len(same) <= 1, "Duplicate VEP records already exist")
    for row in rows:
        if row["vep_number"] == spec["vep_number"]:
            expected = {"chat_id": spec["chat_id"], "represented_cuit": identity["represented_cuit"],
                        "period": quote["period"], "network_id": spec["network_id"],
                        "principal": quote["principal"], "interest": quote["interest"],
                        "total": quote["total"], "vep_expires_on": quote["vep_expires_on"],
                        "payment_date": quote["payment_date"], "obligation_due_date": quote["due_date"]}
            require(all(row[k] == value for k, value in expected.items()) and
                    account_key(row["account"]) == account_key(spec["account"]) and
                    row["official_pdf"] == document and row["status"] in ("GENERATED", "PAID", "EXPIRED"),
                    "Existing VEP conflicts with receipt; nothing overwritten")
        elif account_key(row["account"]) == account_key(spec["account"]):
            require(row["status"] not in (*ACTIVE_STATES, "PAID"),
                    "Period already has an active or paid record; nothing overwritten")
    if same:
        return {"status": "ALREADY_PRESENT", "id": same[0]["id"]}, document, identity
    siat = fresh_snapshot(connection, spec["chat_id"], "SANTA_FE_IIBB", now)
    require(siat.get("account") == spec["account"], "Account differs from current SIAT snapshot")
    obligation = siat.get("next_obligation") or {}
    require(obligation.get("period") == quote["period"] and
            obligation.get("due_date") == quote["due_date"] and
            Decimal(str(obligation.get("amount", "-1"))) == Decimal(quote["total"]),
            "SIAT pending obligation differs; inspect before import")
    require(date.fromisoformat(quote["vep_expires_on"]) >= now.astimezone(
        timezone(timedelta(hours=-3))).date(), "VEP has expired; inspect before import")
    seti = fresh_snapshot(connection, spec["chat_id"], "ARCA_SETI", now)
    veps = [v for v in seti.get("veps", []) if v.get("number") == spec["vep_number"]]
    require(len(veps) == 1, "SETI must contain exactly one matching VEP")
    vep = veps[0]
    require(vep.get("seti_state") == 1 and vep.get("status") == "UNKNOWN" and
            not vep.get("paid_on") and not vep.get("paid_at") and
            vep.get("tax_type") == "SANTA_FE_IIBB" and vep.get("tax_period") == quote["period"] and
            vep.get("payment_entity") == "BANELCO" and
            Decimal(str(vep.get("amount", "-1"))) == Decimal(quote["total"]),
            "SETI state or values changed; inspect before import")
    row_id = "imp" + hashlib.sha256(
        f'{spec["chat_id"]}:{spec["vep_number"]}'.encode()).hexdigest()[:13]
    return {"status": "READY", "id": row_id}, document, identity


def backup_database(db_path, backup_path):
    """Called with BEGIN IMMEDIATE held by another connection: no writer can race."""
    backup = Path(backup_path).resolve()
    require(backup.parent == Path(db_path).resolve().parent, "Backup must be beside database")
    # Exclusive creation prevents clobbering an existing file. Restricted from creation.
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(connect(db_path, "ro")) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
        require(target.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Backup failed integrity check")
    return str(backup)


def run(spec, *, now=None):
    now = now or datetime.now(timezone.utc)
    apply = spec.get("apply", False)
    require(type(apply) is bool, "apply must be a boolean")
    operation = spec.get("operation", "import")
    require(operation in ("import", "rollback"), "Invalid operation")
    with closing(connect(spec["db"], "rw" if apply else "ro")) as connection:
        try:
            # Acquire the write reservation BEFORE validation and keep it through commit.
            connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            result, document, identity = validate(connection, spec, now)
            if operation == "rollback":
                require(result["status"] == "ALREADY_PRESENT", "Imported receipt not found")
                row = connection.execute("SELECT * FROM iibb_payment_drafts WHERE id=?", (result["id"],)).fetchone()
                events = connection.execute("SELECT * FROM iibb_payment_events WHERE draft_id=?", (result["id"],)).fetchall()
                require(row["status"] == "GENERATED" and row["notification_pending"] == 0 and
                        row["created_at"] == row["updated_at"] and len(events) == 1 and
                        events[0]["event_type"] == "IMPORTED" and
                        json.loads(events[0]["payload_json"]).get("sha256") == spec["sha256"],
                        "Imported record has changed; automatic rollback refused")
                if not apply:
                    connection.rollback()
                    return {"status": "ROLLBACK_READY", "id": result["id"]}
                backup = backup_database(spec["db"], spec["backup"])
                connection.execute("DELETE FROM iibb_payment_events WHERE id=?", (events[0]["id"],))
                connection.execute("DELETE FROM iibb_payment_drafts WHERE id=?", (result["id"],))
                connection.commit()
                return {"status": "ROLLED_BACK", "id": result["id"], "backup": backup}
            if not apply or result["status"] == "ALREADY_PRESENT":
                connection.rollback()
                return result
            backup = backup_database(spec["db"], spec["backup"])
            quote = spec["quote"]
            connection.execute(
                """INSERT INTO iibb_payment_drafts (
                    id,chat_id,represented_cuit,account,period,status,network_id,network_name,
                    payment_date,obligation_due_date,principal,interest,total,vep_number,
                    vep_expires_on,official_pdf,notification_pending
                ) VALUES (?,?,?,?,?,'GENERATED',?,?,?,?,?,?,?,?,?,?,0)""",
                (result["id"], spec["chat_id"], identity["represented_cuit"], spec["account"],
                 quote["period"], spec["network_id"], NETWORKS[spec["network_id"]],
                 quote["payment_date"], quote["due_date"], quote["principal"], quote["interest"],
                 quote["total"], spec["vep_number"], quote["vep_expires_on"], document),
            )
            connection.execute(
                "INSERT INTO iibb_payment_events(draft_id,chat_id,event_type,payload_json) VALUES (?,?,'IMPORTED',?)",
                (result["id"], spec["chat_id"], json.dumps({"vep_number": spec["vep_number"],
                    "sha256": spec["sha256"], "source": "official_receipt_and_seti",
                    "backup": backup}, sort_keys=True)),
            )
            verified, _, _ = validate(connection, spec, now)
            require(verified["status"] == "ALREADY_PRESENT", "Post-insert validation failed")
            require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "Foreign key check failed")
            connection.commit()
            return {"status": "IMPORTED", "id": result["id"], "backup": backup}
        except Exception:
            connection.rollback()
            raise


if __name__ == "__main__":
    try:
        print(json.dumps(run(json.load(sys.stdin)), sort_keys=True))
    except Exception as exc:
        # Do not print parser diagnostics, database payloads or fiscal identity.
        print(json.dumps({"status": "REFUSED", "error_type": type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
