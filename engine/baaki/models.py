"""Domain model for a merchant's month-end settlement reconciliation.

The chain Baaki reconciles, and the identifier that joins each hop:

    Order  --order_id-->  Payment  --payment_id-->  SettlementRow
           --settlement_id-->  Settlement  --utr-->  BankTxn

Every hop that fails to join is money the merchant cannot account for. The
reason codes in :class:`Reason` name each distinct way the chain breaks.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class Method(str, enum.Enum):
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"


class Network(str, enum.Enum):
    RUPAY = "rupay"
    VISA = "visa"
    MASTERCARD = "mastercard"
    AMEX = "amex"
    NONE = "none"


class PaymentStatus(str, enum.Enum):
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"


class SettlementStatus(str, enum.Enum):
    PROCESSED = "processed"
    ON_HOLD = "on_hold"


class EntityType(str, enum.Enum):
    PAYMENT = "payment"
    REFUND = "refund"
    DISPUTE = "dispute"
    ADJUSTMENT = "adjustment"


class Severity(str, enum.Enum):
    """How much an analyst should care, independent of rupee value."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Reason(str, enum.Enum):
    """Exception taxonomy.

    Each code is one distinct failure of the order->bank chain. The engine must
    be able to state, for every rupee it flags, which of these it is and what
    evidence rows support it. "Something looks off" is not a reason code.
    """

    MDR_OVERCHARGE = "MDR_OVERCHARGE"
    GST_MISCALC = "GST_MISCALC"
    ORDER_PAID_NOT_SETTLED = "ORDER_PAID_NOT_SETTLED"
    SETTLED_NOT_IN_BANK = "SETTLED_NOT_IN_BANK"
    BANK_CREDIT_UNIDENTIFIED = "BANK_CREDIT_UNIDENTIFIED"
    SETTLEMENT_AMOUNT_MISMATCH = "SETTLEMENT_AMOUNT_MISMATCH"
    REFUND_DOUBLE_COUNTED = "REFUND_DOUBLE_COUNTED"
    CHARGEBACK_NETTED_TWICE = "CHARGEBACK_NETTED_TWICE"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    LATE_SETTLEMENT = "LATE_SETTLEMENT"
    PARTIAL_BANK_CREDIT = "PARTIAL_BANK_CREDIT"
    SETTLEMENT_ON_HOLD = "SETTLEMENT_ON_HOLD"


#: Analyst-facing metadata per reason code. ``recoverable`` marks the codes
#: whose rupee impact is money the merchant can actually claw back, as opposed
#: to timing differences that only cost working capital.
REASON_META: dict[Reason, dict] = {
    Reason.MDR_OVERCHARGE: {
        "severity": Severity.HIGH,
        "recoverable": True,
        "title": "Gateway fee charged above contracted rate",
        "action": "Raise a fee-correction ticket with the gateway citing the contract rate.",
    },
    Reason.GST_MISCALC: {
        "severity": Severity.HIGH,
        "recoverable": True,
        "title": "GST not 18% of fee",
        "action": "Request a revised tax invoice; the input-credit claim is wrong until fixed.",
    },
    Reason.ORDER_PAID_NOT_SETTLED: {
        "severity": Severity.CRITICAL,
        "recoverable": True,
        "title": "Customer paid but money never settled",
        "action": "Escalate with payment_id; funds are held or lost.",
    },
    Reason.SETTLED_NOT_IN_BANK: {
        "severity": Severity.CRITICAL,
        "recoverable": True,
        "title": "Settlement issued but no matching bank credit",
        "action": "Trace the UTR with the bank; possible failed or returned transfer.",
    },
    Reason.BANK_CREDIT_UNIDENTIFIED: {
        "severity": Severity.MEDIUM,
        "recoverable": False,
        "title": "Bank credit not attributable to any settlement",
        "action": "Classify the source before booking it as revenue.",
    },
    Reason.SETTLEMENT_AMOUNT_MISMATCH: {
        "severity": Severity.CRITICAL,
        "recoverable": True,
        "title": "Settlement net does not equal the sum of its rows",
        "action": "Request the gateway line-level breakup for this settlement_id.",
    },
    Reason.REFUND_DOUBLE_COUNTED: {
        "severity": Severity.CRITICAL,
        "recoverable": True,
        "title": "Same refund deducted in two settlements",
        "action": "Claim the duplicate deduction back.",
    },
    Reason.CHARGEBACK_NETTED_TWICE: {
        "severity": Severity.CRITICAL,
        "recoverable": True,
        "title": "Same chargeback deducted in two settlements",
        "action": "Claim the duplicate deduction back.",
    },
    Reason.DUPLICATE_PAYMENT: {
        "severity": Severity.HIGH,
        "recoverable": False,
        "title": "One order captured twice",
        "action": "Refund the customer before they raise a chargeback.",
    },
    Reason.LATE_SETTLEMENT: {
        "severity": Severity.LOW,
        "recoverable": False,
        "title": "Settled beyond the contracted T+2 window",
        "action": "Track against SLA; cost is working capital, not principal.",
    },
    Reason.PARTIAL_BANK_CREDIT: {
        "severity": Severity.MEDIUM,
        "recoverable": False,
        "title": "Settlement arrived split across multiple bank credits",
        "action": "Confirm the parts sum to the settlement; usually benign.",
    },
    Reason.SETTLEMENT_ON_HOLD: {
        "severity": Severity.MEDIUM,
        "recoverable": False,
        "title": "Settlement withheld by the gateway",
        "action": "Confirm the hold reason and expected release date.",
    },
}


@dataclass(slots=True)
class FeeContract:
    """The rate card the merchant actually signed.

    Reconciliation without a contract is just addition. The contract is what
    turns "the gateway charged 2.4%" into "the gateway overcharged 0.4%".
    """

    method: Method
    network: Network
    rate_bps: int
    fixed_paise: int = 0


@dataclass(slots=True)
class Order:
    order_id: str
    amount_paise: int
    customer_id: str
    created_at: datetime
    status: str = "paid"
    channel: str = "web"


@dataclass(slots=True)
class Payment:
    payment_id: str
    order_id: str
    amount_paise: int
    method: Method
    network: Network
    status: PaymentStatus
    fee_paise: int
    tax_paise: int
    captured_at: datetime
    international: bool = False


@dataclass(slots=True)
class Refund:
    refund_id: str
    payment_id: str
    amount_paise: int
    created_at: datetime
    speed: str = "normal"


@dataclass(slots=True)
class Dispute:
    dispute_id: str
    payment_id: str
    amount_paise: int
    status: str
    raised_at: datetime


@dataclass(slots=True)
class SettlementRow:
    """One line of the gateway settlement recon report."""

    settlement_id: str
    entity_type: EntityType
    entity_id: str
    gross_paise: int
    fee_paise: int
    tax_paise: int
    net_paise: int


@dataclass(slots=True)
class Settlement:
    settlement_id: str
    utr: str | None
    net_paise: int
    fees_paise: int
    tax_paise: int
    status: SettlementStatus
    created_at: datetime


@dataclass(slots=True)
class BankTxn:
    """A line off the merchant's bank statement.

    ``narration`` is free text written by the remitting bank. It is the single
    dirtiest field in the whole pipeline and the reason a purely deterministic
    matcher cannot close the last few percent.
    """

    bank_txn_id: str
    value_date: date
    narration: str
    credit_paise: int
    debit_paise: int
    balance_paise: int


@dataclass(slots=True)
class Corpus:
    """One month of a merchant's books, from all four sources."""

    orders: list[Order] = field(default_factory=list)
    payments: list[Payment] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    disputes: list[Dispute] = field(default_factory=list)
    settlement_rows: list[SettlementRow] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    bank_txns: list[BankTxn] = field(default_factory=list)
    contracts: list[FeeContract] = field(default_factory=list)

    def record_count(self) -> int:
        return (
            len(self.orders)
            + len(self.payments)
            + len(self.refunds)
            + len(self.disputes)
            + len(self.settlement_rows)
            + len(self.settlements)
            + len(self.bank_txns)
        )


@dataclass(slots=True)
class InjectedDefect:
    """Ground truth.

    The corpus generator writes these; the matcher never reads them. Recall is
    measured by asking which of these the matcher independently rediscovered.
    """

    defect_id: str
    reason: Reason
    entity_type: str
    entity_id: str
    impact_paise: int
    note: str = ""
