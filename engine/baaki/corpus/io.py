"""Reading and writing a corpus as the files a merchant actually has.

A month of books leaves the dashboard as CSVs and the bank as a statement
export, so that is the shape Baaki takes as input. Generating to disk and
reading back also proves the engine depends on nothing but the files -- if a
run from CSV matches a run from memory, no adversary knowledge leaked through
the object graph.

The ground-truth file is written alongside, named ``_ground_truth.json`` with a
leading underscore and never loaded by :func:`load`. It is the answer key. The
engine has no code path that can read it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from ..models import (
    BankTxn,
    Corpus,
    Dispute,
    EntityType,
    FeeContract,
    InjectedDefect,
    Method,
    Network,
    Order,
    Payment,
    PaymentStatus,
    Reason,
    Refund,
    Settlement,
    SettlementRow,
    SettlementStatus,
)

TRUTH_FILE = "_ground_truth.json"


def _write(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _enum(value: str) -> str:
    return value.split(".")[-1].lower() if "." in value else value


def save(corpus: Corpus, out: Path, truth: list[InjectedDefect] | None = None) -> Path:
    """Write the corpus as the four sources plus the rate card."""
    out.mkdir(parents=True, exist_ok=True)

    _write(out / "orders.csv", [
        {
            "order_id": o.order_id,
            "amount_paise": o.amount_paise,
            "customer_id": o.customer_id,
            "created_at": o.created_at.isoformat(),
            "status": o.status,
            "channel": o.channel,
        }
        for o in corpus.orders
    ])

    _write(out / "payments.csv", [
        {
            "payment_id": p.payment_id,
            "order_id": p.order_id,
            "amount_paise": p.amount_paise,
            "method": p.method.value,
            "network": p.network.value,
            "status": p.status.value,
            "fee_paise": p.fee_paise,
            "tax_paise": p.tax_paise,
            "captured_at": p.captured_at.isoformat(),
            "international": int(p.international),
        }
        for p in corpus.payments
    ])

    _write(out / "refunds.csv", [
        {
            "refund_id": r.refund_id,
            "payment_id": r.payment_id,
            "amount_paise": r.amount_paise,
            "created_at": r.created_at.isoformat(),
            "speed": r.speed,
        }
        for r in corpus.refunds
    ])

    _write(out / "disputes.csv", [
        {
            "dispute_id": d.dispute_id,
            "payment_id": d.payment_id,
            "amount_paise": d.amount_paise,
            "status": d.status,
            "raised_at": d.raised_at.isoformat(),
        }
        for d in corpus.disputes
    ])

    _write(out / "settlements.csv", [
        {
            "settlement_id": s.settlement_id,
            "utr": s.utr or "",
            "net_paise": s.net_paise,
            "fees_paise": s.fees_paise,
            "tax_paise": s.tax_paise,
            "status": s.status.value,
            "created_at": s.created_at.isoformat(),
        }
        for s in corpus.settlements
    ])

    _write(out / "settlement_rows.csv", [
        {
            "settlement_id": r.settlement_id,
            "entity_type": r.entity_type.value,
            "entity_id": r.entity_id,
            "gross_paise": r.gross_paise,
            "fee_paise": r.fee_paise,
            "tax_paise": r.tax_paise,
            "net_paise": r.net_paise,
        }
        for r in corpus.settlement_rows
    ])

    _write(out / "bank_statement.csv", [
        {
            "bank_txn_id": b.bank_txn_id,
            "value_date": b.value_date.isoformat(),
            "narration": b.narration,
            "credit_paise": b.credit_paise,
            "debit_paise": b.debit_paise,
            "balance_paise": b.balance_paise,
        }
        for b in corpus.bank_txns
    ])

    _write(out / "contract.csv", [
        {
            "method": c.method.value,
            "network": c.network.value,
            "rate_bps": c.rate_bps,
            "fixed_paise": c.fixed_paise,
        }
        for c in corpus.contracts
    ])

    if truth is not None:
        payload = [
            {**asdict(d), "reason": d.reason.value} for d in truth
        ]
        (out / TRUTH_FILE).write_text(
            json.dumps(
                {
                    "warning": (
                        "Answer key. The engine has no code path that reads this file. "
                        "It exists so evaluation can measure recall against what was "
                        "actually planted."
                    ),
                    "defects": payload,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return out


def load(src: Path) -> Corpus:
    """Read a corpus back from the four sources. Never touches the answer key."""
    corpus = Corpus()

    for row in _read(src / "orders.csv"):
        corpus.orders.append(
            Order(
                order_id=row["order_id"],
                amount_paise=int(row["amount_paise"]),
                customer_id=row["customer_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                status=row["status"],
                channel=row["channel"],
            )
        )

    for row in _read(src / "payments.csv"):
        corpus.payments.append(
            Payment(
                payment_id=row["payment_id"],
                order_id=row["order_id"],
                amount_paise=int(row["amount_paise"]),
                method=Method(_enum(row["method"])),
                network=Network(_enum(row["network"])),
                status=PaymentStatus(_enum(row["status"])),
                fee_paise=int(row["fee_paise"]),
                tax_paise=int(row["tax_paise"]),
                captured_at=datetime.fromisoformat(row["captured_at"]),
                international=bool(int(row["international"])),
            )
        )

    for row in _read(src / "refunds.csv"):
        corpus.refunds.append(
            Refund(
                refund_id=row["refund_id"],
                payment_id=row["payment_id"],
                amount_paise=int(row["amount_paise"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                speed=row["speed"],
            )
        )

    for row in _read(src / "disputes.csv"):
        corpus.disputes.append(
            Dispute(
                dispute_id=row["dispute_id"],
                payment_id=row["payment_id"],
                amount_paise=int(row["amount_paise"]),
                status=row["status"],
                raised_at=datetime.fromisoformat(row["raised_at"]),
            )
        )

    for row in _read(src / "settlements.csv"):
        corpus.settlements.append(
            Settlement(
                settlement_id=row["settlement_id"],
                utr=row["utr"] or None,
                net_paise=int(row["net_paise"]),
                fees_paise=int(row["fees_paise"]),
                tax_paise=int(row["tax_paise"]),
                status=SettlementStatus(_enum(row["status"])),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    for row in _read(src / "settlement_rows.csv"):
        corpus.settlement_rows.append(
            SettlementRow(
                settlement_id=row["settlement_id"],
                entity_type=EntityType(_enum(row["entity_type"])),
                entity_id=row["entity_id"],
                gross_paise=int(row["gross_paise"]),
                fee_paise=int(row["fee_paise"]),
                tax_paise=int(row["tax_paise"]),
                net_paise=int(row["net_paise"]),
            )
        )

    for row in _read(src / "bank_statement.csv"):
        corpus.bank_txns.append(
            BankTxn(
                bank_txn_id=row["bank_txn_id"],
                value_date=date.fromisoformat(row["value_date"]),
                narration=row["narration"],
                credit_paise=int(row["credit_paise"]),
                debit_paise=int(row["debit_paise"]),
                balance_paise=int(row["balance_paise"]),
            )
        )

    for row in _read(src / "contract.csv"):
        corpus.contracts.append(
            FeeContract(
                method=Method(_enum(row["method"])),
                network=Network(_enum(row["network"])),
                rate_bps=int(row["rate_bps"]),
                fixed_paise=int(row["fixed_paise"]),
            )
        )

    return corpus


def load_truth(src: Path) -> list[InjectedDefect]:
    """Load the answer key. Only evaluation may call this."""
    path = src / TRUTH_FILE
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        InjectedDefect(
            defect_id=d["defect_id"],
            reason=Reason(d["reason"]),
            entity_type=d["entity_type"],
            entity_id=d["entity_id"],
            impact_paise=d["impact_paise"],
            note=d.get("note", ""),
        )
        for d in payload["defects"]
    ]
