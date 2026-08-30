"""Deciding which claims are worth making.

Finding money is not the same as it being worth collecting. A fee overcharge of
six rupees is real, and provable, and costs more in analyst time to chase than
it returns. A tool that files it anyway is not being thorough, it is wasting
the one resource the finance team actually has.

So every claim gets an expected value:

    expected value  =  claimed x P(recovery | reason)  -  cost of asking

and three possible answers: chase it alone, batch it with others of the same
reason, or drop it. **Dropping is a decision that gets recorded**, not an item
quietly falling off the list -- the ledger tracks what was dropped and what it
was worth, so the policy itself can be audited later.

Batching is where most of the value is. Forty fee overcharges of six rupees are
individually worthless and collectively worth chasing, because one ticket
covers all forty and the cost is paid once.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Reason
from ..money import rupees
from .claims import Claim, ClaimState, Disposition

#: What an analyst's time costs, in paise per hour. A finance associate in
#: India, loaded. Configurable because the answer changes with who is doing it.
ANALYST_COST_PER_HOUR_PAISE = 60_000

#: Handling one claim on its own: read it, check the evidence, raise the
#: ticket, follow it up once.
MINUTES_PER_CLAIM = 12

#: A batched ticket costs a fixed setup plus a little per line.
BATCH_SETUP_MINUTES = 20
BATCH_MINUTES_PER_ITEM = 1.5

#: Prior probability that a claim of each kind is paid back.
#:
#: **These are informed guesses, and they are meant to be replaced.** Once a
#: cycle has resolved real claims, :func:`learned_rates` blends observation into
#: these and the priors matter less every month. They are ordered by how
#: arguable the underlying fact is: a fee against a signed rate card is
#: arithmetic and hard to refuse; a settlement missing between gateway and bank
#: may be genuinely disputed or sit with a third party.
PRIOR_RECOVERY: dict[str, float] = {
    Reason.MDR_OVERCHARGE.value: 0.85,
    Reason.GST_MISCALC.value: 0.80,
    Reason.REFUND_DOUBLE_COUNTED.value: 0.75,
    Reason.SETTLEMENT_AMOUNT_MISMATCH.value: 0.70,
    Reason.CHARGEBACK_NETTED_TWICE.value: 0.70,
    Reason.ORDER_PAID_NOT_SETTLED.value: 0.55,
    Reason.SETTLED_NOT_IN_BANK.value: 0.45,
}

DEFAULT_PRIOR = 0.50

#: Strength of the prior, in pseudo-observations. With k = 8, a single
#: recovered claim moves the rate a little and eight move it a lot. Without
#: this, the first claim to resolve would swing the whole policy -- one lucky
#: refund would have the engine chasing everything, and one refusal would have
#: it dropping a category that is actually worth pursuing.
PRIOR_STRENGTH = 8.0


def analyst_cost_paise(minutes: float) -> int:
    return int(round(ANALYST_COST_PER_HOUR_PAISE * minutes / 60.0))


#: Cost of handling one claim by itself.
SOLO_COST_PAISE = analyst_cost_paise(MINUTES_PER_CLAIM)


def learned_rates(ledger) -> dict[str, float]:
    """Blend observed recovery with the prior, weighted by how much we've seen.

    Returns a rate per reason code. Early on this is mostly prior; after a few
    cycles it is mostly evidence.
    """
    observed = ledger.recovery_by_reason()
    rates: dict[str, float] = {}

    for reason in Reason:
        key = reason.value
        prior = PRIOR_RECOVERY.get(key, DEFAULT_PRIOR)
        row = observed.get(key)
        if not row or not row["claimed_paise"]:
            rates[key] = prior
            continue

        # Weight by resolved count, not claim count: an unresolved claim is not
        # evidence of anything yet.
        n = row["resolved"]
        if n == 0:
            rates[key] = prior
            continue
        rate = row["recovered_paise"] / row["claimed_paise"]
        rates[key] = (rate * n + prior * PRIOR_STRENGTH) / (n + PRIOR_STRENGTH)

    return rates


#: Resolved claims needed before a reason's observed rate is trusted on its own.
#: Below this, the policy deliberately spends a little money to learn.
EXPLORATION_THRESHOLD = 5


def observed_resolutions(ledger, reason: str) -> int:
    row = ledger.recovery_by_reason().get(reason)
    return row["resolved"] if row else 0


def _exploration_probe(group: list[Claim], reason: str, resolved: int) -> Claim | None:
    """Pick one claim to file anyway, when a reason has no track record.

    A policy that only files what it already expects to win never finds out it
    was wrong. Every reason code starts on a prior that was, honestly, a guess;
    if the prior is pessimistic, expected value drops the whole category
    forever and no evidence ever arrives to correct it. The rate stays wrong and
    the policy stays confident.

    So while a reason has fewer than :data:`EXPLORATION_THRESHOLD` resolved
    claims, one claim from each rejected batch is filed regardless -- the
    largest, since it is the cheapest way to buy information relative to the
    analyst minute it costs. Once there is a track record, this stops: the
    exploration is bounded by the evidence, not run forever.
    """
    if resolved >= EXPLORATION_THRESHOLD or not group:
        return None
    return max(group, key=lambda c: c.claimed_paise)


@dataclass(slots=True)
class Decision:
    disposition: Disposition
    expected_paise: int
    cost_paise: int
    rate: float
    rationale: str

    @property
    def net_paise(self) -> int:
        return self.expected_paise - self.cost_paise


def assess(claim: Claim, rates: dict[str, float]) -> Decision:
    """Expected value of chasing this claim on its own."""
    rate = rates.get(claim.reason, DEFAULT_PRIOR)
    expected = int(round(claim.claimed_paise * rate))
    net = expected - SOLO_COST_PAISE

    if net > 0:
        return Decision(
            Disposition.CHASE,
            expected,
            SOLO_COST_PAISE,
            rate,
            f"Expected recovery {rupees(expected)} at a {rate:.0%} rate against "
            f"{rupees(SOLO_COST_PAISE)} of analyst time. Worth filing alone.",
        )

    return Decision(
        Disposition.BATCH,
        expected,
        SOLO_COST_PAISE,
        rate,
        f"Expected recovery {rupees(expected)} does not cover {rupees(SOLO_COST_PAISE)} "
        f"to file alone. Only worth it batched.",
    )


def triage(ledger, *, on) -> dict:
    """Decide what to do with every open claim, and record the decision.

    Two passes. The first values each claim on its own. The second gathers
    everything that failed that test, groups it by reason, and asks whether the
    group clears one shared cost -- which is where small systematic overcharges
    stop being noise and become a ticket worth raising.
    """
    rates = learned_rates(ledger)
    pending = [c for c in ledger.open_claims() if c.state == ClaimState.OPEN.value]

    solo: list[Claim] = []
    candidates: dict[str, list[Claim]] = {}

    for claim in pending:
        decision = assess(claim, rates)
        if decision.disposition is Disposition.CHASE:
            claim.disposition = Disposition.CHASE.value
            claim.transition(ClaimState.FILED, decision.rationale, on=on)
            solo.append(claim)
        else:
            candidates.setdefault(claim.reason, []).append(claim)

    batched: dict[str, list[Claim]] = {}
    dropped: list[Claim] = []
    explored: list[Claim] = []

    for reason, group in candidates.items():
        rate = rates.get(reason, DEFAULT_PRIOR)
        total = sum(c.claimed_paise for c in group)
        expected = int(round(total * rate))
        cost = analyst_cost_paise(BATCH_SETUP_MINUTES + BATCH_MINUTES_PER_ITEM * len(group))

        if expected - cost > 0:
            batch_id = f"batch_{reason.lower()}_{on:%Y%m}"
            for claim in group:
                claim.disposition = Disposition.BATCH.value
                claim.batch_id = batch_id
                claim.transition(
                    ClaimState.FILED,
                    f"Filed in {batch_id}: {len(group)} claims worth {rupees(total)} "
                    f"together, expected {rupees(expected)} at {rate:.0%} against "
                    f"{rupees(cost)} to raise one ticket.",
                    on=on,
                )
            batched[batch_id] = group
        else:
            probe = _exploration_probe(group, reason, observed_resolutions(ledger, reason))
            for claim in group:
                if claim is probe:
                    claim.disposition = Disposition.CHASE.value
                    claim.transition(
                        ClaimState.FILED,
                        f"Filed as an exploration probe. The batch was not worth "
                        f"raising, but {reason} has too little resolved history to "
                        f"trust the {rate:.0%} rate that decided against it.",
                        on=on,
                    )
                    explored.append(claim)
                    continue
                claim.disposition = Disposition.DROP.value
                claim.transition(
                    ClaimState.NOT_PURSUED,
                    f"Not pursued: {len(group)} claims worth {rupees(total)} together "
                    f"expect {rupees(expected)}, under the {rupees(cost)} it costs to "
                    f"raise even one batched ticket.",
                    on=on,
                )
                dropped.append(claim)

    return {
        "rates": rates,
        "chase": solo,
        "batches": batched,
        "dropped": dropped,
        "explored": explored,
        "filed_paise": sum(c.claimed_paise for c in solo)
        + sum(c.claimed_paise for g in batched.values() for c in g),
        "dropped_paise": sum(c.claimed_paise for c in dropped),
    }
