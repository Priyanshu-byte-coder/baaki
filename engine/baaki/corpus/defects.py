"""Fault injection: plant known defects in a clean corpus and label every one.

This is the other half of the adversary. Each injector mutates the corpus the
way the underlying failure would really mutate a merchant's books, and appends
one :class:`~baaki.models.InjectedDefect` describing what it did.

Two properties matter for the evaluation to mean anything:

**Coherent propagation.** If the gateway overcharges a fee, the merchant
receives less: the payment row, the settlement line, the settlement header and
the bank credit all move together. An injector that changed only the fee would
also manufacture a settlement-mismatch defect it never declared, and the engine
would be scored as a false positive for correctly noticing it.

**Isolation.** Each entity is touched by at most one injector, tracked in
:class:`Injector._claimed`. Overlapping defects make recall ambiguous -- if two
faults land on one entity, it is no longer clear which one a finding found.

Claims are namespaced by entity kind (``pay:``, ``setl:``, ``order:``,
``ent:``) rather than pooled. An earlier version claimed the enclosing
``settlement_id`` for payment level faults, and since a month of books contains
only a few dozen settlements, the first injector to run claimed nearly all of
them and every subsequent injector silently planted nothing. Per code recall
looked perfect because the denominator was zero.
"""

from __future__ import annotations

import random
from datetime import timedelta

from ..models import (
    BankTxn,
    Corpus,
    EntityType,
    InjectedDefect,
    Payment,
    PaymentStatus,
    Reason,
    Settlement,
    SettlementRow,
    SettlementStatus,
)
from ..money import compute_fee, compute_gst
from .generate import Generated, contract_for, rebalance


class Injector:
    """Plants labelled faults into a generated corpus."""

    def __init__(self, generated: Generated, seed: int) -> None:
        self.g = generated
        self.corpus: Corpus = generated.corpus
        self.rng = random.Random(seed * 7919 + 13)
        self._claimed: set[str] = set()
        self._counter = 0

        self.settlement_by_id: dict[str, Settlement] = {
            s.settlement_id: s for s in self.corpus.settlements
        }
        self.rows_by_settlement: dict[str, list[SettlementRow]] = {}
        for row in self.corpus.settlement_rows:
            self.rows_by_settlement.setdefault(row.settlement_id, []).append(row)
        self.row_by_entity: dict[str, SettlementRow] = {
            row.entity_id: row for row in self.corpus.settlement_rows
        }
        self.bank_by_id: dict[str, BankTxn] = {b.bank_txn_id: b for b in self.corpus.bank_txns}
        self.payment_by_id: dict[str, Payment] = {p.payment_id: p for p in self.corpus.payments}

    # -- plumbing -----------------------------------------------------------

    def _next_id(self, reason: Reason) -> str:
        self._counter += 1
        return f"defect_{self._counter:04d}_{reason.value.lower()}"

    def _claim(self, *keys: str) -> bool:
        """Reserve entities for one defect. Returns False if any is already used."""
        if any(k in self._claimed for k in keys):
            return False
        self._claimed.update(keys)
        return True

    def _adjust_settlement(self, settlement_id: str, delta_net: int) -> None:
        """Move a settlement header and its bank credit by the same delta."""
        settlement = self.settlement_by_id[settlement_id]
        settlement.net_paise += delta_net
        bank_id = self.g.settlement_to_bank.get(settlement_id)
        if bank_id and bank_id in self.bank_by_id:
            self.bank_by_id[bank_id].credit_paise += delta_net

    def _settled_payments(self) -> list[Payment]:
        return [
            p
            for p in self.corpus.payments
            if p.status is PaymentStatus.CAPTURED and p.payment_id in self.row_by_entity
        ]

    def _paid_settlements(self) -> list[Settlement]:
        """Settlements that actually produced a bank credit."""
        return [
            s
            for s in self.corpus.settlements
            if s.settlement_id in self.g.settlement_to_bank
            and self.g.settlement_to_bank[s.settlement_id] in self.bank_by_id
        ]

    def _record(
        self, reason: Reason, entity_type: str, entity_id: str, impact: int, note: str
    ) -> None:
        self.g.truth.append(
            InjectedDefect(
                defect_id=self._next_id(reason),
                reason=reason,
                entity_type=entity_type,
                entity_id=entity_id,
                impact_paise=impact,
                note=note,
            )
        )

    # -- injectors ----------------------------------------------------------

    def mdr_overcharge(self, count: int, extra_bps: int = 45) -> None:
        """The gateway bills a rate above the one in the contract.

        The commonest silent margin leak, and invisible on any single
        transaction: on a 500 rupee UPI payment an extra 45bps is 2.25 rupees.
        Across a month it is a salary.
        """
        pool = self._settled_payments()
        self.rng.shuffle(pool)
        planted = 0
        for payment in pool:
            if planted >= count:
                break
            row = self.row_by_entity[payment.payment_id]
            if not self._claim(f"pay:{payment.payment_id}"):
                continue

            rate_bps, fixed = contract_for(
                self.corpus.contracts, payment.method, payment.network, payment.international
            )
            new_fee = compute_fee(payment.amount_paise, rate_bps + extra_bps, fixed)
            new_tax = compute_gst(new_fee)
            overcharge = (new_fee + new_tax) - (payment.fee_paise + payment.tax_paise)
            if overcharge <= 0:
                continue

            settlement = self.settlement_by_id[row.settlement_id]
            settlement.fees_paise += new_fee - payment.fee_paise
            settlement.tax_paise += new_tax - payment.tax_paise
            payment.fee_paise, payment.tax_paise = new_fee, new_tax
            row.fee_paise, row.tax_paise = new_fee, new_tax
            row.net_paise = row.gross_paise - new_fee - new_tax
            self._adjust_settlement(row.settlement_id, -overcharge)

            self._record(
                Reason.MDR_OVERCHARGE,
                "payment",
                payment.payment_id,
                overcharge,
                f"Billed at {rate_bps + extra_bps}bps against a contracted {rate_bps}bps.",
            )
            planted += 1

    def gst_miscalc(self, count: int) -> None:
        """GST charged on the transaction value instead of on the fee.

        Eighteen percent of gross rather than of fee. Large, obvious once you
        look, and routinely missed because nobody recomputes the tax column.
        """
        pool = self._settled_payments()
        self.rng.shuffle(pool)
        planted = 0
        for payment in pool:
            if planted >= count:
                break
            row = self.row_by_entity[payment.payment_id]
            if payment.fee_paise <= 0 or not self._claim(f"pay:{payment.payment_id}"):
                continue

            new_tax = compute_gst(payment.amount_paise)
            delta = new_tax - payment.tax_paise
            if delta <= 0:
                continue

            settlement = self.settlement_by_id[row.settlement_id]
            settlement.tax_paise += delta
            payment.tax_paise = new_tax
            row.tax_paise = new_tax
            row.net_paise = row.gross_paise - row.fee_paise - new_tax
            self._adjust_settlement(row.settlement_id, -delta)

            self._record(
                Reason.GST_MISCALC,
                "payment",
                payment.payment_id,
                delta,
                "Tax billed as 18% of gross rather than 18% of fee.",
            )
            planted += 1

    def order_paid_not_settled(self, count: int) -> None:
        """Captured from the customer, never included in any settlement."""
        pool = self._settled_payments()
        self.rng.shuffle(pool)
        planted = 0
        for payment in pool:
            if planted >= count:
                break
            row = self.row_by_entity[payment.payment_id]
            if not self._claim(f"pay:{payment.payment_id}"):
                continue

            settlement = self.settlement_by_id[row.settlement_id]
            settlement.fees_paise -= row.fee_paise
            settlement.tax_paise -= row.tax_paise
            self._adjust_settlement(row.settlement_id, -row.net_paise)
            self.corpus.settlement_rows.remove(row)
            self.rows_by_settlement[row.settlement_id].remove(row)
            del self.row_by_entity[payment.payment_id]

            self._record(
                Reason.ORDER_PAID_NOT_SETTLED,
                "payment",
                payment.payment_id,
                row.net_paise,
                "Captured payment absent from every settlement.",
            )
            planted += 1

    def settled_not_in_bank(self, count: int) -> None:
        """A settlement was issued with a UTR, but no credit ever landed."""
        pool = self._paid_settlements()
        self.rng.shuffle(pool)
        planted = 0
        for settlement in pool:
            if planted >= count:
                break
            if not self._claim(f"setl:{settlement.settlement_id}"):
                continue
            bank_id = self.g.settlement_to_bank[settlement.settlement_id]
            txn = self.bank_by_id.pop(bank_id)
            self.corpus.bank_txns.remove(txn)
            del self.g.settlement_to_bank[settlement.settlement_id]

            self._record(
                Reason.SETTLED_NOT_IN_BANK,
                "settlement",
                settlement.settlement_id,
                settlement.net_paise,
                f"UTR {settlement.utr} has no corresponding bank credit.",
            )
            planted += 1

    def settlement_amount_mismatch(self, count: int) -> None:
        """The settlement header disagrees with the sum of its own lines.

        The bank credit follows the header, so the merchant is paid the wrong
        amount and only line-level arithmetic reveals it.
        """
        pool = self._paid_settlements()
        self.rng.shuffle(pool)
        planted = 0
        for settlement in pool:
            if planted >= count:
                break
            if not self._claim(settlement.settlement_id):
                continue
            delta = -self.rng.randrange(50_00, 5_000_00)
            self._adjust_settlement(settlement.settlement_id, delta)

            self._record(
                Reason.SETTLEMENT_AMOUNT_MISMATCH,
                "settlement",
                settlement.settlement_id,
                abs(delta),
                "Header net differs from the sum of the settlement lines.",
            )
            planted += 1

    def _double_count(self, count: int, entity_type: EntityType, reason: Reason) -> None:
        """Shared body for a refund or chargeback deducted in two settlements."""
        originals = [
            row
            for row in self.corpus.settlement_rows
            if row.entity_type is entity_type and f"ent:{row.entity_id}" not in self._claimed
        ]
        targets = self._paid_settlements()
        self.rng.shuffle(originals)
        planted = 0
        for row in originals:
            if planted >= count:
                break
            other = self.rng.choice(targets)
            if other.settlement_id == row.settlement_id:
                continue
            if not self._claim(f"ent:{row.entity_id}"):
                continue

            duplicate = SettlementRow(
                settlement_id=other.settlement_id,
                entity_type=entity_type,
                entity_id=row.entity_id,
                gross_paise=row.gross_paise,
                fee_paise=0,
                tax_paise=0,
                net_paise=row.net_paise,
            )
            self.corpus.settlement_rows.append(duplicate)
            self.rows_by_settlement.setdefault(other.settlement_id, []).append(duplicate)
            self._adjust_settlement(other.settlement_id, duplicate.net_paise)

            self._record(
                reason,
                entity_type.value,
                row.entity_id,
                abs(row.net_paise),
                f"Deducted in both {row.settlement_id} and {other.settlement_id}.",
            )
            planted += 1

    def refund_double_counted(self, count: int) -> None:
        self._double_count(count, EntityType.REFUND, Reason.REFUND_DOUBLE_COUNTED)

    def chargeback_netted_twice(self, count: int) -> None:
        self._double_count(count, EntityType.DISPUTE, Reason.CHARGEBACK_NETTED_TWICE)

    def duplicate_payment(self, count: int) -> None:
        """One order captured twice, both halves settled normally.

        Nothing in the settlement report looks wrong here. It is only visible
        against the merchant's own order ledger, which is exactly why a
        gateway-only reconciliation misses it.
        """
        pool = self._settled_payments()
        self.rng.shuffle(pool)
        planted = 0
        for payment in pool:
            if planted >= count:
                break
            row = self.row_by_entity[payment.payment_id]
            if not self._claim(f"pay:{payment.payment_id}", f"order:{payment.order_id}"):
                continue

            dup = Payment(
                payment_id=f"{payment.payment_id}d",
                order_id=payment.order_id,
                amount_paise=payment.amount_paise,
                method=payment.method,
                network=payment.network,
                status=PaymentStatus.CAPTURED,
                fee_paise=payment.fee_paise,
                tax_paise=payment.tax_paise,
                captured_at=payment.captured_at + timedelta(seconds=self.rng.randrange(20, 300)),
                international=payment.international,
            )
            dup_row = SettlementRow(
                settlement_id=row.settlement_id,
                entity_type=EntityType.PAYMENT,
                entity_id=dup.payment_id,
                gross_paise=dup.amount_paise,
                fee_paise=dup.fee_paise,
                tax_paise=dup.tax_paise,
                net_paise=dup.amount_paise - dup.fee_paise - dup.tax_paise,
            )
            self.corpus.payments.append(dup)
            self.payment_by_id[dup.payment_id] = dup
            self.corpus.settlement_rows.append(dup_row)
            self.rows_by_settlement[row.settlement_id].append(dup_row)
            self.row_by_entity[dup.payment_id] = dup_row

            settlement = self.settlement_by_id[row.settlement_id]
            settlement.fees_paise += dup.fee_paise
            settlement.tax_paise += dup.tax_paise
            self._adjust_settlement(row.settlement_id, dup_row.net_paise)

            self._record(
                Reason.DUPLICATE_PAYMENT,
                "order",
                payment.order_id,
                dup.amount_paise,
                "Two captured payments against a single order.",
            )
            planted += 1

    def late_settlement(self, count: int, extra_days: int = 4) -> None:
        """Settled outside the contracted T+2 window.

        Not a loss of principal, so the impact is recorded as zero. It is a
        working-capital cost and an SLA breach, and the engine is expected to
        report it separately from money that is actually missing.
        """
        pool = self._paid_settlements()
        self.rng.shuffle(pool)
        planted = 0
        for settlement in pool:
            if planted >= count:
                break
            if not self._claim(settlement.settlement_id):
                continue
            shift = timedelta(days=extra_days)
            settlement.created_at += shift
            bank_id = self.g.settlement_to_bank[settlement.settlement_id]
            self.bank_by_id[bank_id].value_date += shift

            self._record(
                Reason.LATE_SETTLEMENT,
                "settlement",
                settlement.settlement_id,
                0,
                f"Settled T+{2 + extra_days} against a contracted T+2.",
            )
            planted += 1

    def partial_bank_credit(self, count: int) -> None:
        """One settlement paid out as two bank credits.

        Benign, but it defeats naive amount matching: neither credit equals the
        settlement, and the engine has to find the pair that sums to it.
        """
        pool = self._paid_settlements()
        self.rng.shuffle(pool)
        planted = 0
        for settlement in pool:
            if planted >= count:
                break
            if not self._claim(settlement.settlement_id):
                continue
            bank_id = self.g.settlement_to_bank[settlement.settlement_id]
            txn = self.bank_by_id[bank_id]
            if txn.credit_paise < 200:
                continue

            first = txn.credit_paise // 3
            second = txn.credit_paise - first
            txn.credit_paise = first
            sibling = BankTxn(
                bank_txn_id=f"{txn.bank_txn_id}b",
                value_date=txn.value_date,
                narration=txn.narration + "-PART2",
                credit_paise=second,
                debit_paise=0,
                balance_paise=0,
            )
            self.corpus.bank_txns.append(sibling)
            self.bank_by_id[sibling.bank_txn_id] = sibling

            self._record(
                Reason.PARTIAL_BANK_CREDIT,
                "settlement",
                settlement.settlement_id,
                0,
                "Paid out as two bank credits that together match the settlement.",
            )
            planted += 1

    def settlement_on_hold(self, count: int) -> None:
        """Withheld by the gateway. No bank credit, and that is expected.

        Deliberately shaped like :meth:`settled_not_in_bank` so that an engine
        which flags every creditless settlement as missing money is caught. The
        only thing separating the two is the settlement status.
        """
        pool = self._paid_settlements()
        self.rng.shuffle(pool)
        planted = 0
        for settlement in pool:
            if planted >= count:
                break
            if not self._claim(settlement.settlement_id):
                continue
            bank_id = self.g.settlement_to_bank[settlement.settlement_id]
            txn = self.bank_by_id.pop(bank_id)
            self.corpus.bank_txns.remove(txn)
            del self.g.settlement_to_bank[settlement.settlement_id]
            settlement.status = SettlementStatus.ON_HOLD
            settlement.utr = None

            self._record(
                Reason.SETTLEMENT_ON_HOLD,
                "settlement",
                settlement.settlement_id,
                settlement.net_paise,
                "Gateway placed the settlement on hold; no credit is due yet.",
            )
            planted += 1


#: Default fault load for a month of books. Roughly one defect per 120 records,
#: which is heavier than a healthy merchant but keeps every reason code
#: populated enough for per-code recall to carry a meaningful denominator.
#:
#: **Order is significant** and the dict is applied in insertion order. Entity
#: level faults run first, while every settlement still has exactly one bank
#: credit to absorb their propagation. Settlement level faults follow, and
#: ``partial_bank_credit`` runs last because it is the only injector that
#: changes the *number* of bank lines a settlement maps to -- anything
#: propagating to that settlement afterwards would adjust one half of a split
#: credit and quietly break the invariant.
DEFAULT_PLAN: dict[str, int] = {
    # entity level
    "mdr_overcharge": 40,
    "gst_miscalc": 18,
    "order_paid_not_settled": 12,
    "duplicate_payment": 10,
    "refund_double_counted": 8,
    "chargeback_netted_twice": 2,
    # settlement level
    "settlement_amount_mismatch": 3,
    "settled_not_in_bank": 3,
    "settlement_on_hold": 2,
    "late_settlement": 3,
    "partial_bank_credit": 3,
}


def inject(generated: Generated, *, seed: int, plan: dict[str, int] | None = None) -> Generated:
    """Apply a fault plan to a generated corpus and return it, labelled."""
    injector = Injector(generated, seed)
    for name, count in (plan or DEFAULT_PLAN).items():
        if count <= 0:
            continue
        getattr(injector, name)(count)
    rebalance(generated.corpus)
    return generated
