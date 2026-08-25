# Evaluation

Every number here is reproducible with the commands beside it. Nothing is
hand-picked; where a result is unflattering it is in the table with the rest.

```bash
baaki eval --plan hard --no-tail          # the offline board
baaki eval --plan hard --tail             # with the model stage
python engine/run_ablation.py 34 4000     # the stage-by-stage table
```

---

## What is measured, and why these numbers and not others

**Precision and recall are reported separately and never averaged.** They are
not symmetric here. A missed defect costs an analyst the time to find it by
hand. A false auto-match silently closes a real loss — the money is gone and
the report says everything reconciled. An F1 would hide which of those is
failing, so there is no F1.

**Coverage and correctness are also kept apart.** Coverage is the share of
findings resolved without a person. Correctness is precision. A pipeline that
escalates everything is useless and a pipeline that auto-closes everything is
dangerous, and a single blended "match rate" cannot tell you which one you
have.

**Money is tracked independently of counts.** Recall counts findings; the money
column counts rupees. They diverge sharply, and that divergence is the most
useful thing in the report — see the ablation below.

**Impact accuracy.** Naming the right defect on the right payment is half the
job. If the recoverable amount is wrong, the claim gets rejected. Every matched
finding is also checked for an exact rupee match against the injection log.

---

## Method, and its honest limits

The corpus is synthetic. That is a real limitation and it is worth stating
plainly rather than burying: **these numbers measure the engine against a
generator I also wrote.** Four things are done to stop that being circular.

**The generator shares no code with the matchers.** It formats bank narrations
from templates; `baaki.match.fuzzy` recovers references with a parser written
against the RBI UTR specification, which has never seen those templates.

**Ground truth is the injection log, not the engine's own output.** Recall is
the share of planted defects independently rediscovered.

**Nothing flaggable exists without a label.** A clean corpus must produce an
empty exception queue, which is what makes the false-positive count mean
anything (`test_a_clean_corpus_only_reports_unattributed_credits`).

**The answer key is unreachable.** It is written as `_ground_truth.json`
alongside the CSVs, and the engine has no code path that opens it. Deleting it
changes not one finding, and a run from disk produces byte-identical results to
a run from memory — so nothing leaked through the object graph either
(`test_the_answer_key_is_never_read_by_the_engine`,
`test_running_from_csv_matches_running_from_memory`).

**Seeds are held out.** Thresholds were only ever inspected on seed 7. Every
other seed in these tables was run once, after the fact.

What this still cannot tell you: whether the *distribution* is right. Real bank
narrations are stranger than eight templates, real merchants have fee
structures with tiers and slabs, and real settlement files have encodings that
would defeat the CSV reader. The engine's behaviour on ambiguity is measured
here; its behaviour on the ambiguity it has never seen is not.

---

## Two fault plans

`DEFAULT_PLAN` plants defects and asks whether the engine finds them.
`HARD_PLAN` adds four ambiguity traps and asks whether it knows when to stop.

| trap | what it attacks |
|---|---|
| `coincident_noise_credit` | an unrelated credit of identical value and date |
| `utr_prefix_collision` | two references sharing a truncation-length prefix |
| `corrupted_reference` | O-for-zero, I-for-one mangling in the narration |
| `oversized_split` | a four-way payout, past the bounded search cap |

---

## Results

### Offline — no model, no network

`baaki eval --plan default --no-tail`

| seed | records | planted | recall | precision | money | coverage | time |
|---|---|---|---|---|---|---|---|
| 7 | 12,104 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.01s |
| 13 | 12,075 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.01s |
| 21 | 12,136 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.01s |
| 34 | 12,149 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.01s |
| 55 | 12,137 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.01s |
| 89 | 12,075 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.01s |
| 101 | 12,136 | 122 | 100.0% | 100.0% | 100.0% | 82.8% | 0.02s |

A clean sweep on the default plan is **not** presented as a strong result. It
is what prompted hard mode: an engine scoring 100% has been graded by a weak
adversary, not proved correct.

`baaki eval --plan hard --no-tail`

| seed | records | planted | recall | precision | money | coverage | time |
|---|---|---|---|---|---|---|---|
| 7 | 12,113 | 127 | 100.0% | 100.0% | 100.0% | 81.1% | 0.02s |
| 13 | 12,084 | 127 | 100.0% | 100.0% | 100.0% | 81.1% | 0.01s |
| 21 | 12,145 | 127 | 100.0% | **96.9%** | 100.0% | 78.6% | 0.01s |
| 34 | 12,158 | 127 | 100.0% | **96.9%** | 100.0% | 78.6% | 0.01s |
| 55 | 12,146 | 127 | 100.0% | **96.9%** | 100.0% | 78.6% | 0.02s |
| 89 | 12,084 | 127 | 100.0% | **96.9%** | 100.0% | 78.6% | 0.02s |
| 101 | 12,145 | 127 | 100.0% | **98.4%** | 100.0% | 79.8% | 0.02s |

**Money identified stays at 100% on every seed.** The traps cost precision and
never principal, which is the direction this system has to fail in.

### With the tail stage

`baaki eval --plan hard --tail`

| seed | recall | precision | money | coverage | time |
|---|---|---|---|---|---|
| 21 | 100.0% | **100.0%** | 100.0% | 81.1% | 10.04s |
| 34 | 100.0% | **100.0%** | 100.0% | 81.1% | 8.89s |
| 55 | 100.0% | **100.0%** | 100.0% | 81.1% | 10.21s |
| 89 | 100.0% | **100.0%** | 100.0% | 81.1% | 39.65s |

Seed 89's 39.65s is rate-limit backoff on a shared free-tier key pool, not
compute. It is in the table because it is what happened.

---

## Stage ablation — the design argument

`python engine/run_ablation.py 34 4000`, cumulative, cheapest stage first:

| cumulative stage | findings | recall | precision | **money** | FP | missed |
|---|---|---|---|---|---|---|
| 1 arithmetic | 61 | 48.0% | 100.0% | **1.8%** | 0 | 66 |
| 2 + deterministic joins | 98 | 77.2% | 100.0% | **6.5%** | 0 | 29 |
| 3 + bank matching | 131 | 100.0% | 96.9% | **100.0%** | 4 | 0 |
| 4 + tail (model) | 127 | 100.0% | **100.0%** | 100.0% | 0 | 0 |

Read the money column. **The first two stages find 77% of the defects and 6.5%
of the money.** Defect counts and rupees are concentrated in opposite places:
fee overcharges are numerous and individually trivial, while a settlement that
never reached the bank is a single row worth six figures. Any system reporting
"77% reconciled" after stage 2 would be describing a run that had located one
rupee in fifteen.

**Stage 4 changes no recall and no money.** Its entire contribution is
precision, 96.9% → 100%, by resolving four escalations the bounded search and
the refuse-to-guess policy necessarily produced.

### How little the model is used

```
model invocation rate: 6 calls over 12,158 records = 0.0494% of records
guardrails: 2 proposals, 2 accepted, 0 rejected
```

**Roughly one call per two thousand records.** Everything else is closed by
arithmetic and exact joins that an analyst can redo by hand.

The two accepted proposals were accepted because the guardrail added the
credits up and they summed to the settlement net — not because the model was
confident. Its self-reported confidence is read and discarded
(`test_model_self_reported_confidence_is_ignored`).

### Bank matching, by pass

```
exact_utr 9   prefix_utr 2   amount_date 8   split_credit 5
unmatched settlements 5   unmatched credits 23   ambiguous 2
```

`amount_date` firing 8 times is `corrupted_reference` working as intended: both
reference passes come up empty and the engine degrades to value and date rather
than declaring the settlement missing.

---

## Where it fails, precisely

**Four-way splits with no recoverable reference.** There are two paths that
close a split payout and only one is capped. The reference path sums *every*
credit quoting a UTR, so it closes a split of any width. The combination search
is the fallback when no reference survives, and it stops at three parts because
past that the search space grows faster than the evidence does. A four-way
split is therefore missed only when both conditions hold at once. When it is
missed, the settlement is still escalated — never dropped
(`test_the_only_defect_the_offline_stages_may_miss_is_a_referenceless_split`).

**Two candidates of identical value and date.** The engine refuses to match and
escalates both. The scorer counts that as two false positives. It is still the
right call: guessing has a 50% chance of marking a real settlement reconciled
against someone else's money. This is most of the 96.9% precision figure, and
it is a policy choice, not a bug.

**Non-determinism above the pipeline.** The offline decision stream hashes to a
stable fingerprint that `baaki verify` re-derives. The tail stage is not
bit-reproducible even at temperature zero, so its decisions are logged with
model id and prompt hash and excluded from that fingerprint rather than being
claimed as replayable.

---

## What would have to change for real data

- Fee contracts are a flat rate card here. Real ones have slabs, promotional
  periods and per-BIN pricing; `baaki.contract` would become a lookup with
  effective dates.
- Narrations come from eight templates. Real statements need a much wider
  parser and, more importantly, an honest measurement of how often the parser
  fails — which is exactly what the residue counter already reports.
- The corpus is one merchant, one month, one bank account. Multi-account and
  marketplace splits (Route-style commission and TDS) are not modelled.
- 180,259 records reconcile in 0.18s in memory. Real batches would stream, and
  the subset-sum search would need per-day partitioning rather than a whole-set
  scan.
