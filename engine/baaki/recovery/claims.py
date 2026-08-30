"""Claims: what happened after we found the money.

A finding is a statement about the past. A claim is a position that moves:
opened, filed with the gateway, and then either recovered, refused, or quietly
aged out. The difference between the two is the difference between an auditor
and an agent that closes a loop.

The ledger persists across settlement cycles. Month one finds a fee overcharge
and files it; month two goes looking for the adjustment line that repays it. A
claim is only ``RECOVERED`` when the rupees are found in a later settlement --
never because the gateway said so, and never because we filed it.

Every transition is appended to the claim's own history, so the question "what
did we do about this, and when" has an answer that does not depend on anyone
remembering.
"""

from __future__ import annotations

import enum
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..models import REASON_META, Reason
from ..money import rupees


class ClaimState(str, enum.Enum):
    OPEN = "open"
    """Found, not yet acted on."""

    FILED = "filed"
    """Sent to the gateway. Waiting on an adjustment to appear."""

    RECOVERED = "recovered"
    """The money came back, and we found it in a later settlement."""

    PARTIAL = "partial"
    """Some of it came back. The remainder is still outstanding."""

    REJECTED = "rejected"
    """The gateway refused, or the claim was answered and closed unpaid."""

    WRITTEN_OFF = "written_off"
    """Not worth pursuing, or aged out. A decision, not a failure to decide."""

    NOT_PURSUED = "not_pursued"
    """Triage judged the chase to cost more than the money is worth."""


#: States a claim can no longer move out of.
TERMINAL = frozenset(
    {ClaimState.RECOVERED, ClaimState.REJECTED, ClaimState.WRITTEN_OFF, ClaimState.NOT_PURSUED}
)

#: States that count as the loop having been closed, whatever the outcome.
CLOSED = TERMINAL


class Disposition(str, enum.Enum):
    """What triage decided to do about a claim."""

    CHASE = "chase"
    """File on its own. Worth an analyst's time by itself."""

    BATCH = "batch"
    """Too small alone; file together with others of the same reason."""

    DROP = "drop"
    """Expected recovery does not cover the cost of asking."""


@dataclass(slots=True)
class ClaimEvent:
    at: str
    state: str
    note: str
    amount_paise: int = 0


@dataclass(slots=True)
class Claim:
    claim_id: str
    reason: str
    entity_type: str
    entity_id: str
    claimed_paise: int
    opened_on: str
    state: str = ClaimState.OPEN.value
    disposition: str = Disposition.CHASE.value
    recovered_paise: int = 0
    filed_on: str | None = None
    resolved_on: str | None = None
    batch_id: str | None = None
    evidence: list[str] = field(default_factory=list)
    explanation: str = ""
    history: list[ClaimEvent] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """Same identity a finding has, so a re-run recognises its own claims."""
        return (self.reason, self.entity_id)

    @property
    def outstanding_paise(self) -> int:
        return max(0, self.claimed_paise - self.recovered_paise)

    @property
    def is_closed(self) -> bool:
        return ClaimState(self.state) in CLOSED

    def age_days(self, as_of: date) -> int:
        return (as_of - date.fromisoformat(self.opened_on)).days

    def transition(self, state: ClaimState, note: str, *, amount_paise: int = 0,
                   on: date | None = None) -> None:
        self.state = state.value
        stamp = (on or datetime.now().date()).isoformat()
        if state is ClaimState.FILED and self.filed_on is None:
            self.filed_on = stamp
        if state in TERMINAL or state is ClaimState.PARTIAL:
            self.resolved_on = stamp
        self.history.append(
            ClaimEvent(at=stamp, state=state.value, note=note, amount_paise=amount_paise)
        )

    def describe(self) -> str:
        meta = REASON_META[Reason(self.reason)]
        return (
            f"{self.claim_id} · {self.reason} · {rupees(self.claimed_paise)} · "
            f"{self.state} · {meta['severity'].value}"
        )


@dataclass(slots=True)
class Ledger:
    """The book of claims, carried between cycles."""

    claims: dict[str, Claim] = field(default_factory=dict)
    cycles: list[str] = field(default_factory=list)

    # -- lookup -------------------------------------------------------------

    def by_key(self) -> dict[tuple[str, str], Claim]:
        return {c.key: c for c in self.claims.values()}

    def open_claims(self) -> list[Claim]:
        return [c for c in self.claims.values() if not c.is_closed]

    def filed(self) -> list[Claim]:
        return [c for c in self.claims.values() if c.state == ClaimState.FILED.value]

    def in_state(self, *states: ClaimState) -> list[Claim]:
        wanted = {s.value for s in states}
        return [c for c in self.claims.values() if c.state in wanted]

    # -- writing ------------------------------------------------------------

    def open_from_findings(self, findings, *, on: date, cycle: str) -> list[Claim]:
        """Create claims for findings not already tracked.

        Re-running the same cycle must not duplicate claims, and a finding that
        persists into the next cycle must attach to its existing claim rather
        than starting a second one. Identity is ``(reason, entity_id)`` -- the
        same key scoring uses.
        """
        if cycle not in self.cycles:
            self.cycles.append(cycle)

        existing = self.by_key()
        opened: list[Claim] = []

        for finding in findings:
            if not finding.recoverable or finding.impact_paise <= 0:
                # Timing and attribution items are reported, never claimed.
                # Filing a ticket for a late settlement wastes everyone's time.
                continue
            key = (finding.reason.value, finding.entity_id)
            if key in existing:
                continue

            claim = Claim(
                claim_id=f"clm_{len(self.claims) + 1:05d}",
                reason=finding.reason.value,
                entity_type=finding.entity_type,
                entity_id=finding.entity_id,
                claimed_paise=finding.impact_paise,
                opened_on=on.isoformat(),
                evidence=[e.render() for e in finding.evidence],
                explanation=finding.explanation,
            )
            claim.transition(ClaimState.OPEN, f"Found in cycle {cycle}.", on=on)
            self.claims[claim.claim_id] = claim
            existing[key] = claim
            opened.append(claim)

        return opened

    # -- reporting ----------------------------------------------------------

    def totals(self) -> dict:
        claimed = sum(c.claimed_paise for c in self.claims.values())
        recovered = sum(c.recovered_paise for c in self.claims.values())
        pursued = [c for c in self.claims.values() if c.disposition != Disposition.DROP.value]
        pursued_claimed = sum(c.claimed_paise for c in pursued)
        return {
            "claims": len(self.claims),
            "claimed_paise": claimed,
            "recovered_paise": recovered,
            "outstanding_paise": sum(c.outstanding_paise for c in self.open_claims()),
            "pursued_claimed_paise": pursued_claimed,
            "recovery_rate": (recovered / pursued_claimed) if pursued_claimed else 0.0,
            "by_state": self.state_counts(),
        }

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for claim in self.claims.values():
            counts[claim.state] = counts.get(claim.state, 0) + 1
        return counts

    def recovery_by_reason(self) -> dict[str, dict]:
        """The number that does not exist in any other reconciliation tool.

        Recovery rate per reason code tells a merchant which fights are worth
        having. A fee correction that lands nine times out of ten is worth
        filing on sight; an unattributed credit that resolves a third of the
        time is worth batching and chasing once a quarter.

        Only claims that were actually pursued count in the denominator --
        including the ones triage dropped would measure our own triage, not the
        gateway's behaviour.
        """
        out: dict[str, dict] = {}
        for claim in self.claims.values():
            if claim.disposition == Disposition.DROP.value:
                continue
            row = out.setdefault(
                claim.reason,
                {"claims": 0, "claimed_paise": 0, "recovered_paise": 0, "resolved": 0, "won": 0},
            )
            row["claims"] += 1
            row["claimed_paise"] += claim.claimed_paise
            row["recovered_paise"] += claim.recovered_paise
            if claim.is_closed or claim.state == ClaimState.PARTIAL.value:
                row["resolved"] += 1
                if claim.recovered_paise > 0:
                    row["won"] += 1

        for row in out.values():
            row["rate"] = (
                row["recovered_paise"] / row["claimed_paise"] if row["claimed_paise"] else 0.0
            )
            row["win_rate"] = row["won"] / row["resolved"] if row["resolved"] else None
        return out

    def aging(self, as_of: date) -> dict[str, int]:
        """Outstanding money by how long it has been waiting."""
        buckets = {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
        for claim in self.open_claims():
            age = claim.age_days(as_of)
            key = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"
            buckets[key] += claim.outstanding_paise
        return buckets

    # -- persistence --------------------------------------------------------

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cycles": self.cycles,
            "claims": [asdict(c) for c in self.claims.values()],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        ledger = cls(cycles=payload.get("cycles", []))
        for raw in payload.get("claims", []):
            history = [ClaimEvent(**e) for e in raw.pop("history", [])]
            claim = Claim(**raw, history=history)
            ledger.claims[claim.claim_id] = claim
        return ledger
