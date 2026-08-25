"""The merchant's commercial terms.

This module is shared ground, and deliberately so. The corpus generator applies
these terms to produce a month of books; the engine applies the same terms to
verify them. That is not circular, it is the whole point of reconciliation --
the contract is the merchant's, agreed before either side ran, and both the
gateway and the auditor are measured against it.

What the engine must *not* import is anything the adversary knows about how the
books were actually constructed: which bank line paid which settlement, which
narration template was used, or where a fault was planted. Those live in
:mod:`baaki.corpus` and never cross into :mod:`baaki.match`.
"""

from __future__ import annotations

from .models import FeeContract, Method, Network

#: The merchant's contracted rate card.
#:
#: UPI carries nil MDR for person-to-merchant transactions in India, which
#: makes *any* fee on a UPI payment a finding rather than a rounding argument.
#: RuPay is priced below the international schemes and Amex above them, so
#: verifying a fee means looking up (method, network, international) rather
#: than applying one blended rate.
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

#: Contracted settlement window. A payment captured on day T is due in the
#: merchant's bank account on T+2. Anything later is an SLA breach and a
#: working-capital cost, but not a loss of principal.
SETTLEMENT_LAG_DAYS = 2


def contract_for(
    contracts: list[FeeContract], method: Method, network: Network, international: bool
) -> tuple[int, int]:
    """Return the ``(rate_bps, fixed_paise)`` the merchant actually signed for."""
    for c in contracts:
        if c.method is method and c.network is network:
            rate = c.rate_bps + (INTERNATIONAL_SURCHARGE_BPS if international else 0)
            return rate, c.fixed_paise
    raise KeyError(f"no contracted rate for {method.value}/{network.value}")
