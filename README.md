# baaki

**बाकी** — *the remainder.* What's left over after the books are closed, and nobody can say where it went.

Baaki reconciles a merchant's four sources of payment truth — order ledger, gateway
payment report, settlement recon report, and bank statement — and reports the money
that does not add up, with a reason code and the evidence rows behind it.

It is not a matcher that reports "97% reconciled". It is an auditor that reports
**₹ recoverable**, and can show its working for every rupee.

> Razorpay AI Buildathon 2026 — Track 04, AI Finance Controller.

---

## The problem

A merchant closing the month has to prove that every rupee a customer paid arrived in
the bank, less exactly the fees that were contracted. The chain is four hops long:

```
Order --order_id--> Payment --payment_id--> SettlementRow --settlement_id--> Settlement --utr--> BankTxn
```

Each hop breaks in a different way, and each break is a different kind of money:

| Break | What it costs |
|---|---|
| Payment captured, never settled | principal, unrecovered |
| Settlement issued, no bank credit | principal, in transit or lost |
| Fee above the contracted rate | margin, silently |
| GST charged on gross instead of on fee | a wrong input-tax-credit claim |
| Refund or chargeback netted twice | principal, twice |
| Settled beyond T+2 | working capital |

Finance teams do this in spreadsheets. The spreadsheet finds the big breaks and misses
the systematic small ones, which are the expensive kind.

## What Baaki does

Three stages, in descending order of trust:

1. **Deterministic** — exact joins on `payment_id` / `settlement_id` / `utr`, plus fee,
   GST and net arithmetic recomputed from the contracted rate card. Reproducible,
   auditable, no model involved.
2. **Algorithmic** — UTR recovery from free-text bank narrations, and subset-sum
   matching where one bank credit covers several settlements. Scored, thresholded,
   still no model.
3. **LLM tail** — only the residue the first two stages cannot close: narrations that
   defeat parsing, genuinely ambiguous multi-candidate cases, and the analyst-facing
   write-up of each exception.

**An LLM never decides a match of record.** It proposes, the rules dispose, and
anything above a rupee threshold routes to a human. The reasoning is in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Status

Under active development for the buildathon submission. See [`FAILURES.md`](FAILURES.md)
for what broke along the way.

- [x] Money layer — integer paise end to end, no floats
- [x] Domain model and 12-code exception taxonomy
- [x] Synthetic corpus generator with ground-truth labelling
- [ ] Fault injector
- [ ] Deterministic and arithmetic matchers
- [ ] Fuzzy narration and subset-sum matching
- [ ] LLM tail resolver
- [ ] Evaluation harness and ablation
- [ ] Audit ledger and deterministic replay
- [ ] Dashboard

## Evaluation, honestly

The corpus is synthetic, and the generator is written as an adversary rather than as a
fixture:

- The generator and the matchers **share no code**. The generator formats bank
  narrations from templates; the parser recovers them with an independent
  implementation that has never seen those templates.
- Ground truth is the **injection log**, not the matcher's own output. Recall is the
  fraction of planted defects independently rediscovered.
- **Nothing flaggable exists in the corpus without a label.** A clean corpus must
  produce an empty exception queue, which is what makes the false-positive count mean
  something.
- Thresholds are tuned on one seed and reported on a **different, unseen seed**.

Numbers land in [`EVAL.md`](EVAL.md) as the stages come online. The headline metric is
not match rate — it is **precision on auto-matches**. In reconciliation a wrong
auto-match is far worse than an exception: an exception costs an analyst ten minutes,
a wrong auto-match silently closes a real loss.

## Running it

```bash
uv sync
uv run baaki generate --seed 7 --orders 4000 --out data/generated/dev
uv run baaki run --corpus data/generated/dev
```

The deterministic and algorithmic stages run fully offline. Only the tail resolver
needs `ANTHROPIC_API_KEY`; without it, Baaki reports the residue as unresolved instead
of guessing.

## Licence

MIT.
