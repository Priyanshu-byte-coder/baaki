"""Stage 1a: recompute every number the gateway reported.

No matching happens here, and no model is involved. The contracted rate card is
applied to each captured payment and the result is compared against what was
actually billed. A settlement header is compared against the sum of its own
lines.

This stage is deliberately the first one written and the first one run. It
catches the defects that cost the most and are the least visible -- a fee
forty-five basis points over contract is invisible on any single transaction --
and it does so with arithmetic that an analyst can redo by hand from the
evidence attached to the finding.

There is no threshold to tune here and no confidence below 1.0. Either the
number reconciles or it does not.
"""

from __future__ import annotations

from collections import defaultdict

from ..contract import INTERNATIONAL_SURCHARGE_BPS
from ..models import Corpus, PaymentStatus, Reason
from ..money import compute_fee, compute_gst, effective_rate_bps, rupees
from .findings import Evidence, Finding, Stage

#: Tolerance for fee and tax comparison, in paise. Zero: fees are computed by
#: a documented rule with documented rounding, so any difference is real. A
#: tolerance here would be a place for a systematic overcharge to hide.
FEE_TOLERANCE_PAISE = 0

#: Tolerance when summing a settlement's lines against its header. Also zero,
#: for the same reason -- every line is an integer count of paise.
SETTLEMENT_TOLERANCE_PAISE = 0


def _contract_lookup(corpus: Corpus) -> dict[tuple[str, str], tuple[int, int]]:
    return {(c.method.value, c.network.value): (c.rate_bps, c.fixed_paise) for c in corpus.contracts}


def check_fees(corpus: Corpus) -> list[Finding]:
    """Recompute fee and GST for every captured payment from the rate card.

    Two distinct defects fall out of the same pass:

    ``MDR_OVERCHARGE``
        The fee itself exceeds the contracted rate for this
        (method, network, international) combination. Note that UPI carries a
        nil contracted rate, so any fee at all on a UPI payment is a finding
        rather than a rounding argument.

    ``GST_MISCALC``
        Tax is not 18% of the fee. The usual cause is 18% of *gross*, which is
        both much larger and a wrong input-tax-credit claim.

    The two are reported separately even when they land on the same payment,
    because they are recovered through different conversations: one is a fee
    correction, the other is a reissued tax invoice.
    """
    contracts = _contract_lookup(corpus)
    findings: list[Finding] = []

    for payment in corpus.payments:
        if payment.status is not PaymentStatus.CAPTURED:
            continue

        rate = contracts.get((payment.method.value, payment.network.value))
        if rate is None:
            continue
        rate_bps, fixed = rate
        if payment.international:
            rate_bps += INTERNATIONAL_SURCHARGE_BPS

        expected_fee = compute_fee(payment.amount_paise, rate_bps, fixed)
        fee_delta = payment.fee_paise - expected_fee

        if fee_delta > FEE_TOLERANCE_PAISE:
            billed_bps = effective_rate_bps(payment.amount_paise, payment.fee_paise)
            expected_tax = compute_gst(expected_fee)
            impact = fee_delta + (compute_gst(payment.fee_paise) - expected_tax)
            findings.append(
                Finding(
                    reason=Reason.MDR_OVERCHARGE,
                    entity_type="payment",
                    entity_id=payment.payment_id,
                    impact_paise=impact,
                    stage=Stage.DETERMINISTIC,
                    confidence=1.0,
                    explanation=(
                        f"Billed {billed_bps}bps against a contracted {rate_bps}bps on "
                        f"{payment.method.value}/{payment.network.value}"
                        f"{' international' if payment.international else ''}. "
                        f"Fee {rupees(payment.fee_paise)} against an expected "
                        f"{rupees(expected_fee)}, a difference of {rupees(fee_delta)} "
                        f"before tax."
                    ),
                    evidence=[
                        Evidence("payments", payment.payment_id, "amount", rupees(payment.amount_paise)),
                        Evidence("payments", payment.payment_id, "fee", rupees(payment.fee_paise)),
                        Evidence(
                            "contract",
                            f"{payment.method.value}/{payment.network.value}",
                            "rate_bps",
                            str(rate_bps),
                        ),
                        Evidence("computed", payment.payment_id, "expected_fee", rupees(expected_fee)),
                    ],
                )
            )

        # GST is checked against the fee that was actually billed, not against
        # the fee that should have been. Otherwise a single overcharge would be
        # reported twice, once as fee and again as a tax discrepancy it caused.
        expected_tax_on_billed = compute_gst(payment.fee_paise)
        tax_delta = payment.tax_paise - expected_tax_on_billed

        if abs(tax_delta) > FEE_TOLERANCE_PAISE:
            on_gross = compute_gst(payment.amount_paise)
            cause = (
                "Tax equals 18% of the gross transaction value rather than 18% of the fee."
                if payment.tax_paise == on_gross
                else "Tax is not 18% of the billed fee."
            )
            findings.append(
                Finding(
                    reason=Reason.GST_MISCALC,
                    entity_type="payment",
                    entity_id=payment.payment_id,
                    impact_paise=tax_delta,
                    stage=Stage.DETERMINISTIC,
                    confidence=1.0,
                    explanation=(
                        f"{cause} Charged {rupees(payment.tax_paise)} against an expected "
                        f"{rupees(expected_tax_on_billed)} on a fee of {rupees(payment.fee_paise)}."
                    ),
                    evidence=[
                        Evidence("payments", payment.payment_id, "fee", rupees(payment.fee_paise)),
                        Evidence("payments", payment.payment_id, "tax", rupees(payment.tax_paise)),
                        Evidence(
                            "computed",
                            payment.payment_id,
                            "expected_tax",
                            rupees(expected_tax_on_billed),
                        ),
                    ],
                )
            )

    return findings


def check_settlement_totals(corpus: Corpus) -> list[Finding]:
    """Compare each settlement header against the sum of its own lines.

    When these disagree the merchant is paid the header amount, so the
    difference is real money and the line-level breakup is the only place it
    shows up.
    """
    line_totals: dict[str, int] = defaultdict(int)
    line_counts: dict[str, int] = defaultdict(int)
    for row in corpus.settlement_rows:
        line_totals[row.settlement_id] += row.net_paise
        line_counts[row.settlement_id] += 1

    findings: list[Finding] = []
    for settlement in corpus.settlements:
        expected = line_totals[settlement.settlement_id]
        delta = settlement.net_paise - expected
        if abs(delta) <= SETTLEMENT_TOLERANCE_PAISE:
            continue

        direction = "below" if delta < 0 else "above"
        findings.append(
            Finding(
                reason=Reason.SETTLEMENT_AMOUNT_MISMATCH,
                entity_type="settlement",
                entity_id=settlement.settlement_id,
                impact_paise=abs(delta),
                stage=Stage.DETERMINISTIC,
                confidence=1.0,
                explanation=(
                    f"Header net {rupees(settlement.net_paise)} is {rupees(abs(delta))} "
                    f"{direction} the sum of its {line_counts[settlement.settlement_id]} "
                    f"lines, {rupees(expected)}."
                ),
                evidence=[
                    Evidence(
                        "settlements", settlement.settlement_id, "net", rupees(settlement.net_paise)
                    ),
                    Evidence(
                        "settlement_rows",
                        settlement.settlement_id,
                        "sum(net)",
                        rupees(expected),
                    ),
                    Evidence(
                        "settlement_rows",
                        settlement.settlement_id,
                        "line_count",
                        str(line_counts[settlement.settlement_id]),
                    ),
                ],
            )
        )
    return findings


def run(corpus: Corpus) -> list[Finding]:
    """Every arithmetic check, in one pass over the books."""
    return check_fees(corpus) + check_settlement_totals(corpus)
