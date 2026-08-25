"""Compare the offline pipeline against the full pipeline on the same books.

The point of the comparison is the delta: what does the tail stage actually buy
on the cases the earlier stages had to escalate, and what did it cost.
"""

from __future__ import annotations

import os
import pathlib
import sys

env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from baaki.corpus.defects import HARD_PLAN, inject  # noqa: E402
from baaki.corpus.generate import generate  # noqa: E402
from baaki.evaluation.score import score  # noqa: E402
from baaki.llm.client import LLMClient  # noqa: E402
from baaki.match import pipeline  # noqa: E402
from baaki.money import rupees  # noqa: E402


def line(label: str, result, s) -> None:
    planted, found = s.money(recoverable_only=True)
    pct = 100 * found / planted if planted else float("nan")
    print(
        f"{label:<12}{100 * s.recall:>8.1f}%{100 * s.precision:>11.1f}%"
        f"{pct:>9.1f}%{s.false_positives:>6}{len(s.missed):>8}"
        f"{100 * result.coverage():>10.1f}%{result.elapsed_total:>9.2f}s"
    )


def main() -> None:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 34
    orders = int(sys.argv[2]) if len(sys.argv) > 2 else 4000

    g = inject(generate(seed=seed, n_orders=orders), seed=seed, plan=HARD_PLAN)
    print(f"hard mode, seed {seed}, {g.corpus.record_count():,} records\n")
    print(f"{'':<12}{'RECALL':>9}{'PRECISION':>11}{'MONEY':>9}{'FP':>6}{'MISS':>8}{'COVERAGE':>10}{'TIME':>10}")

    offline = pipeline.run_offline(g.corpus)
    line("offline", offline, score(offline.findings, g.truth))

    client = LLMClient()
    if not client.available:
        print("\nno keys configured; tail stage cannot run")
        return

    full = pipeline.run(g.corpus, client=client)
    full_score = score(full.findings, g.truth)
    line("with tail", full, full_score)

    r = full.tail
    print(
        f"\ntail: {r.calls} call(s), {r.proposals} proposal(s), "
        f"{r.accepted} accepted, {r.rejected} rejected"
    )
    if r.flags:
        print(f"      guardrail flags {r.flags}")
    if r.errors:
        print(f"      errors {r.errors[:3]}")

    offline_score = score(offline.findings, g.truth)
    print(
        f"\ndelta: false positives {offline_score.false_positives} -> "
        f"{full_score.false_positives}, misses {len(offline_score.missed)} -> "
        f"{len(full_score.missed)}"
    )

    if full_score.spurious:
        print("\nremaining false positives:")
        for f in full_score.spurious[:6]:
            print(f"   {f.reason.value:<28}{f.entity_id:<24}{rupees(f.impact_paise):>14}")


if __name__ == "__main__":
    main()
