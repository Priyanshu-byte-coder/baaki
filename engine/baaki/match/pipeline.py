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
class RunResult:
    findings: list[Finding] = field(default_factory=list)
    residue: Residue = field(default_factory=Residue)
    bank_stats: dict[str, int] = field(default_factory=dict)
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
    result.elapsed_total = result.elapsed_offline + (time.perf_counter() - started)
    return result
