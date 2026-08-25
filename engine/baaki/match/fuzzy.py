"""Stage 2: match settlements to bank credits without a model.

The bank statement is the only source the gateway does not control, which makes
it the one that proves the money actually arrived -- and the one written in free
text by a third party. This stage closes that join with four passes of
descending confidence, and hands whatever is left to the LLM stage rather than
guessing.

    Pass A   exact UTR recovered from the narration          confidence 1.00
    Pass B   truncated UTR, unique prefix, >= 12 chars       confidence 0.97
    Pass C   exact amount within a date window, unique       confidence 0.90
    Pass D   subset of credits summing to one settlement     confidence 0.85

Uniqueness is enforced in both directions on every pass. A settlement that
could plausibly pair with two credits is not matched at 90% confidence, it is
recorded as ambiguous and escalated. In reconciliation a confident wrong match
silently closes a real loss, which is far worse than an unresolved item sitting
in a queue.

The UTR parser here is written against the RBI reference format, not against
the generator's narration templates. That independence is what makes the
narration results worth reporting.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from itertools import combinations

from ..models import BankTxn, Corpus, Reason, Settlement, SettlementStatus
from ..money import rupees
from .findings import Evidence, Finding, Residue, Stage

#: An RBI-format UTR: four-character bank code, a scheme letter, an eight-digit
#: date, then a nine-digit sequence. Written from the specification rather than
#: from any example the generator produces.
UTR_PATTERN = re.compile(r"\b([A-Z]{4}[A-Z]\d{8}\d{9})\b", re.IGNORECASE)

#: Fallback for narrations the remitting bank has truncated. Any alphanumeric
#: run long enough to be a UTR prefix is a candidate; it only counts if it
#: prefixes exactly one settlement.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{12,}")

#: Shortest prefix accepted as a truncated UTR. Twelve characters covers the
#: bank code, the scheme letter and the full date, so a collision requires two
#: settlements from the same bank on the same day whose sequences also share a
#: prefix.
MIN_PREFIX = 12

#: How far a bank credit may sit from its settlement date. Value dating moves a
#: credit by a day either way; more than that and amount agreement alone is not
#: enough to claim a match.
DATE_WINDOW_DAYS = 1

#: Largest group of credits considered when looking for a split payout. Beyond
#: three the search space grows faster than the evidence does, and a four-way
#: coincidence of amounts is more likely than a four-way split.
MAX_SPLIT_PARTS = 3


def extract_utrs(narration: str) -> list[str]:
    """Recover UTR candidates from a bank narration, best first."""
    full = [m.group(1).upper() for m in UTR_PATTERN.finditer(narration)]
    partial = [
        t.upper()
        for t in TOKEN_PATTERN.findall(narration)
        if t.upper() not in full and any(c.isdigit() for c in t)
    ]
    return full + partial


def _expects_a_credit(settlement: Settlement) -> bool:
    """Whether this settlement should have produced a bank credit at all.

    Held settlements and wholly negative batches must be excluded here. A held
    settlement has no credit *by design*; reporting it as missing money would
    be a false positive, and the taxonomy already accounts for it separately
    under ``SETTLEMENT_ON_HOLD``.
    """
    return settlement.status is SettlementStatus.PROCESSED and settlement.net_paise > 0


class BankMatcher:
    """Resolves the settlement-to-bank join, and reports what it could not."""

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.settlements = [s for s in corpus.settlements if _expects_a_credit(s)]
        self.credits = [b for b in corpus.bank_txns if b.credit_paise > 0]

        self.open_settlements: dict[str, Settlement] = {
            s.settlement_id: s for s in self.settlements
        }
        self.open_credits: dict[str, BankTxn] = {b.bank_txn_id: b for b in self.credits}

        #: settlement_id -> [bank_txn_id], with the pass that made the call.
        self.matches: dict[str, tuple[list[str], str, float]] = {}
        self.residue = Residue()

    # -- passes -------------------------------------------------------------

    def _resolve_by_reference(
        self, owner_of: dict[str, str], how: str, confidence: float
    ) -> None:
        """Commit reference-based matches, but only where the money agrees.

        ``owner_of`` maps a recovered reference to the single settlement that
        owns it. Every open credit quoting that reference is gathered, and the
        group is committed **only if it sums to the settlement net**.

        The amount check is not belt and braces, it is the substance of the
        pass. A settlement paid out as two credits has both of them quoting the
        same UTR, so matching the first credit to arrive and stopping closes
        the settlement against a fraction of its value and orphans the rest.
        Matching on an identifier proves the two records refer to each other;
        only the amount proves the money arrived.
        """
        quoting: dict[str, list[str]] = defaultdict(list)
        for bank_id, txn in self.open_credits.items():
            for candidate in extract_utrs(txn.narration):
                if candidate in owner_of:
                    quoting[candidate].append(bank_id)

        for reference, bank_ids in quoting.items():
            settlement_id = owner_of[reference]
            settlement = self.open_settlements.get(settlement_id)
            if settlement is None:
                continue
            live = [b for b in bank_ids if b in self.open_credits]
            if not live:
                continue

            total = sum(self.open_credits[b].credit_paise for b in live)
            if total != settlement.net_paise:
                # The reference is right and the money is not. Escalate rather
                # than commit; a later pass may still find a group that sums,
                # and if none does this is a genuine shortfall worth a human.
                self.residue.ambiguous.append((settlement_id, live))
                continue

            if len(live) == 1:
                self._commit(settlement_id, live, how, confidence)
            else:
                self._commit(settlement_id, live, "split_credit", min(confidence, 0.95))

    def pass_exact_utr(self) -> None:
        by_utr: dict[str, list[str]] = defaultdict(list)
        for settlement in self.open_settlements.values():
            if settlement.utr:
                by_utr[settlement.utr.upper()].append(settlement.settlement_id)

        unique = {utr: owners[0] for utr, owners in by_utr.items() if len(owners) == 1}
        self._resolve_by_reference(unique, "exact_utr", 1.00)

    def pass_prefix_utr(self) -> None:
        """Truncated narrations, where the bank cut the reference short."""
        remaining = {sid: s.utr.upper() for sid, s in self.open_settlements.items() if s.utr}

        prefixes: dict[str, list[str]] = defaultdict(list)
        for txn in self.open_credits.values():
            for candidate in extract_utrs(txn.narration):
                if len(candidate) < MIN_PREFIX:
                    continue
                owners = [sid for sid, utr in remaining.items() if utr.startswith(candidate)]
                if len(owners) == 1:
                    prefixes[candidate] = owners

        unique = {ref: owners[0] for ref, owners in prefixes.items() if len(owners) == 1}
        self._resolve_by_reference(unique, "prefix_utr", 0.97)

    def pass_amount_and_date(self) -> None:
        """No usable reference, so fall back to value and timing.

        Only committed when the pairing is unique in both directions. If two
        settlements on the same day share an amount, neither is matched.
        """
        by_amount: dict[int, list[str]] = defaultdict(list)
        for bank_id, txn in self.open_credits.items():
            by_amount[txn.credit_paise].append(bank_id)

        for sid, settlement in list(self.open_settlements.items()):
            due = settlement.created_at.date()
            candidates = [
                bank_id
                for bank_id in by_amount.get(settlement.net_paise, [])
                if bank_id in self.open_credits
                and abs((self.open_credits[bank_id].value_date - due).days) <= DATE_WINDOW_DAYS
            ]
            if not candidates:
                continue
            if len(candidates) > 1:
                self.residue.ambiguous.append((sid, candidates))
                continue

            bank_id = candidates[0]
            # Reverse uniqueness: no other open settlement may claim this credit.
            rival = [
                other
                for other, os in self.open_settlements.items()
                if other != sid
                and os.net_paise == settlement.net_paise
                and abs((self.open_credits[bank_id].value_date - os.created_at.date()).days)
                <= DATE_WINDOW_DAYS
            ]
            if rival:
                self.residue.ambiguous.append((sid, [bank_id]))
                continue

            self._commit(sid, [bank_id], "amount_date", 0.90)

    def pass_split_credits(self) -> None:
        """One settlement paid out as several credits.

        Restricted to credits inside the date window and to groups of at most
        :data:`MAX_SPLIT_PARTS`, which keeps the search trivial in practice --
        a day has a handful of open credits, not thousands.
        """
        for sid, settlement in list(self.open_settlements.items()):
            due = settlement.created_at.date()
            nearby = [
                bank_id
                for bank_id, txn in self.open_credits.items()
                if abs((txn.value_date - due).days) <= DATE_WINDOW_DAYS
                and txn.credit_paise < settlement.net_paise
            ]
            if len(nearby) < 2:
                continue

            hit = None
            for size in range(2, min(MAX_SPLIT_PARTS, len(nearby)) + 1):
                for group in combinations(nearby, size):
                    if sum(self.open_credits[b].credit_paise for b in group) == settlement.net_paise:
                        hit = list(group)
                        break
                if hit:
                    break
            if hit:
                self._commit(sid, hit, "split_credit", 0.85)

    # -- plumbing -----------------------------------------------------------

    def _commit(self, settlement_id: str, bank_ids: list[str], how: str, confidence: float) -> None:
        self.matches[settlement_id] = (bank_ids, how, confidence)
        self.open_settlements.pop(settlement_id, None)
        for bank_id in bank_ids:
            self.open_credits.pop(bank_id, None)

    def run(self) -> list[Finding]:
        self.pass_exact_utr()
        self.pass_prefix_utr()
        self.pass_amount_and_date()
        self.pass_split_credits()

        findings: list[Finding] = []

        for settlement_id, (bank_ids, how, confidence) in self.matches.items():
            if how != "split_credit":
                continue
            settlement = next(s for s in self.settlements if s.settlement_id == settlement_id)
            parts = [self.corpus_credit(b) for b in bank_ids]
            findings.append(
                Finding(
                    reason=Reason.PARTIAL_BANK_CREDIT,
                    entity_type="settlement",
                    entity_id=settlement_id,
                    impact_paise=0,
                    stage=Stage.ALGORITHMIC,
                    confidence=confidence,
                    explanation=(
                        f"{rupees(settlement.net_paise)} arrived as {len(bank_ids)} credits "
                        f"({', '.join(rupees(p.credit_paise) for p in parts)}) which together "
                        f"match the settlement exactly. No money is missing."
                    ),
                    evidence=[
                        Evidence("settlements", settlement_id, "net", rupees(settlement.net_paise)),
                        *[
                            Evidence("bank", p.bank_txn_id, "credit", rupees(p.credit_paise))
                            for p in parts
                        ],
                    ],
                )
            )

        for settlement_id, settlement in self.open_settlements.items():
            self.residue.unmatched_settlements.append(settlement_id)
            findings.append(
                Finding(
                    reason=Reason.SETTLED_NOT_IN_BANK,
                    entity_type="settlement",
                    entity_id=settlement_id,
                    impact_paise=settlement.net_paise,
                    stage=Stage.ALGORITHMIC,
                    confidence=0.90,
                    requires_human=True,
                    explanation=(
                        f"{rupees(settlement.net_paise)} was settled on "
                        f"{settlement.created_at:%d %b %Y} under UTR {settlement.utr}, but no "
                        f"bank credit matches it by reference, by value and date, or as a "
                        f"split payout. Trace the UTR with the bank."
                    ),
                    evidence=[
                        Evidence("settlements", settlement_id, "utr", str(settlement.utr)),
                        Evidence("settlements", settlement_id, "net", rupees(settlement.net_paise)),
                        Evidence(
                            "settlements",
                            settlement_id,
                            "created_at",
                            f"{settlement.created_at:%Y-%m-%d}",
                        ),
                        Evidence("bank", "-", "candidate_credits", "0"),
                    ],
                )
            )

        for bank_id, txn in self.open_credits.items():
            self.residue.unmatched_bank_credits.append(bank_id)
            findings.append(
                Finding(
                    reason=Reason.BANK_CREDIT_UNIDENTIFIED,
                    entity_type="bank_txn",
                    entity_id=bank_id,
                    impact_paise=txn.credit_paise,
                    stage=Stage.ALGORITHMIC,
                    confidence=0.90,
                    requires_human=True,
                    explanation=(
                        f"{rupees(txn.credit_paise)} credited on {txn.value_date:%d %b %Y} "
                        f"does not correspond to any gateway settlement. Classify the source "
                        f"before it is booked as revenue."
                    ),
                    evidence=[
                        Evidence("bank", bank_id, "credit", rupees(txn.credit_paise)),
                        Evidence("bank", bank_id, "narration", txn.narration),
                        Evidence("bank", bank_id, "value_date", f"{txn.value_date}"),
                    ],
                )
            )

        return findings

    def corpus_credit(self, bank_id: str) -> BankTxn:
        return next(b for b in self.corpus.bank_txns if b.bank_txn_id == bank_id)

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for _bank_ids, how, _confidence in self.matches.values():
            counts[how] += 1
        counts["unmatched_settlements"] = len(self.open_settlements)
        counts["unmatched_credits"] = len(self.open_credits)
        counts["ambiguous"] = len(self.residue.ambiguous)
        return dict(counts)


def run(corpus: Corpus) -> tuple[list[Finding], Residue, dict[str, int]]:
    matcher = BankMatcher(corpus)
    findings = matcher.run()
    return findings, matcher.residue, matcher.stats()
