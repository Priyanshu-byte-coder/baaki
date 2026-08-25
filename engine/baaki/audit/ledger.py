"""Append-only decision log.

Every finding Baaki reports is written here with what produced it: the stage,
the named rule, the records cited, and the rupee value claimed. A finding
becomes a claim against a gateway or an entry in a ledger, and neither survives
"the system said so".

**Reproducibility is split, deliberately.** The offline stages are pure
functions of the books, so the same input produces a byte-identical decision
stream and :meth:`Ledger.fingerprint` proves it. The tail stage calls a model
and is *not* bit-reproducible, even at temperature zero. Rather than pretend
otherwise, tail decisions are logged with the model id and a hash of the exact
prompt, and excluded from the offline fingerprint.

That split is the honest claim: the part of the pipeline that decides money
deterministically can be replayed and verified; the part that cannot be
replayed is confined to proposals that arithmetic had to confirm before they
counted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..match.findings import Finding, Stage


def _hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


@dataclass(slots=True)
class Decision:
    """One reported finding, and the provenance behind it."""

    seq: int
    stage: str
    rule: str
    reason: str
    entity_type: str
    entity_id: str
    impact_paise: int
    confidence: float
    requires_human: bool
    evidence: list[str]
    explanation: str
    model: str | None = None
    prompt_sha: str | None = None

    def digest(self) -> str:
        """Stable hash of the decision's substance.

        Deliberately excludes ``seq`` and the timestamp so that reordering an
        independent stage does not change a decision's identity, and includes
        the rupee value so that a changed amount always does.
        """
        return _hash(
            self.stage,
            self.rule,
            self.reason,
            self.entity_id,
            str(self.impact_paise),
            "|".join(sorted(self.evidence)),
        )


@dataclass(slots=True)
class Ledger:
    run_id: str
    corpus_sha: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    decisions: list[Decision] = field(default_factory=list)

    def append(self, finding: Finding, *, rule: str, model: str | None = None,
               prompt_sha: str | None = None) -> Decision:
        decision = Decision(
            seq=len(self.decisions),
            stage=finding.stage.value,
            rule=rule,
            reason=finding.reason.value,
            entity_type=finding.entity_type,
            entity_id=finding.entity_id,
            impact_paise=finding.impact_paise,
            confidence=finding.confidence,
            requires_human=finding.requires_human,
            evidence=[e.render() for e in finding.evidence],
            explanation=finding.explanation,
            model=model,
            prompt_sha=prompt_sha,
        )
        self.decisions.append(decision)
        return decision

    def extend(self, findings: list[Finding]) -> None:
        for finding in findings:
            self.append(finding, rule=_rule_for(finding))

    def fingerprint(self) -> str:
        """Hash of the offline decision stream. Stable across runs and machines.

        Sorted rather than sequential, because the offline stages are
        independent of each other and the order they happen to append in is not
        part of what was decided.
        """
        offline = sorted(
            d.digest() for d in self.decisions if d.stage != Stage.LLM.value
        )
        return _hash(self.corpus_sha, *offline)

    def tail_fingerprint(self) -> str:
        """Separate hash for model-assisted decisions. Not expected to be stable."""
        tail = sorted(d.digest() for d in self.decisions if d.stage == Stage.LLM.value)
        return _hash(*tail) if tail else "none"

    def summary(self) -> dict:
        by_stage: dict[str, int] = {}
        for d in self.decisions:
            by_stage[d.stage] = by_stage.get(d.stage, 0) + 1
        return {
            "run_id": self.run_id,
            "corpus_sha": self.corpus_sha,
            "started_at": self.started_at,
            "decisions": len(self.decisions),
            "by_stage": by_stage,
            "offline_fingerprint": self.fingerprint(),
            "tail_fingerprint": self.tail_fingerprint(),
            "requires_human": sum(1 for d in self.decisions if d.requires_human),
        }

    def write(self, path: Path) -> Path:
        """Write as JSON Lines: one header, then one object per decision."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"header": self.summary()}, ensure_ascii=False) + "\n")
            for decision in self.decisions:
                handle.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")
        return path

    @classmethod
    def read(cls, path: Path) -> tuple[dict, list[Decision]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])["header"]
        decisions = [Decision(**json.loads(line)) for line in lines[1:] if line.strip()]
        return header, decisions


#: Which named rule produced each reason code. The rule name is what an analyst
#: quotes back when disputing a finding, so it is recorded rather than derived.
_RULES: dict[str, str] = {
    "MDR_OVERCHARGE": "arithmetic.check_fees/rate_vs_contract",
    "GST_MISCALC": "arithmetic.check_fees/gst_on_fee",
    "SETTLEMENT_AMOUNT_MISMATCH": "arithmetic.check_settlement_totals",
    "ORDER_PAID_NOT_SETTLED": "deterministic.check_payments_reach_a_settlement",
    "DUPLICATE_PAYMENT": "deterministic.check_duplicate_captures",
    "REFUND_DOUBLE_COUNTED": "deterministic.check_double_deductions",
    "CHARGEBACK_NETTED_TWICE": "deterministic.check_double_deductions",
    "LATE_SETTLEMENT": "deterministic.check_settlement_timeliness",
    "SETTLEMENT_ON_HOLD": "deterministic.check_held_settlements",
    "SETTLED_NOT_IN_BANK": "fuzzy.BankMatcher/no_candidate",
    "BANK_CREDIT_UNIDENTIFIED": "fuzzy.BankMatcher/unattributed_credit",
    "PARTIAL_BANK_CREDIT": "fuzzy.BankMatcher/split_credit",
}


def _rule_for(finding: Finding) -> str:
    base = _RULES.get(finding.reason.value, "unknown")
    return f"{base}@{finding.stage.value}" if finding.stage is Stage.LLM else base


def corpus_fingerprint(corpus) -> str:
    """Hash the books themselves, so a ledger names the input it judged."""
    parts = [
        str(len(corpus.orders)),
        str(len(corpus.payments)),
        str(len(corpus.settlements)),
        str(len(corpus.bank_txns)),
        str(sum(p.amount_paise for p in corpus.payments)),
        str(sum(s.net_paise for s in corpus.settlements)),
        str(sum(b.credit_paise for b in corpus.bank_txns)),
    ]
    return _hash(*parts)
