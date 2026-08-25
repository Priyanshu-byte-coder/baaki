"""Money arithmetic for reconciliation.

Every amount in Baaki is an ``int`` count of paise. Floats are never used to
represent or compute money. A reconciliation engine that is wrong by one paise
on a 50,000-row batch produces an exception queue full of noise, and an analyst
who learns to ignore the queue.

Rounding follows the convention Indian payment gateways use when computing
fees and GST: round half up to the nearest paise.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

GST_BPS = 1800
"""GST on payment-gateway fees, in basis points (18%)."""

BPS_DIVISOR = 10_000


def rupees(paise: int) -> str:
    """Render paise as an Indian-format rupee string, e.g. ``₹12,34,567.89``."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}\u20b9{digits}.{frac:02d}"


def to_paise(amount: str | int | float | Decimal) -> int:
    """Parse a rupee amount into integer paise.

    Accepts floats only at the ingest boundary, where upstream CSV parsers hand
    us one; the value is routed through ``Decimal(str(...))`` so that 0.1 + 0.2
    style representation error cannot enter the ledger.
    """
    if isinstance(amount, int):
        return amount * 100
    dec = Decimal(str(amount))
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apply_bps(base_paise: int, bps: int) -> int:
    """Apply a basis-point rate to an amount, rounding half up."""
    product = Decimal(base_paise) * Decimal(bps) / Decimal(BPS_DIVISOR)
    return int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_fee(gross_paise: int, rate_bps: int, fixed_paise: int = 0) -> int:
    """Gateway fee for a transaction under a given contracted rate."""
    return apply_bps(gross_paise, rate_bps) + fixed_paise


def compute_gst(fee_paise: int) -> int:
    """GST is charged on the fee, never on the gross transaction value.

    Charging 18% of gross instead of 18% of fee is a real and expensive
    gateway-side defect; :mod:`baaki.corpus.defects` injects it deliberately and
    :mod:`baaki.match.arithmetic` is expected to catch every instance.
    """
    return apply_bps(fee_paise, GST_BPS)


def effective_rate_bps(gross_paise: int, fee_paise: int) -> int:
    """Back out the effective MDR in basis points from a charged fee."""
    if gross_paise == 0:
        return 0
    product = Decimal(fee_paise) * Decimal(BPS_DIVISOR) / Decimal(gross_paise)
    return int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
