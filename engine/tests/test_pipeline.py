"""End-to-end properties of the offline pipeline.

These are the claims the README makes, asserted rather than asserted-at.
"""

from __future__ import annotations

import pytest

from baaki.audit.ledger import Ledger, corpus_fingerprint
from baaki.corpus import io
from baaki.corpus.defects import HARD_PLAN, inject
from baaki.corpus.generate import generate
from baaki.evaluation.score import score
from baaki.match import pipeline
from baaki.match.findings import Stage
from baaki.models import Reason

SEED = 34
ORDERS = 1_500


@pytest.fixture(scope="module")
def faulted():
    return inject(generate(seed=SEED, n_orders=ORDERS), seed=SEED, plan=HARD_PLAN)


def _fingerprint(findings) -> list[tuple]:
    return sorted(
        (f.reason.value, f.entity_id, f.impact_paise, f.stage.value) for f in findings
    )


def test_the_offline_pipeline_uses_no_model(faulted):
    result = pipeline.run_offline(faulted.corpus)
    assert all(f.stage is not Stage.LLM for f in result.findings)
    assert result.tail.calls == 0


def test_running_from_csv_matches_running_from_memory(faulted, tmp_path):
    """The engine must depend on the files and nothing else.

    The generator holds the true settlement-to-bank mapping in memory. If any
    of that leaked into the matchers through the object graph rather than
    through the four exported sources, a run from disk would differ from a run
    in memory. It does not.
    """
    io.save(faulted.corpus, tmp_path, truth=faulted.truth)
    from_disk = pipeline.run_offline(io.load(tmp_path))
    from_memory = pipeline.run_offline(faulted.corpus)

    assert _fingerprint(from_disk.findings) == _fingerprint(from_memory.findings)


def test_the_answer_key_is_never_read_by_the_engine(faulted, tmp_path):
    """Deleting the ground truth must not change a single finding."""
    io.save(faulted.corpus, tmp_path, truth=faulted.truth)
    with_key = pipeline.run_offline(io.load(tmp_path))

    (tmp_path / io.TRUTH_FILE).unlink()
    without_key = pipeline.run_offline(io.load(tmp_path))

    assert _fingerprint(with_key.findings) == _fingerprint(without_key.findings)


def test_the_offline_decision_stream_is_reproducible(faulted):
    def ledger_for():
        result = pipeline.run_offline(faulted.corpus)
        ledger = Ledger(run_id="t", corpus_sha=corpus_fingerprint(faulted.corpus))
        ledger.extend(result.findings)
        return ledger

    assert ledger_for().fingerprint() == ledger_for().fingerprint()


def test_a_changed_amount_changes_the_fingerprint(faulted):
    """The fingerprint must be sensitive to money, not just to shape."""
    result = pipeline.run_offline(faulted.corpus)
    ledger = Ledger(run_id="t", corpus_sha=corpus_fingerprint(faulted.corpus))
    ledger.extend(result.findings)
    before = ledger.fingerprint()

    ledger.decisions[0].impact_paise += 1
    assert ledger.fingerprint() != before


def test_a_clean_corpus_only_reports_unattributed_credits():
    """No defects planted means nothing but statement noise may be flagged.

    This is the false-positive floor. Any other reason code appearing here is
    the engine inventing a problem.
    """
    g = generate(seed=11, n_orders=ORDERS)
    result = pipeline.run_offline(g.corpus)
    reasons = {f.reason for f in result.findings}
    assert reasons <= {Reason.BANK_CREDIT_UNIDENTIFIED, Reason.PARTIAL_BANK_CREDIT}


@pytest.mark.parametrize("seed", [7, 13, 21, 34, 55, 89, 101])
def test_the_only_defect_the_offline_stages_may_miss_is_a_referenceless_split(seed):
    """Offline recall is total except for one precisely bounded case.

    There are two ways the engine closes a split payout, and only one of them
    is capped. The reference path gathers *every* credit quoting a UTR and
    checks whether the group sums, so it closes a split of any width. The
    combination search is the fallback for credits with no usable reference,
    and it stops at :data:`~baaki.match.fuzzy.MAX_SPLIT_PARTS` because past
    three the search space grows faster than the evidence does.

    So a four-way split is missed only when its narrations also carry no
    recoverable reference -- both conditions at once. Asserting "nothing is
    ever missed" would have been the easy claim and a false one; this asserts
    the real boundary, and that anything falling outside it is still escalated
    rather than dropped.
    """
    g = inject(generate(seed=seed, n_orders=ORDERS), seed=seed, plan=HARD_PLAN)
    result = pipeline.run_offline(g.corpus)
    s = score(result.findings, g.truth)

    assert all(d.reason is Reason.PARTIAL_BANK_CREDIT for d in s.missed), [
        (d.reason.value, d.entity_id) for d in s.missed
    ]

    # Whatever was missed must still be visible to an analyst, as a settlement
    # the engine could not account for. Missing it must never mean losing it.
    escalated = {f.entity_id for f in result.findings if f.requires_human}
    for defect in s.missed:
        assert defect.entity_id in escalated


@pytest.mark.parametrize("seed", [7, 13, 21, 34, 55, 89, 101])
def test_all_recoverable_money_is_accounted_for(seed):
    """The headline claim. Every rupee planted as recoverable must be named.

    Failures of the matcher show up as an item an analyst has to look at, never
    as money that quietly disappears from the report.
    """
    g = inject(generate(seed=seed, n_orders=ORDERS), seed=seed, plan=HARD_PLAN)
    result = pipeline.run_offline(g.corpus)
    planted, found = score(result.findings, g.truth).money(recoverable_only=True)
    assert found == planted


def test_findings_above_the_ceiling_are_not_silently_auto_closed(faulted):
    """Anything the algorithmic stage could not resolve must ask for a person."""
    result = pipeline.run_offline(faulted.corpus)
    unresolved = [
        f
        for f in result.findings
        if f.reason in (Reason.SETTLED_NOT_IN_BANK, Reason.BANK_CREDIT_UNIDENTIFIED)
    ]
    assert unresolved
    assert all(f.requires_human for f in unresolved)


def test_every_finding_carries_evidence(faulted):
    """A finding with nothing cited is an opinion, and cannot be disputed."""
    result = pipeline.run_offline(faulted.corpus)
    assert all(f.evidence for f in result.findings)
