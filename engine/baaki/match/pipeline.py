"""The four stages, in order, with the residue passed down between them.

Order is not incidental. Each stage is cheaper, more certain and more auditable
than the one after it, so every stage is given the chance to close an item
before anything less certain sees it. By the time the model is invoked, the
only things left are the ones that provably cannot be settled by arithmetic.

    1  arithmetic      recompute fees, tax and settlement totals
    2  deterministic   exact joins along the order-to-bank chain
    3  algorithmic     reference, value-and-date and split matching
    4  tail            the residue, model-assisted, guardrailed

Stages one to three need no network. If stage four cannot run, the report is
less explained and still correct.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from ..models import Corpus
from . import arithmetic, deterministic, fuzzy, llm
from .findings import Finding, Residue
from .llm import TailReport


@dataclass(slots=True)
class MatchRates:
    """Match rate, per hop of the chain and overall.

    Reported because it is the conventional measure of a reconciliation run and
    because it was asked for. It is deliberately **not** the headline anywhere
    in this project, for a reason the ablation makes concrete: after the first
    two stages the engine has matched 77% of the defects and located 6.5% of
    the money. Counts and rupees concentrate in opposite places, so a match rate
    quoted alone describes a run that may have found one rupee in fifteen.

    Broken out per hop rather than given as one number, because "94% matched"
    hides which join is failing, and the join that is failing is the whole
    diagnosis.
    """

    payments_total: int = 0
    payments_settled: int = 0
    settlements_total: int = 0
    settlements_banked: int = 0
    credits_total: int = 0
    credits_attributed: int = 0
    records_total: int = 0
    records_clean: int = 0

    @staticmethod
    def _rate(part: int, whole: int) -> float:
        return part / whole if whole else 1.0

    @property
    def payment_to_settlement(self) -> float:
        return self._rate(self.payments_settled, self.payments_total)

    @property
    def settlement_to_bank(self) -> float:
        return self._rate(self.settlements_banked, self.settlements_total)

    @property
    def bank_attribution(self) -> float:
        return self._rate(self.credits_attributed, self.credits_total)

    @property
    def overall(self) -> float:
        """Share of records not named in any exception."""
        return self._rate(self.records_clean, self.records_total)

    def as_dict(self) -> dict:
        return {
            "overall": self.overall,
            "payment_to_settlement": self.payment_to_settlement,
            "settlement_to_bank": self.settlement_to_bank,
            "bank_attribution": self.bank_attribution,
            "records_total": self.records_total,
            "records_clean": self.records_clean,
        }


def match_rates(corpus: Corpus, findings: list[Finding], bank_stats: dict[str, int]) -> MatchRates:
    """Compute match rate per hop from the corpus and the run's findings."""
    from ..models import EntityType, PaymentStatus, SettlementStatus

    captured = [p for p in corpus.payments if p.status is PaymentStatus.CAPTURED]
    settled_ids = {
        row.entity_id for row in corpus.settlement_rows if row.entity_type is EntityType.PAYMENT
    }

    # A settlement is only "expecting" a bank credit if it was actually paid
    # out: held settlements and wholly negative batches are excluded, because
    # counting them as unmatched would penalise the engine for being right.
    expecting = [
        s
        for s in corpus.settlements
        if s.status is SettlementStatus.PROCESSED and s.net_paise > 0
    ]
    unmatched_settlements = bank_stats.get("unmatched_settlements", 0)
    credits = [b for b in corpus.bank_txns if b.credit_paise > 0]
    unmatched_credits = bank_stats.get("unmatched_credits", 0)

    flagged = {f.entity_id for f in findings}

    return MatchRates(
        payments_total=len(captured),
        payments_settled=sum(1 for p in captured if p.payment_id in settled_ids),
        settlements_total=len(expecting),
        settlements_banked=len(expecting) - unmatched_settlements,
        credits_total=len(credits),
        credits_attributed=len(credits) - unmatched_credits,
        records_total=corpus.record_count(),
        records_clean=corpus.record_count() - len(flagged),
    )


@dataclass(slots=True)
class RunResult:
    findings: list[Finding] = field(default_factory=list)
    residue: Residue = field(default_factory=Residue)
    bank_stats: dict[str, int] = field(default_factory=dict)
    rates: MatchRates = field(default_factory=MatchRates)
    tail: TailReport = field(default_factory=TailReport)
    elapsed_offline: float = 0.0
    elapsed_total: float = 0.0

    def coverage(self) -> float:
        """Share of findings resolved without needing a person.

        Reported next to precision and never multiplied into it. A pipeline
        that escalates everything is useless and a pipeline that auto-closes
        everything is dangerous, and one number cannot say which you have.
        """
        if not self.findings:
            return 1.0
        automatic = sum(1 for f in self.findings if not f.requires_human)
        return automatic / len(self.findings)


def run_offline(corpus: Corpus) -> RunResult:
    """Stages one to three. No network, no model, fully reproducible."""
    started = time.perf_counter()
    result = RunResult()
    result.findings = arithmetic.run(corpus) + deterministic.run(corpus)
    bank_findings, residue, stats = fuzzy.run(corpus)
    result.findings += bank_findings
    result.residue = residue
    result.bank_stats = stats
    result.rates = match_rates(corpus, result.findings, stats)
    result.elapsed_offline = time.perf_counter() - started
    result.elapsed_total = result.elapsed_offline
    return result


def run(corpus: Corpus, *, client=None, max_concurrency: int = 2) -> RunResult:
    """The full pipeline. Falls back to the offline result when no model exists."""
    result = run_offline(corpus)
    if client is None or not client.available:
        result.tail.skipped = True
        return result

    started = time.perf_counter()
    outcome = asyncio.run(
        llm.resolve(corpus, result.residue, client, max_concurrency=max_concurrency)
    )
    result.findings = llm.apply(result.findings, outcome)
    result.tail = outcome.report
    result.rates = match_rates(corpus, result.findings, result.bank_stats)
    result.elapsed_total = result.elapsed_offline + (time.perf_counter() - started)
    return result
