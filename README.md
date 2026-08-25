# baaki

**बाकी** — *the remainder.* What's left over after the books are closed, and nobody can say where it went.

Baaki reconciles a merchant's four sources of payment truth — order ledger, gateway
payment report, settlement recon report, and bank statement — and reports the money that
does not add up, with a reason code and the evidence rows behind it.

It is not a matcher that reports "97% reconciled". It is an auditor that reports
**₹ recoverable**, and can show its working for every rupee.

> Razorpay AI Buildathon 2026 — Track 04, AI Finance Controller.

```
12,158 records reconciled in 0.01s
₹7,23,457.53 recoverable across 88 finding(s)
₹10,98,071.28 in timing and unattributed items (not a loss of principal)
131 exception(s), 28 need a person (78.6% resolved automatically)
```

---

## The problem

The chain is four hops long, and each hop breaks in a different way:

```
Order ──order_id──▶ Payment ──payment_id──▶ SettlementRow ──settlement_id──▶ Settlement ──utr──▶ BankTxn
```

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

## Results

Hard mode, 12,158 records, seven held-out seeds. Full tables and method in
[`EVAL.md`](EVAL.md).

| | recall | precision | money identified | time |
|---|---|---|---|---|
| offline (no model, no network) | 100% | 96.9–100% | **100%** | **0.01s** |
| with the tail stage | 100% | **100%** | **100%** | ~10s |

Stage ablation, cumulative — read the money column:

| cumulative stage | findings | recall | precision | **money** |
|---|---|---|---|---|
| 1 arithmetic | 61 | 48.0% | 100.0% | **1.8%** |
| 2 + deterministic joins | 98 | 77.2% | 100.0% | **6.5%** |
| 3 + bank matching | 131 | 100.0% | 96.9% | **100.0%** |
| 4 + tail (model) | 127 | 100.0% | **100.0%** | 100.0% |

The first two stages find **77% of the defects and 6.5% of the money**. Counts and
rupees concentrate in opposite places, which is why "77% reconciled" would be a
misleading headline for a run that had located one rupee in fifteen.

**Model invocation rate: 6 calls over 12,158 records — 0.0494%.** Its entire measured
contribution is precision, 96.9% → 100%. It changes no recall and no money.

## Where AI is used, and where it isn't

The rule the design rests on:

> **An LLM never decides a match of record.**

It proposes which credits belong to a settlement. Whether that becomes a finding is
decided by adding them up. A match is accepted *because the credits sum to the
settlement net* — the model only suggested which ones to add.

It is used for exactly two things arithmetic provably cannot reach: splits wider than
the bounded combination search, and candidates identical in value and date, where both
sum correctly and only the narration separates a `RAZORPAY … SETTLEMENT` from an
`ARORA TEXTILES … INV4097`. Reasoning in [`ARCHITECTURE.md`](ARCHITECTURE.md).

Every proposal runs a guardrail battery — schema, taxonomy, evidence grounded in what
the model was actually shown, credits summing to the settlement, a ₹25,000 ceiling above
which a person signs off, and a confidence *we* compute rather than one the model claims
about itself. Twelve adversarial tests cover it, none needing an API key.

## Evaluation, honestly

The corpus is synthetic, and the generator is written as an adversary rather than a
fixture:

- Generator and matchers **share no code**. Narrations come from templates; the parser
  is written against the RBI UTR spec and has never seen them.
- Ground truth is the **injection log**, not the engine's output.
- **Nothing flaggable exists without a label** — a clean corpus must produce an empty
  queue, which is what makes false positives measurable.
- The answer key is **unreachable**: deleting it changes no finding, and a run from CSV
  is byte-identical to a run from memory.
- Thresholds were only ever inspected on seed 7. Every other seed was run once, after.

`EVAL.md` also states what this *cannot* tell you, and where the engine fails on purpose.

## Running it

```bash
pip install -e ".[dev]"

baaki doctor                                              # what's configured
baaki generate --seed 34 --orders 4000 --plan hard --out data/generated/demo
baaki run --corpus data/generated/demo --ledger data/runs/demo.jsonl
baaki verify --ledger data/runs/demo.jsonl --corpus data/generated/demo
baaki eval --plan hard --no-tail
```

Stages 1–3 run fully offline. Only the tail needs keys (`.env`, see `.env.example`);
without them Baaki reports its residue as unresolved instead of guessing.

## Audit and replay

Every finding is logged with its stage, its named rule, the records cited and the rupee
value claimed. The offline decision stream hashes to a fingerprint `baaki verify`
re-derives:

```
corpus     match  a93c1f0aeb8f524c vs a93c1f0aeb8f524c
decisions  match  219bf4762df241cd vs 219bf4762df241cd
offline decisions reproduce exactly
```

Tail decisions are logged with model id and prompt hash and **excluded** from that
fingerprint — a model call is not bit-reproducible even at temperature zero, and
claiming otherwise would be the easy lie.

## What broke

[`FAILURES.md`](FAILURES.md) — four entries, written up properly. The one worth reading
is #2: a deterministic pass closed a ₹1,10,272 settlement against a ₹36,757 credit at
confidence 1.00, because it matched on an identifier and never checked the money. That
bug made the exception queue *shorter*, which is the direction that loses money quietly.

## Tests

61, running in 1.5s. `pytest`

## Licence

MIT.
