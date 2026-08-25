"""Synthetic corpus generator: one month of a merchant's books.

This module is the *adversary*, not part of the engine. It writes four sources
that a real merchant would export -- order ledger, payment report, settlement
recon report, bank statement -- plus a ground-truth list of every condition the
engine is expected to surface.

Two rules keep the evaluation honest:

1. The generator and the matcher share no code. The generator formats bank
   narrations from templates; :mod:`baaki.match.fuzzy` parses them back with an
   independent implementation that has never seen the templates.
2. Nothing flaggable appears in the corpus without a corresponding
   :class:`~baaki.models.InjectedDefect`. A clean corpus must produce an empty
   exception queue -- that is what makes the false-positive count meaningful.

Everything is driven by an explicit seed. Same seed in, byte-identical corpus
out, on any machine.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

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
from ..money import compute_fee, compute_gst

# ---------------------------------------------------------------------------
# Rate card
# ---------------------------------------------------------------------------

#: The merchant's contracted rate card. UPI carries nil MDR for person-to-
#: merchant transactions in India, which makes *any* fee on a UPI payment a
#: finding rather than a rounding argument. RuPay is priced below the
#: international schemes, and Amex above them. Reconciling a fee therefore
#: requires looking up (method, network, international) -- not a flat rate.
DEFAULT_CONTRACTS: list[FeeContract] = [
    FeeContract(Method.UPI, Network.NONE, rate_bps=0, fixed_paise=0),
    FeeContract(Method.NETBANKING, Network.NONE, rate_bps=190, fixed_paise=0),
    FeeContract(Method.WALLET, Network.NONE, rate_bps=200, fixed_paise=0),
    FeeContract(Method.CARD, Network.RUPAY, rate_bps=100, fixed_paise=0),
    FeeContract(Method.CARD, Network.VISA, rate_bps=200, fixed_paise=0),
    FeeContract(Method.CARD, Network.MASTERCARD, rate_bps=200, fixed_paise=0),
    FeeContract(Method.CARD, Network.AMEX, rate_bps=300, fixed_paise=0),
]

#: Cross-border transactions carry a surcharge on top of the domestic rate.
INTERNATIONAL_SURCHARGE_BPS = 130

#: Contracted settlement window. Capture on day T lands in the bank on T+2.
SETTLEMENT_LAG_DAYS = 2

METHOD_WEIGHTS = [(Method.UPI, 0.62), (Method.CARD, 0.24), (Method.NETBANKING, 0.09), (Method.WALLET, 0.05)]
CARD_NETWORK_WEIGHTS = [
    (Network.VISA, 0.40),
    (Network.MASTERCARD, 0.33),
    (Network.RUPAY, 0.23),
    (Network.AMEX, 0.04),
]

BANK_CODES = ["HDFC", "ICIC", "UTIB", "SBIN", "KKBK"]


def contract_for(
    contracts: list[FeeContract], method: Method, network: Network, international: bool
) -> tuple[int, int]:
    """Return the ``(rate_bps, fixed_paise)`` the merchant actually signed for."""
    for c in contracts:
        if c.method is method and c.network is network:
            rate = c.rate_bps + (INTERNATIONAL_SURCHARGE_BPS if international else 0)
            return rate, c.fixed_paise
    raise KeyError(f"no contracted rate for {method.value}/{network.value}")


# ---------------------------------------------------------------------------
# Bank narration templates
# ---------------------------------------------------------------------------

#: How remitting banks actually describe an inbound settlement. Difficulty
#: rises down the list. The last template carries no UTR at all, which is the
#: case no regular expression can close and the reason the pipeline has an
#: LLM stage.
NARRATION_TEMPLATES: list[tuple[str, str]] = [
    ("clean", "NEFT-{utr}-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT"),
    ("clean", "IMPS/{utr}/RAZORPAY/SETL"),
    ("clean", "RTGS {utr} RAZORPAY SOFTWARE PRIVATE LIMITED"),
    ("spaced", "NEFT CR {utr} RAZORPAY  SOFTWARE   PVT  LTD"),
    ("lowercase", "neft-{utr}-razorpay software pvt ltd-payout"),
    ("truncated", "NEFT-{utr_trunc}-RAZORPAY SOFT"),
    ("noisy", "NEFT*{utr}*RZPY SOFTWARE PVTLTD*SETTLEMENT*IN"),
    ("no_utr", "NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT PAYOUT"),
]

#: Credits that land in the same bank account but are not gateway settlements.
#: A real statement is full of these and an engine that flags none of them is
#: not reading the statement; an engine that silently books them as revenue is
#: worse.
NOISE_NARRATIONS: list[str] = [
    "NEFT-{ref}-CASHFREE PAYMENTS INDIA PVT LTD-SETTLEMENT",
    "NEFT-{ref}-{customer} PRIVATE LIMITED-INV{inv}",
    "INT.PD:{ref}:SAVINGS INTEREST CREDIT",
    "IMPS/{ref}/{customer}/ADVANCE",
    "RTGS {ref} {customer} LLP VENDOR REFUND",
]

CUSTOMER_NAMES = [
    "ARORA TEXTILES",
    "MEHTA TRADING",
    "SUNRISE LOGISTICS",
    "KRISHNA ENTERPRISES",
    "NORTHPOINT RETAIL",
    "VELLORE FOODS",
]


def _make_utr(rng: random.Random, when: date) -> str:
    """Build an RBI-style 22-character UTR: bank(4) + N + YYYYMMDD + seq(9)."""
    bank = rng.choice(BANK_CODES)
    seq = rng.randrange(10**8, 10**9)
    return f"{bank}N{when:%Y%m%d}{seq}"


def _weighted(rng: random.Random, weights: list[tuple]) -> object:
    roll = rng.random()
    cumulative = 0.0
    for value, weight in weights:
        cumulative += weight
        if roll <= cumulative:
            return value
    return weights[-1][0]


def _order_amount_paise(rng: random.Random) -> int:
    """Order values: heavy mass around a few hundred rupees, a long right tail."""
    magnitude = rng.lognormvariate(6.6, 0.95)
    rupees_ = min(max(magnitude, 49.0), 250_000.0)
    return int(round(rupees_ * 100))


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def generate(
    *,
    seed: int,
    n_orders: int = 4_000,
    month_start: date = date(2026, 7, 1),
    days: int = 31,
    refund_rate: float = 0.06,
    dispute_rate: float = 0.004,
    failure_rate: float = 0.11,
    noise_credits: int = 18,
) -> tuple[Corpus, list[InjectedDefect]]:
    """Generate one internally consistent month of books.

    Returns the corpus and the ground-truth findings that a correct engine must
    surface from it. A corpus straight out of this function contains no
    arithmetic defects; the only expected findings are the unattributable bank
    credits, which are a fact of every real statement rather than an error.
    Fault injection is :mod:`baaki.corpus.defects`.
    """
    rng = random.Random(seed)
    corpus = Corpus(contracts=list(DEFAULT_CONTRACTS))
    truth: list[InjectedDefect] = []

    # -- orders and payments ------------------------------------------------
    for i in range(n_orders):
        day_offset = rng.randrange(days)
        created = datetime.combine(
            month_start + timedelta(days=day_offset),
            datetime.min.time(),
        ) + timedelta(seconds=rng.randrange(86_400))

        order_id = f"order_{seed:04d}{i:06d}"
        amount = _order_amount_paise(rng)
        order = Order(
            order_id=order_id,
            amount_paise=amount,
            customer_id=f"cust_{rng.randrange(1, max(2, n_orders // 3)):06d}",
            created_at=created,
            status="paid",
            channel=rng.choice(["web", "web", "app", "app", "pos"]),
        )

        failed = rng.random() < failure_rate
        method = _weighted(rng, METHOD_WEIGHTS)
        network = _weighted(rng, CARD_NETWORK_WEIGHTS) if method is Method.CARD else Network.NONE
        international = method is Method.CARD and rng.random() < 0.03

        if failed:
            order.status = "attempted"
            corpus.orders.append(order)
            corpus.payments.append(
                Payment(
                    payment_id=f"pay_{seed:04d}{i:06d}",
                    order_id=order_id,
                    amount_paise=amount,
                    method=method,
                    network=network,
                    status=PaymentStatus.FAILED,
                    fee_paise=0,
                    tax_paise=0,
                    captured_at=created + timedelta(seconds=rng.randrange(30, 900)),
                    international=international,
                )
            )
            continue

        rate_bps, fixed = contract_for(corpus.contracts, method, network, international)
        fee = compute_fee(amount, rate_bps, fixed)
        tax = compute_gst(fee)

        corpus.orders.append(order)
        corpus.payments.append(
            Payment(
                payment_id=f"pay_{seed:04d}{i:06d}",
                order_id=order_id,
                amount_paise=amount,
                method=method,
                network=network,
                status=PaymentStatus.CAPTURED,
                fee_paise=fee,
                tax_paise=tax,
                captured_at=created + timedelta(seconds=rng.randrange(30, 900)),
                international=international,
            )
        )

    captured = [p for p in corpus.payments if p.status is PaymentStatus.CAPTURED]

    # -- refunds and disputes ----------------------------------------------
    for payment in captured:
        if rng.random() < refund_rate:
            full = rng.random() < 0.7
            amount = payment.amount_paise if full else int(payment.amount_paise * rng.uniform(0.2, 0.8))
            corpus.refunds.append(
                Refund(
                    refund_id=f"rfnd_{payment.payment_id[4:]}",
                    payment_id=payment.payment_id,
                    amount_paise=amount,
                    created_at=payment.captured_at + timedelta(days=rng.randrange(1, 12)),
                    speed=rng.choice(["normal", "normal", "instant"]),
                )
            )
        elif rng.random() < dispute_rate:
            corpus.disputes.append(
                Dispute(
                    dispute_id=f"disp_{payment.payment_id[4:]}",
                    payment_id=payment.payment_id,
                    amount_paise=payment.amount_paise,
                    status="open",
                    raised_at=payment.captured_at + timedelta(days=rng.randrange(3, 25)),
                )
            )

    # -- settlements --------------------------------------------------------
    # A settlement batches everything whose settlement date is the same day:
    # captured payments net of fee and tax, minus refunds and chargebacks that
    # became due that day.
    buckets: dict[date, list[tuple[EntityType, object]]] = {}

    for payment in captured:
        settle_on = (payment.captured_at + timedelta(days=SETTLEMENT_LAG_DAYS)).date()
        buckets.setdefault(settle_on, []).append((EntityType.PAYMENT, payment))
    for refund in corpus.refunds:
        settle_on = (refund.created_at + timedelta(days=1)).date()
        buckets.setdefault(settle_on, []).append((EntityType.REFUND, refund))
    for dispute in corpus.disputes:
        settle_on = (dispute.raised_at + timedelta(days=1)).date()
        buckets.setdefault(settle_on, []).append((EntityType.DISPUTE, dispute))

    for idx, settle_date in enumerate(sorted(buckets)):
        settlement_id = f"setl_{seed:04d}{idx:05d}"
        rows: list[SettlementRow] = []
        net_total = 0
        fee_total = 0
        tax_total = 0

        for entity_type, entity in buckets[settle_date]:
            if entity_type is EntityType.PAYMENT:
                net = entity.amount_paise - entity.fee_paise - entity.tax_paise
                rows.append(
                    SettlementRow(
                        settlement_id=settlement_id,
                        entity_type=EntityType.PAYMENT,
                        entity_id=entity.payment_id,
                        gross_paise=entity.amount_paise,
                        fee_paise=entity.fee_paise,
                        tax_paise=entity.tax_paise,
                        net_paise=net,
                    )
                )
                fee_total += entity.fee_paise
                tax_total += entity.tax_paise
            elif entity_type is EntityType.REFUND:
                net = -entity.amount_paise
                rows.append(
                    SettlementRow(
                        settlement_id=settlement_id,
                        entity_type=EntityType.REFUND,
                        entity_id=entity.refund_id,
                        gross_paise=-entity.amount_paise,
                        fee_paise=0,
                        tax_paise=0,
                        net_paise=net,
                    )
                )
            else:
                net = -entity.amount_paise
                rows.append(
                    SettlementRow(
                        settlement_id=settlement_id,
                        entity_type=EntityType.DISPUTE,
                        entity_id=entity.dispute_id,
                        gross_paise=-entity.amount_paise,
                        fee_paise=0,
                        tax_paise=0,
                        net_paise=net,
                    )
                )
            net_total += net

        corpus.settlement_rows.extend(rows)
        corpus.settlements.append(
            Settlement(
                settlement_id=settlement_id,
                utr=_make_utr(rng, settle_date),
                net_paise=net_total,
                fees_paise=fee_total,
                tax_paise=tax_total,
                status=SettlementStatus.PROCESSED,
                created_at=datetime.combine(settle_date, datetime.min.time()) + timedelta(hours=11),
            )
        )

    # -- bank statement -----------------------------------------------------
    # Credits are emitted in value-date order so the running balance is
    # coherent, the way a downloaded statement would be. Each line carries the
    # index of the ground-truth finding it belongs to (or ``None``), so the
    # link survives the sort instead of being re-derived from the narration.
    lines: list[tuple[date, str, int, int, int | None]] = []

    for settlement in corpus.settlements:
        if settlement.net_paise <= 0:
            # A wholly negative batch is carried forward rather than debited.
            continue
        _style, template = rng.choice(NARRATION_TEMPLATES)
        utr = settlement.utr or ""
        narration = template.format(utr=utr, utr_trunc=utr[:16])
        lines.append((settlement.created_at.date(), narration, settlement.net_paise, 0, None))

    for n in range(noise_credits):
        when = month_start + timedelta(days=rng.randrange(days))
        template = rng.choice(NOISE_NARRATIONS)
        narration = template.format(
            ref=f"{rng.choice(BANK_CODES)}N{when:%Y%m%d}{rng.randrange(10**8, 10**9)}",
            customer=rng.choice(CUSTOMER_NAMES),
            inv=rng.randrange(1000, 9999),
        )
        amount = int(round(rng.lognormvariate(9.4, 1.1) * 100))
        truth.append(
            InjectedDefect(
                defect_id=f"noise_{seed:04d}_{n:03d}",
                reason=Reason.BANK_CREDIT_UNIDENTIFIED,
                entity_type="bank_txn",
                entity_id="",  # filled in once the bank_txn_id is assigned
                impact_paise=amount,
                note="Credit from a source other than the gateway.",
            )
        )
        lines.append((when, narration, amount, 0, len(truth) - 1))

    lines.sort(key=lambda row: (row[0], row[1], row[2]))
    balance = 5_000_000
    for n, (value_date, narration, credit, debit, truth_idx) in enumerate(lines):
        balance += credit - debit
        txn = BankTxn(
            bank_txn_id=f"bank_{seed:04d}{n:06d}",
            value_date=value_date,
            narration=narration,
            credit_paise=credit,
            debit_paise=debit,
            balance_paise=balance,
        )
        corpus.bank_txns.append(txn)
        if truth_idx is not None:
            truth[truth_idx].entity_id = txn.bank_txn_id

    return corpus, truth
