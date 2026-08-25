"""Ad-hoc evaluation driver used during development.

Prints the per-reason board for a plan across a set of seeds. The packaged
equivalent lands in the CLI; this exists so the numbers can be checked without
installing anything.
"""

from __future__ import annotations

import sys
import time

from baaki.corpus.defects import DEFAULT_PLAN, HARD_PLAN, inject
from baaki.corpus.generate import generate
from baaki.evaluation.score import by_stage, score
from baaki.match import arithmetic, deterministic, fuzzy
from baaki.models import Reason
from baaki.money import rupees


def run_once(seed: int, orders: int, plan: dict):
    g = inject(generate(seed=seed, n_orders=orders), seed=seed, plan=plan)
    started = time.perf_counter()
    findings = arithmetic.run(g.corpus) + deterministic.run(g.corpus)
    bank_findings, residue, stats = fuzzy.run(g.corpus)
    findings += bank_findings
    elapsed = time.perf_counter() - started
    return g, findings, score(findings, g.truth), residue, stats, elapsed


def board(name: str, plan: dict, seed: int, orders: int) -> None:
    g, findings, s, residue, stats, elapsed = run_once(seed, orders, plan)
    print(f"\n{'=' * 78}\n{name}  (seed {seed}, {g.corpus.record_count():,} records)\n{'=' * 78}")
    print(f"{'REASON':<30}{'PLANT':>7}{'FOUND':>7}{'FP':>6}{'RECALL':>9}{'IMPACT':>9}")
    for reason in Reason:
        b = s.by_reason[reason]
        if b.planted == 0 and b.false_positives == 0:
            continue
        rec = "-" if not b.planted else f"{100 * b.recall:.0f}%"
        imp = "-" if not b.found else f"{100 * b.impact_accuracy:.0f}%"
        print(f"{reason.value:<30}{b.planted:>7}{b.found:>7}{b.false_positives:>6}{rec:>9}{imp:>9}")

    planted_money, found_money = s.money(recoverable_only=True)
    print(f"\nrecall     {100 * s.recall:5.1f}%   ({s.found}/{s.planted})")
    print(f"precision  {100 * s.precision:5.1f}%   ({s.false_positives} false positive(s))")
    print(f"money      {rupees(found_money)} identified of {rupees(planted_money)} recoverable")
    print(f"stages     {by_stage(findings)}")
    print(f"bank       {stats}")
    print(f"residue    {residue.size()} item(s) for the tail stage")
    print(f"time       {elapsed:.3f}s")

    if s.spurious:
        print("\nfalse positives:")
        for f in s.spurious[:6]:
            print(f"   {f.reason.value:<28}{f.entity_id:<22}{rupees(f.impact_paise):>14}")
    if s.missed:
        print("\nmissed:")
        for d in s.missed[:6]:
            print(f"   {d.reason.value:<28}{d.entity_id:<22}{d.note[:40]}")


def sweep(name: str, plan: dict, seeds: list[int], orders: int) -> None:
    print(f"\n{name} across {len(seeds)} seeds")
    print(f"{'seed':<7}{'records':>9}{'plant':>7}{'recall':>9}{'precision':>11}{'money':>9}{'time':>8}")
    for seed in seeds:
        g, _findings, s, _r, _st, elapsed = run_once(seed, orders, plan)
        planted_money, found_money = s.money(recoverable_only=True)
        pct = 100 * found_money / planted_money if planted_money else float("nan")
        print(
            f"{seed:<7}{g.corpus.record_count():>9,}{s.planted:>7}"
            f"{100 * s.recall:>8.1f}%{100 * s.precision:>10.1f}%{pct:>8.1f}%{elapsed:>7.3f}s"
        )


if __name__ == "__main__":
    orders = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    if len(sys.argv) > 2:
        board("HARD MODE", HARD_PLAN, int(sys.argv[2]), orders)
    else:
        board("DEFAULT PLAN", DEFAULT_PLAN, 7, orders)
        board("HARD MODE", HARD_PLAN, 7, orders)
        sweep("HARD MODE", HARD_PLAN, [13, 21, 34, 55, 89, 101], orders)
