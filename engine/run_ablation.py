"""Stage ablation: what each layer of the pipeline actually contributes.

Run cumulatively, cheapest stage first, so each row shows what is left for the
next one to do. The shape of this table is the design argument: if the model
row moved the numbers a lot, the earlier stages would be doing too little; if
it moved nothing, it would not belong in the pipeline.
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
from baaki.match import arithmetic, deterministic, fuzzy, llm, pipeline  # noqa: E402
from baaki.money import rupees  # noqa: E402


def row(label: str, findings, truth, extra: str = "") -> None:
    s = score(findings, truth)
    planted, found = s.money(recoverable_only=True)
    pct = 100 * found / planted if planted else 0.0
    print(
        f"{label:<34}{len(findings):>7}{100 * s.recall:>9.1f}%"
        f"{100 * s.precision:>11.1f}%{pct:>9.1f}%{s.false_positives:>6}"
        f"{len(s.missed):>7}   {extra}"
    )


def main() -> None:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 34
    orders = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    g = inject(generate(seed=seed, n_orders=orders), seed=seed, plan=HARD_PLAN)
    corpus = g.corpus

    print(f"hard mode, seed {seed}, {corpus.record_count():,} records, {len(g.truth)} planted\n")
    print(f"{'CUMULATIVE STAGE':<34}{'FIND':>7}{'RECALL':>10}{'PRECISION':>11}{'MONEY':>9}{'FP':>6}{'MISS':>7}")

    a = arithmetic.run(corpus)
    row("1  arithmetic", a, g.truth)

    d = a + deterministic.run(corpus)
    row("2  + deterministic joins", d, g.truth)

    bank, residue, stats = fuzzy.run(corpus)
    f = d + bank
    row("3  + bank matching", f, g.truth)

    client = LLMClient()
    if client.available:
        result = pipeline.run(corpus, client=client)
        row("4  + tail (model)", result.findings, g.truth,
            f"{result.tail.calls} call(s)")
        calls = result.tail.calls
        print(
            f"\nmodel invocation rate: {calls} call(s) over {corpus.record_count():,} "
            f"records = {100 * calls / corpus.record_count():.4f}% of records"
        )
        print(
            f"guardrails: {result.tail.proposals} proposal(s), "
            f"{result.tail.accepted} accepted, {result.tail.rejected} rejected"
        )
    else:
        print("\n(no keys configured; stage 4 skipped)")

    print(f"\nbank matching passes: {stats}")
    recoverable = sum(x.impact_paise for x in f if x.recoverable)
    print(f"recoverable identified offline: {rupees(recoverable)}")


if __name__ == "__main__":
    main()
