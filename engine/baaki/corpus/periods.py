"""Generating the *next* month, including what came back from the last one.

The recovery loop only means something if the repayments are really there to be
found. So a second cycle is an ordinary month of books plus adjustment lines
that repay some of the claims filed in the first -- and the generator writes
down exactly which ones it repaid, so the verifier can be scored rather than
trusted.

Repayments arrive the way gateways actually issue them: as an ``adjustment``
line inside a later settlement, which lifts that settlement's net and the bank
credit that pays it. Some quote the original entity in their reference and some
do not, because both happen, and the ones that do not are what force the
verifier to match on value and timing instead.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..models import EntityType, SettlementRow
from ..recovery.claims import ClaimState, Ledger
from .generate import Generated, generate, rebalance

#: How often a filed claim is repaid at all, by reason. Deliberately *not* the
#: same numbers as the triage priors -- triage should have to learn the real
#: behaviour from observation rather than being handed it.
REPAY_PROBABILITY: dict[str, float] = {
    "MDR_OVERCHARGE": 0.90,
    "GST_MISCALC": 0.86,
    "REFUND_DOUBLE_COUNTED": 0.72,
    "SETTLEMENT_AMOUNT_MISMATCH": 0.60,
    "CHARGEBACK_NETTED_TWICE": 0.50,
    "ORDER_PAID_NOT_SETTLED": 0.42,
    "SETTLED_NOT_IN_BANK": 0.30,
}
DEFAULT_REPAY = 0.5

#: Share of repayments that quote the original entity in the adjustment
#: reference. The rest arrive as a bare credit of the right amount.
REFERENCED_SHARE = 0.7

#: Share of repayments that come back short.
PARTIAL_SHARE = 0.12


@dataclass(slots=True)
class Cycle:
    """One settlement period, plus the truth about what it repaid."""

    generated: Generated
    label: str
    starts_on: date
    #: claim_id -> paise actually repaid in this cycle. The verifier never sees
    #: this; it is the answer key for scoring the recovery loop.
    repaid: dict[str, int] = field(default_factory=dict)
    #: claim_id -> the adjustment entity_id that carries the repayment.
    adjustment_of: dict[str, str] = field(default_factory=dict)


def next_cycle(
    ledger: Ledger,
    *,
    seed: int,
    month_start: date,
    label: str,
    n_orders: int = 4_000,
    plan: dict | None = None,
) -> Cycle:
    """Generate the following month, repaying some of what was filed.

    Only claims in ``FILED`` are eligible. Something never filed cannot come
    back, and pretending otherwise would let the verifier score points on
    claims the triage step deliberately dropped.
    """
    from .defects import inject

    g = generate(seed=seed, n_orders=n_orders, month_start=month_start)
    if plan:
        g = inject(g, seed=seed, plan=plan)

    rng = random.Random(seed * 104_729 + 7)
    cycle = Cycle(generated=g, label=label, starts_on=month_start)

    eligible = [c for c in ledger.claims.values() if c.state == ClaimState.FILED.value]
    if not eligible:
        return cycle

    # Only settlements that actually paid out can carry an adjustment: the
    # repayment has to reach the bank to be a repayment.
    payable = [
        s
        for s in g.corpus.settlements
        if s.settlement_id in g.settlement_to_bank and s.net_paise > 0
    ]
    if not payable:
        return cycle

    bank_by_id = {b.bank_txn_id: b for b in g.corpus.bank_txns}
    rows_by_settlement: dict[str, list[SettlementRow]] = {}
    for row in g.corpus.settlement_rows:
        rows_by_settlement.setdefault(row.settlement_id, []).append(row)

    for claim in eligible:
        probability = REPAY_PROBABILITY.get(claim.reason, DEFAULT_REPAY)
        if rng.random() > probability:
            continue

        amount = claim.outstanding_paise
        if amount <= 0:
            continue
        if rng.random() < PARTIAL_SHARE:
            amount = int(amount * rng.uniform(0.35, 0.8))
            if amount <= 0:
                continue

        settlement = rng.choice(payable)
        referenced = rng.random() < REFERENCED_SHARE
        entity_id = (
            f"adj_{claim.entity_id}"
            if referenced
            else f"adj_{rng.randrange(10**9, 10**10)}"
        )

        row = SettlementRow(
            settlement_id=settlement.settlement_id,
            entity_type=EntityType.ADJUSTMENT,
            entity_id=entity_id,
            gross_paise=amount,
            fee_paise=0,
            tax_paise=0,
            net_paise=amount,
        )
        g.corpus.settlement_rows.append(row)
        rows_by_settlement.setdefault(settlement.settlement_id, []).append(row)

        # Keep the books coherent: the header and the bank credit move with the
        # line, so the repayment does not read as a settlement mismatch.
        settlement.net_paise += amount
        bank_id = g.settlement_to_bank[settlement.settlement_id]
        if bank_id in bank_by_id:
            bank_by_id[bank_id].credit_paise += amount

        cycle.repaid[claim.claim_id] = amount
        cycle.adjustment_of[claim.claim_id] = entity_id

    rebalance(g.corpus)
    return cycle


def month_after(start: date) -> date:
    """First day of the month following ``start``."""
    return date(start.year + (start.month == 12), (start.month % 12) + 1, 1)


def cycle_window(start: date) -> tuple[date, date]:
    end = month_after(start) - timedelta(days=1)
    return start, end
