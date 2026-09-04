"""Stage 3: the tail.

This stage exists for two jobs the first two stages provably cannot do, and it
is scoped to those jobs rather than pointed at the whole batch.

**Splits past the search cap.** :mod:`baaki.match.fuzzy` searches combinations
of at most three credits, because past that the search space grows faster than
the evidence does. A four-way payout is therefore outside it by construction. A
model can look at a handful of credits and propose that they belong together;
the guardrail then adds them up. The model supplies the hypothesis and the
arithmetic supplies the proof.

**Equal-value candidates.** When two credits match a settlement's value and
date exactly, arithmetic has nothing left to say -- both sum correctly. The
discriminator is the narration: one reads ``RAZORPAY SOFTWARE PVT LTD
SETTLEMENT`` and the other ``ARORA TEXTILES PRIVATE LIMITED INV4097``. Telling
a gateway payout from a customer's invoice payment is a semantic judgement, and
it is the one thing here that a regular expression genuinely cannot do.

The stage's purpose is to make the exception queue **shorter**, not longer. It
resolves items the earlier stages had to escalate; it does not go looking for
new defects, because the deterministic stages already found those with
certainty and a model would only add noise to a solved problem.

Nothing it proposes is trusted. Every proposal goes through
:mod:`baaki.match.guardrails`, and a resolution is only applied when the
credits it names sum exactly to the settlement. Without an API key the stage
is skipped and the residue stays escalated, which is a worse report but never
a wrong one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from ..models import Corpus, Reason
from ..money import rupees
from .findings import Evidence, Finding, Residue, Stage
from .guardrails import Checked, Proposal, Scope, Verdict, check

log = logging.getLogger(__name__)

#: How far either side of a settlement date to draw candidate credits from.
#: Wider than the algorithmic stage's window, because this stage can afford to
#: reason about why a candidate does not fit.
CANDIDATE_WINDOW_DAYS = 3

#: Most candidates shown for one settlement. A prompt listing forty credits
#: invites the model to find a subset that sums by coincidence.
MAX_CANDIDATES = 12

#: Credits classified per call, and the ceiling for that call.
#:
#: The tail model is a reasoning model, so its chain-of-thought is billed
#: against ``max_tokens`` before a single output token is written. Classifying
#: twenty-three credits in one request spent the whole budget reasoning and
#: truncated the JSON mid-object. Eight per call leaves ample headroom.
CLASSIFY_BATCH = 8
CLASSIFY_MAX_TOKENS = 2_400

RESOLVE_SYSTEM = """You reconcile Indian payment-gateway settlements against bank statement credits.

You are given ONE settlement and a list of candidate bank credits. Decide whether some
subset of those credits is that settlement's payout.

Rules:
- A subset qualifies ONLY if its amounts sum to exactly the settlement net. Add them up.
- A settlement is often paid as several credits. Two, three or four parts are all normal.
- Not every credit belongs to the gateway. Narrations naming another company, an invoice
  number, savings interest, or a different payment processor are somebody else's money.
  Exclude them even when the amount matches exactly.
- Gateway payouts name the gateway: RAZORPAY, RZPY, or similar, usually with SETTLEMENT
  or PAYOUT.
- If nothing sums exactly, or if you cannot tell two candidates apart, answer "unresolved".
  Unresolved is a correct answer. Guessing is not.
- Cite only credit_ids from the list you were given. Never invent one.

Respond with ONLY valid JSON, no prose and no code fences:
{
  "verdict": "matched" | "unresolved",
  "credit_ids": ["bank_..."],
  "reasoning": "one or two sentences",
  "excluded": [{"credit_id": "bank_...", "why": "short reason"}]
}"""

CLASSIFY_SYSTEM = """You classify bank statement credits that are NOT payment-gateway settlements.

For each credit, name the most likely source from:
  other_gateway     a competing payment processor
  customer_transfer a direct payment from a named customer, often with an invoice number
  bank_interest     interest paid by the bank
  vendor_refund     money returned by a supplier
  unknown           the narration does not say

Respond with ONLY valid JSON, no prose and no code fences:
{"classifications": [{"credit_id": "bank_...", "source": "...", "why": "short reason"}]}"""


@dataclass(slots=True)
class TailReport:
    """What the stage cost and what it changed. Reported in EVAL.md."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    proposals: int = 0
    accepted: int = 0
    rejected: int = 0
    flags: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False

    def record(self, checked: Checked) -> None:
        self.proposals += 1
        if checked.verdict is Verdict.REJECT:
            self.rejected += 1
        else:
            self.accepted += 1
        for flag in checked.flags:
            self.flags[flag.value] = self.flags.get(flag.value, 0) + 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True)
class TailOutcome:
    """Retractions and enrichments, not new accusations."""

    resolved_settlements: set[str] = field(default_factory=set)
    resolved_credits: set[str] = field(default_factory=set)
    new_findings: list[Finding] = field(default_factory=list)
    explanations: dict[str, str] = field(default_factory=dict)
    report: TailReport = field(default_factory=TailReport)


def _candidates(corpus: Corpus, settlement_id: str, open_credit_ids: list[str]) -> list:
    settlement = next(s for s in corpus.settlements if s.settlement_id == settlement_id)
    due = settlement.created_at.date()
    window = timedelta(days=CANDIDATE_WINDOW_DAYS)
    pool = [
        b
        for b in corpus.bank_txns
        if b.bank_txn_id in set(open_credit_ids)
        and abs((b.value_date - due).days) <= window.days
        and b.credit_paise > 0
    ]
    pool.sort(key=lambda b: abs((b.value_date - due).days))
    return settlement, pool[:MAX_CANDIDATES]


def _render(settlement, candidates) -> str:
    lines = [
        f"SETTLEMENT {settlement.settlement_id}",
        f"  net        {rupees(settlement.net_paise)}  ({settlement.net_paise} paise)",
        f"  date       {settlement.created_at:%Y-%m-%d}",
        f"  reference  {settlement.utr or 'none on file'}",
        "",
        "CANDIDATE CREDITS",
    ]
    for b in candidates:
        lines.append(
            f"  {b.bank_txn_id}  {b.value_date:%Y-%m-%d}  "
            f"{rupees(b.credit_paise):>16} ({b.credit_paise} paise)  {b.narration}"
        )
    return "\n".join(lines)


async def _resolve_one(client, corpus: Corpus, settlement_id: str, open_credits: list[str]):
    settlement, candidates = _candidates(corpus, settlement_id, open_credits)
    if not candidates:
        return None, None

    payload = await client.complete_json(RESOLVE_SYSTEM, _render(settlement, candidates))
    credit_ids = [c for c in payload.get("credit_ids", []) if isinstance(c, str)]
    verdict = str(payload.get("verdict", "unresolved")).lower()

    if verdict != "matched" or not credit_ids:
        return None, None

    proposal = Proposal(
        reason=Reason.PARTIAL_BANK_CREDIT.value
        if len(credit_ids) > 1
        else Reason.SETTLED_NOT_IN_BANK.value,
        entity_type="settlement",
        entity_id=settlement_id,
        explanation=str(payload.get("reasoning", "")).strip(),
        cited_ids=[settlement_id, *credit_ids],
        proposed_credit_ids=credit_ids,
        impact_paise=0,
        model_confidence=1.0,
    )
    scope = Scope(corpus, [settlement_id], [c.bank_txn_id for c in candidates])
    return proposal, check(proposal, scope)


async def resolve(
    corpus: Corpus, residue: Residue, client, *, max_concurrency: int = 2
) -> TailOutcome:
    """Work the residue. Returns retractions, not accusations."""
    outcome = TailOutcome()

    if client is None or not client.available:
        outcome.report.skipped = True
        log.info("tail stage skipped: no model configured; residue stays escalated")
        return outcome

    open_credits = list(residue.unmatched_bank_credits)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def worker(settlement_id: str):
        async with semaphore:
            try:
                return await _resolve_one(client, corpus, settlement_id, open_credits)
            except Exception as exc:  # noqa: BLE001
                outcome.report.errors.append(f"{settlement_id}: {exc}")
                return None, None

    results = await asyncio.gather(*(worker(s) for s in residue.unmatched_settlements))
    outcome.report.calls += len(residue.unmatched_settlements)

    for proposal, checked in results:
        if checked is None:
            continue
        outcome.report.record(checked)
        if checked.verdict is Verdict.REJECT:
            continue

        # Accepted only because the guardrail confirmed the credits sum to the
        # settlement net. The model chose which ones to add up; it did not get
        # to decide whether they added up.
        outcome.resolved_settlements.add(proposal.entity_id)
        outcome.resolved_credits.update(proposal.proposed_credit_ids)

        if len(proposal.proposed_credit_ids) > 1:
            outcome.new_findings.append(
                Finding(
                    reason=Reason.PARTIAL_BANK_CREDIT,
                    entity_type="settlement",
                    entity_id=proposal.entity_id,
                    impact_paise=0,
                    stage=Stage.LLM,
                    confidence=checked.confidence,
                    requires_human=checked.verdict is Verdict.REVIEW,
                    explanation=(
                        f"Paid out as {len(proposal.proposed_credit_ids)} credits which sum "
                        f"exactly to the settlement. {proposal.explanation}"
                    ),
                    evidence=[
                        Evidence("bank", credit_id, "credit", credit_id)
                        for credit_id in proposal.proposed_credit_ids
                    ],
                )
            )

    leftover = [c for c in open_credits if c not in outcome.resolved_credits]
    if leftover:
        await _classify(client, corpus, leftover, outcome)

    return outcome


async def _classify(client, corpus: Corpus, credit_ids: list[str], outcome: TailOutcome) -> None:
    """Name the likely source of each credit that is not a settlement.

    Enrichment only. It never changes whether something is a finding, only how
    the finding reads to the analyst who has to chase it, so a wrong guess here
    costs a sentence rather than a rupee.
    """
    by_id = {b.bank_txn_id: b for b in corpus.bank_txns}
    known = [cid for cid in credit_ids if cid in by_id]
    if not known:
        return

    allowed = set(known)

    # Chunked, because the tail model is a reasoning model and its
    # chain-of-thought is billed against ``max_tokens``. Asking it to classify
    # twenty-three credits in one call spends the whole budget thinking and
    # truncates mid-object -- which the client correctly raises as
    # TruncatedCompletion, but a raised error classifies nothing. Smaller
    # batches keep each response comfortably inside the ceiling.
    for start in range(0, len(known), CLASSIFY_BATCH):
        batch = known[start : start + CLASSIFY_BATCH]
        lines = [
            f"  {cid}  {by_id[cid].value_date:%Y-%m-%d}  "
            f"{rupees(by_id[cid].credit_paise):>16}  {by_id[cid].narration}"
            for cid in batch
        ]
        try:
            payload = await client.complete_json(
                CLASSIFY_SYSTEM, "CREDITS\n" + "\n".join(lines), max_tokens=CLASSIFY_MAX_TOKENS
            )
            outcome.report.calls += 1
        except Exception as exc:  # noqa: BLE001
            # One bad batch must not lose the others. Classification is
            # enrichment: a failure here costs a sentence, never a rupee.
            outcome.report.errors.append(f"classify[{start}:{start + len(batch)}]: {exc}")
            continue

        for row in payload.get("classifications", []):
            credit_id = row.get("credit_id")
            if credit_id not in allowed:
                # Grounding applies here too: a classification of a credit that
                # was never shown is discarded rather than attached to something.
                continue
            source = str(row.get("source", "unknown")).replace("_", " ")
            why = str(row.get("why", "")).strip()
            outcome.explanations[credit_id] = f"Likely {source}. {why}".strip()


def apply(findings: list[Finding], outcome: TailOutcome) -> list[Finding]:
    """Fold the tail's retractions and enrichments into the finding list."""
    kept: list[Finding] = []
    for finding in findings:
        if (
            finding.reason is Reason.SETTLED_NOT_IN_BANK
            and finding.entity_id in outcome.resolved_settlements
        ):
            continue
        if (
            finding.reason is Reason.BANK_CREDIT_UNIDENTIFIED
            and finding.entity_id in outcome.resolved_credits
        ):
            continue
        if finding.entity_id in outcome.explanations:
            finding.explanation = f"{finding.explanation} {outcome.explanations[finding.entity_id]}"
        kept.append(finding)
    return kept + outcome.new_findings
