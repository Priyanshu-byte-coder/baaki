"""Drive the full recovery loop across two settlement cycles.

    July    reconcile -> open claims -> triage -> file
    August  reconcile -> hunt the adjustments that repay July's claims
            -> report what actually came back
"""

from __future__ import annotations

import sys
from datetime import date

from baaki.corpus.defects import HARD_PLAN, inject
from baaki.corpus.generate import generate
from baaki.corpus.periods import next_cycle
from baaki.match import pipeline
from baaki.money import rupees
from baaki.recovery import triage as triage_mod
from baaki.recovery import verify as verify_mod
from baaki.recovery.claims import ClaimState, Ledger


def rule(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def main() -> None:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 34
    orders = int(sys.argv[2]) if len(sys.argv) > 2 else 4000

    ledger = Ledger()
    july = date(2026, 7, 1)
    august = date(2026, 8, 1)

    # ---------------------------------------------------------------- cycle 1
    rule("CYCLE 1 — July 2026")
    g1 = inject(generate(seed=seed, n_orders=orders, month_start=july), seed=seed, plan=HARD_PLAN)
    r1 = pipeline.run_offline(g1.corpus)
    print(f"{g1.corpus.record_count():,} records, {len(r1.findings)} exceptions, "
          f"{r1.elapsed_total:.2f}s")

    opened = ledger.open_from_findings(r1.findings, on=july, cycle="2026-07")
    claimable = sum(c.claimed_paise for c in opened)
    print(f"opened {len(opened)} claims worth {rupees(claimable)}")

    decisions = triage_mod.triage(ledger, on=july)
    print(f"\ntriage")
    print(f"  chase alone   {len(decisions['chase']):>4} claims")
    for batch_id, group in decisions["batches"].items():
        total = sum(c.claimed_paise for c in group)
        print(f"  batched       {len(group):>4} claims  {rupees(total):>14}  {batch_id}")
    print(f"  not pursued   {len(decisions['dropped']):>4} claims  "
          f"{rupees(decisions['dropped_paise']):>14}")
    print(f"  filed total   {rupees(decisions['filed_paise'])}")

    # ---------------------------------------------------------------- cycle 2
    rule("CYCLE 2 — August 2026")
    cycle2 = next_cycle(
        ledger, seed=seed + 1, month_start=august, label="2026-08",
        n_orders=orders, plan=HARD_PLAN,
    )
    g2 = cycle2.generated
    r2 = pipeline.run_offline(g2.corpus)
    print(f"{g2.corpus.record_count():,} records, {len(r2.findings)} exceptions, "
          f"{r2.elapsed_total:.2f}s")
    print(f"gateway repaid {len(cycle2.repaid)} of July's claims (the engine cannot see this)")

    report = verify_mod.verify(ledger, g2.corpus, on=date(2026, 8, 31))
    print(f"\nverification")
    print(f"  matched by reference  {report.matched_by_reference}")
    print(f"  matched by value      {report.matched_by_value}")
    print(f"  partial repayments    {report.partial}")
    print(f"  ambiguous, left open  {len(report.ambiguous)}")
    print(f"  written off (aged)    {len(report.written_off)}")
    print(f"  recovered             {rupees(report.recovered_paise)}")

    # ---------------------------------------------------------------- scoring
    rule("DID THE LOOP WORK?")
    score = verify_mod.score_recovery(cycle2, ledger, report)
    print(f"gateway actually repaid   {score['repaid_claims']} claims, "
          f"{rupees(score['repaid_paise'])}")
    print(f"engine detected           {score['detected_claims']} claims, "
          f"{rupees(score['detected_paise'])}")
    print(f"detection rate            {100 * score['detection_rate']:.1f}% of claims, "
          f"{100 * score['value_rate']:.1f}% of value")
    print(f"false 'recovered'         {score['false_positives']}")

    rule("THE BOARD")
    totals = ledger.totals()
    print(f"claimed     {rupees(totals['claimed_paise'])}")
    print(f"recovered   {rupees(totals['recovered_paise'])}")
    print(f"outstanding {rupees(totals['outstanding_paise'])}")
    print(f"recovery rate on pursued claims: {100 * totals['recovery_rate']:.1f}%")
    print(f"states: {totals['by_state']}")

    print(f"\n{'REASON':<30}{'CLAIMS':>7}{'CLAIMED':>15}{'RECOVERED':>15}{'RATE':>8}")
    for reason, row in sorted(
        ledger.recovery_by_reason().items(), key=lambda kv: -kv[1]["claimed_paise"]
    ):
        print(f"{reason:<30}{row['claims']:>7}{rupees(row['claimed_paise']):>15}"
              f"{rupees(row['recovered_paise']):>15}{100 * row['rate']:>7.0f}%")

    print(f"\nlearned rates for next cycle (prior blended with observation):")
    for reason, rate in sorted(
        triage_mod.learned_rates(ledger).items(), key=lambda kv: -kv[1]
    )[:7]:
        prior = triage_mod.PRIOR_RECOVERY.get(reason, triage_mod.DEFAULT_PRIOR)
        print(f"  {reason:<30}{100 * rate:>6.1f}%   (prior {100 * prior:.0f}%)")

    aging = ledger.aging(date(2026, 8, 31))
    print(f"\naging of outstanding: " + "  ".join(
        f"{k}: {rupees(v)}" for k, v in aging.items() if v
    ) or "\naging: nothing outstanding")


if __name__ == "__main__":
    main()
