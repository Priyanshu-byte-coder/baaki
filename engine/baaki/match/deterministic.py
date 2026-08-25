"""Stage 1b: exact joins along the order-to-bank chain.

Everything here is an index lookup or a set difference on identifiers that are
supposed to join exactly. No scoring, no thresholds, no model. If a captured
payment has no settlement line, that is not a judgement call.

Between them, this module and :mod:`baaki.match.arithmetic` close the large
majority of every batch. That is the intended shape: the expensive stages exist
to handle what is genuinely ambiguous, and the amount of ambiguity in a set of
books is much smaller than it first appears.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from ..contract import SETTLEMENT_LAG_DAYS
from ..models import Corpus, EntityType, PaymentStatus, Reason, SettlementStatus
from ..money import rupees
from .findings import Evidence, Finding, Stage


def check_payments_reach_a_settlement(corpus: Corpus) -> list[Finding]:
    """Every captured payment must appear on exactly one settlement line.

    A captured payment with no settlement line is the worst case in the
    taxonomy: the customer was debited and the money never came back out the
    other side.
    """
    settled_ids = {
        row.entity_id for row in corpus.settlement_rows if row.entity_type is EntityType.PAYMENT
    }
    findings: list[Finding] = []

    for payment in corpus.payments:
        if payment.status is not PaymentStatus.CAPTURED:
            continue
        if payment.payment_id in settled_ids:
            continue

        expected_net = payment.amount_paise - payment.fee_paise - payment.tax_paise
        findings.append(
            Finding(
                reason=Reason.ORDER_PAID_NOT_SETTLED,
                entity_type="payment",
                entity_id=payment.payment_id,
                impact_paise=expected_net,
                stage=Stage.DETERMINISTIC,
                confidence=1.0,
                explanation=(
                    f"Captured {rupees(payment.amount_paise)} on "
                    f"{payment.captured_at:%d %b %Y} via {payment.method.value}, but the "
                    f"payment appears on no settlement line. Expected net "
                    f"{rupees(expected_net)}."
                ),
                evidence=[
                    Evidence("payments", payment.payment_id, "status", payment.status.value),
                    Evidence("payments", payment.payment_id, "amount", rupees(payment.amount_paise)),
                    Evidence("settlement_rows", payment.payment_id, "matches", "0"),
                    Evidence("orders", payment.order_id, "order_id", payment.order_id),
                ],
            )
        )
    return findings


def check_duplicate_captures(corpus: Corpus) -> list[Finding]:
    """One order, two captured payments.

    Invisible inside the gateway's own reports -- both captures are perfectly
    valid there. It only surfaces against the merchant's order ledger, which is
    the argument for reconciling four sources rather than two.
    """
    by_order: dict[str, list] = defaultdict(list)
    for payment in corpus.payments:
        if payment.status is PaymentStatus.CAPTURED:
            by_order[payment.order_id].append(payment)

    findings: list[Finding] = []
    for order_id, payments in by_order.items():
        if len(payments) < 2:
            continue
        payments.sort(key=lambda p: p.captured_at)
        original, extras = payments[0], payments[1:]
        impact = sum(p.amount_paise for p in extras)
        gap = extras[0].captured_at - original.captured_at

        findings.append(
            Finding(
                reason=Reason.DUPLICATE_PAYMENT,
                entity_type="order",
                entity_id=order_id,
                impact_paise=impact,
                stage=Stage.DETERMINISTIC,
                confidence=1.0,
                explanation=(
                    f"{len(payments)} captured payments against one order, "
                    f"{int(gap.total_seconds())}s apart, totalling an extra "
                    f"{rupees(impact)}. The customer has been charged twice."
                ),
                evidence=[
                    Evidence("orders", order_id, "captured_payments", str(len(payments))),
                    *[
                        Evidence("payments", p.payment_id, "amount", rupees(p.amount_paise))
                        for p in payments
                    ],
                ],
            )
        )
    return findings


def check_double_deductions(corpus: Corpus) -> list[Finding]:
    """A refund or chargeback deducted on more than one settlement.

    The merchant is debited twice for one event, so the whole of the second
    deduction is recoverable.
    """
    occurrences: dict[tuple[EntityType, str], list] = defaultdict(list)
    for row in corpus.settlement_rows:
        if row.entity_type in (EntityType.REFUND, EntityType.DISPUTE):
            occurrences[(row.entity_type, row.entity_id)].append(row)

    reason_for = {
        EntityType.REFUND: Reason.REFUND_DOUBLE_COUNTED,
        EntityType.DISPUTE: Reason.CHARGEBACK_NETTED_TWICE,
    }

    findings: list[Finding] = []
    for (entity_type, entity_id), rows in occurrences.items():
        if len(rows) < 2:
            continue
        duplicate_value = sum(abs(r.net_paise) for r in rows[1:])
        where = ", ".join(r.settlement_id for r in rows)

        findings.append(
            Finding(
                reason=reason_for[entity_type],
                entity_type=entity_type.value,
                entity_id=entity_id,
                impact_paise=duplicate_value,
                stage=Stage.DETERMINISTIC,
                confidence=1.0,
                explanation=(
                    f"Deducted on {len(rows)} settlements ({where}) for a single "
                    f"{entity_type.value}. {rupees(duplicate_value)} of the deduction "
                    f"is duplicated."
                ),
                evidence=[
                    Evidence("settlement_rows", r.settlement_id, "net", rupees(r.net_paise))
                    for r in rows
                ],
            )
        )
    return findings


def check_settlement_timeliness(corpus: Corpus) -> list[Finding]:
    """Settlements that landed outside the contracted T+2 window.

    Reported with a zero rupee impact. The principal is not at risk, only the
    working capital, and folding an SLA breach into a "money found" total would
    inflate the number that matters most.
    """
    latest_capture: dict[str, object] = {}
    payment_by_id = {p.payment_id: p for p in corpus.payments}
    for row in corpus.settlement_rows:
        if row.entity_type is not EntityType.PAYMENT:
            continue
        payment = payment_by_id.get(row.entity_id)
        if payment is None:
            continue
        current = latest_capture.get(row.settlement_id)
        if current is None or payment.captured_at > current:
            latest_capture[row.settlement_id] = payment.captured_at

    findings: list[Finding] = []
    for settlement in corpus.settlements:
        captured_at = latest_capture.get(settlement.settlement_id)
        if captured_at is None:
            # Refund-only batches have no capture to measure a window against.
            continue
        due = (captured_at + timedelta(days=SETTLEMENT_LAG_DAYS)).date()
        actual = settlement.created_at.date()
        if actual <= due:
            continue

        days_late = (actual - due).days
        findings.append(
            Finding(
                reason=Reason.LATE_SETTLEMENT,
                entity_type="settlement",
                entity_id=settlement.settlement_id,
                impact_paise=0,
                stage=Stage.DETERMINISTIC,
                confidence=1.0,
                explanation=(
                    f"Settled {actual:%d %b} against a contractual due date of "
                    f"{due:%d %b}, {days_late} day(s) late. "
                    f"{rupees(settlement.net_paise)} was held beyond T+"
                    f"{SETTLEMENT_LAG_DAYS}."
                ),
                evidence=[
                    Evidence("settlements", settlement.settlement_id, "created_at", f"{actual}"),
                    Evidence("computed", settlement.settlement_id, "due_date", f"{due}"),
                    Evidence(
                        "settlements", settlement.settlement_id, "net", rupees(settlement.net_paise)
                    ),
                ],
            )
        )
    return findings


def check_held_settlements(corpus: Corpus) -> list[Finding]:
    """Settlements the gateway is withholding.

    Reported so the amount is visible in the cash position, but *not* as
    missing money. A held settlement and a settlement that vanished between the
    gateway and the bank look identical from the bank statement alone -- both
    have no credit. The status field is the only thing separating them, and
    :mod:`baaki.match.fuzzy` relies on this check having already accounted for
    the held ones before it starts reporting credits as missing.
    """
    findings: list[Finding] = []
    for settlement in corpus.settlements:
        if settlement.status is not SettlementStatus.ON_HOLD:
            continue
        findings.append(
            Finding(
                reason=Reason.SETTLEMENT_ON_HOLD,
                entity_type="settlement",
                entity_id=settlement.settlement_id,
                impact_paise=settlement.net_paise,
                stage=Stage.DETERMINISTIC,
                confidence=1.0,
                explanation=(
                    f"{rupees(settlement.net_paise)} withheld by the gateway. No bank "
                    f"credit is due, so this is not missing money, but it is not "
                    f"available cash either."
                ),
                evidence=[
                    Evidence("settlements", settlement.settlement_id, "status", "on_hold"),
                    Evidence("settlements", settlement.settlement_id, "utr", str(settlement.utr)),
                    Evidence(
                        "settlements", settlement.settlement_id, "net", rupees(settlement.net_paise)
                    ),
                ],
            )
        )
    return findings


def run(corpus: Corpus) -> list[Finding]:
    """Every exact-join check, in chain order."""
    return (
        check_payments_reach_a_settlement(corpus)
        + check_duplicate_captures(corpus)
        + check_double_deductions(corpus)
        + check_settlement_timeliness(corpus)
        + check_held_settlements(corpus)
    )
