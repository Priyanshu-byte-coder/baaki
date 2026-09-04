# baaki

**बाकी** — *the remainder.* What's left over after the books are closed, and nobody can say where it went.

Baaki reconciles a merchant's four sources of payment truth — order ledger, gateway
payment report, settlement recon report, and bank statement — finds the money that does
not add up, decides which of it is worth chasing, files those claims, and then **goes
back the following month to check whether the money actually came back**.

It is not a matcher that reports "97% reconciled". It closes the loop, and the number it
finishes on is **₹ recovered**, not ₹ found.

> Razorpay AI Buildathon 2026 — Track 04, AI Finance Controller.

```
2026-07   12,158 records, 131 exceptions, 0.01s
          opened 88 claims · filed ₹7,22,779.56 · not pursued ₹405.52 (40 claims)

2026-08   12,156 records, 127 exceptions, 0.01s
          recovered ₹2,86,554.06  (25 by reference, 8 by value, 2 partial)

2026-09   12,140 records, 129 exceptions, 0.01s
          recovered ₹4,85,781.80  (26 by reference, 12 by value, 7 written off)

claimed ₹16,07,690.34 · recovered ₹7,72,335.86 · outstanding ₹6,53,767.13
recovery rate on pursued claims: 48.1%
verifier: detected ₹7,72,335.86 of ₹7,72,465.48 actually repaid, 0 false recoveries
```

**A claim is recovered when the rupees are found again in a later settlement** — never
because it was filed, and never because the gateway said so.

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

## Match rate

The track asks for it by name. Per hop, because one number hides which join is failing:

| hop | matched | of | rate |
|---|---|---|---|
| payment → settlement line | 3,572 | 3,584 | 99.67% |
| settlement → bank credit | 24 | 29 | 82.76% |
| bank credit → attributed | 33 | 56 | 58.93% |
| **overall — records in no exception** | 12,027 | 12,158 | **98.92%** |

A single "98.92% matched" would be true and almost useless. It is not the headline
here for the reason the ablation shows: after the deterministic stages the engine has
matched **77% of the defects and located 6.5% of the money**.

## Closing the loop

Detection is half a loop. The other half is deciding what to do and checking it worked.

**Triage prices every claim** before anyone touches it:

```
expected value  =  claimed × P(recovery | reason)  −  cost of asking
```

Three outcomes, all recorded on the claim: chase it alone, batch it with others of the
same reason, or drop it. **Dropping is a decision on the record**, not an item quietly
falling off a list.

Batching is where the value is. Forty fee overcharges of six rupees are individually
worthless — ₹120 of analyst time to recover ₹6 — and collectively one ticket. In the run
above, **40 claims worth ₹405 were deliberately not pursued**, because chasing them costs
more than they return. Baaki says so instead of padding the queue.

**Verification uses the same discipline as bank matching.** A repayment arrives as an
adjustment line in a later settlement; the verifier finds it by reference, or by value
uniquely in both directions, and refuses to guess when two claims could match one
adjustment.

**Recovery rate by reason code** falls out of this, and exists in no other reconciliation
tool I could find:

| reason | claims | claimed | recovered | rate |
|---|---|---|---|---|
| `SETTLED_NOT_IN_BANK` | 12 | ₹15,11,344.82 | ₹7,27,251.64 | 48% |
| `REFUND_DOUBLE_COUNTED` | 22 | ₹21,420.18 | ₹14,961.88 | 70% |
| `SETTLEMENT_AMOUNT_MISMATCH` | 9 | ₹19,040.41 | ₹12,377.70 | 65% |
| `ORDER_PAID_NOT_SETTLED` | 28 | ₹35,653.91 | ₹11,503.06 | 32% |
| `CHARGEBACK_NETTED_TWICE` | 6 | ₹5,619.51 | ₹755.91 | 13% |

That tells a merchant *which fights are worth having*, and it feeds next month's triage.

**And a pure expected-value policy never learns about what it declines.** Every prior
started as a guess; if a guess is pessimistic, expected value drops the category forever
and no evidence ever arrives to correct it. So while a reason has no track record, one
claim from each rejected batch is filed anyway as a probe. `MDR_OVERCHARGE` was dropped
40 times and probed once — the probe came back, and its rate is now evidence rather than
my guess. Exploration stops after five resolved claims: bounded by evidence, not run
forever.

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
about itself. Nine adversarial tests cover it, none needing an API key.

**On the model choice.** I wanted Claude — Razorpay's own Agent Studio is built on the
Claude Agent SDK, so matching that stack was the obvious call. I don't have a paid
Anthropic key and wasn't going to make the demo depend on a trial credit, so the tail
runs on Groq's free tier (`gpt-oss-120b`), verified live. The client is
provider-agnostic and nothing the model says is trusted anyway: a weaker model yields
*fewer accepted proposals*, never wrong ones. Full reasoning in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

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

The recovery loop is measured the same way: the generator records exactly which claims
it repaid, the verifier cannot read that record, and detection is scored against it.

`EVAL.md` also states what this *cannot* tell you, and where the engine fails on purpose.

## Running it

```bash
pip install -e ".[dev]"

baaki doctor                                              # what's configured
baaki generate --seed 34 --orders 4000 --plan hard --out data/generated/demo
baaki run --corpus data/generated/demo --ledger data/runs/demo.jsonl --report out.html
baaki verify --ledger data/runs/demo.jsonl --corpus data/generated/demo
baaki eval --plan hard --no-tail

# the whole loop, three settlement periods, with a recovery board
baaki cycle --seed 34 --cycles 3 --out data/runs/loop --report recovery.html
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

[`FAILURES.md`](FAILURES.md) — five entries, written up properly. The one worth reading
is #2: a deterministic pass closed a ₹1,10,272 settlement against a ₹36,757 credit at
confidence 1.00, because it matched on an identifier and never checked the money. That
bug made the exception queue *shorter*, which is the direction that loses money quietly.

## Tests

75, running in 1.8s. `pytest`

## Licence

MIT.
