# Architecture

## The chain

A merchant closing the month has to prove every rupee a customer paid arrived
in the bank, less exactly the fees that were contracted. Four sources, four
hops, and one identifier joining each:

```
Order ──order_id──▶ Payment ──payment_id──▶ SettlementRow ──settlement_id──▶ Settlement ──utr──▶ BankTxn
 │                    │                          │                              │                   │
 merchant's own    gateway payment          gateway settlement            gateway settlement    the bank's
 order ledger      report                   recon report (lines)          report (header)       statement
```

Only the last source is written by someone with no stake in the numbers. That
is what makes it the proof, and also what makes it the hardest to read: the
narration is free text produced by the remitting bank.

Every hop that fails to join is money, and each failure is a different *kind*
of money. Twelve reason codes, in `baaki.models.Reason`, seven of which are
recoverable principal and five of which are timing or attribution.

---

## Four stages, in descending order of trust

Each stage is cheaper, more certain and more auditable than the next, so each
gets the chance to close an item before anything less certain sees it. By the
time a model is invoked, the only things left are those that provably cannot be
settled by arithmetic.

```
┌─ 1  arithmetic ──────────────────────── no model, no joins, no thresholds ─┐
│  Recompute every fee, tax and settlement total from the contracted rate    │
│  card. Either the number reconciles or it does not. confidence = 1.0       │
└───────────────────────────────────────────────────────────────────────────┘
                                     │  residue
┌─ 2  deterministic ─────────────────▼── no model, exact joins only ────────┐
│  Set differences along the chain. A captured payment with no settlement    │
│  line is not a judgement call. confidence = 1.0                           │
└───────────────────────────────────────────────────────────────────────────┘
                                     │  residue
┌─ 3  algorithmic ───────────────────▼── no model, scored and thresholded ──┐
│  exact UTR 1.00 │ prefix UTR 0.97 │ value+date 0.90 │ split sum 0.85      │
│  Uniqueness enforced both directions. Ambiguity escalates, never guesses.  │
└───────────────────────────────────────────────────────────────────────────┘
                                     │  residue  (≈0.05% of records)
┌─ 4  tail ──────────────────────────▼── model proposes, arithmetic decides ─┐
│  Splits past the search cap; candidates equal on value and date where the  │
│  narration is the only discriminator. Every proposal guardrailed.          │
└───────────────────────────────────────────────────────────────────────────┘
```

Stages 1–3 need no network. If stage 4 cannot run, the report is less explained
and still correct — the residue stays escalated rather than guessed at.

---

## Where AI is used, and where it deliberately is not

This is the part worth arguing about, so here is the reasoning rather than the
conclusion.

### Not used: anything arithmetic settles

A fee 45 basis points over contract is found by recomputing the fee. A refund
deducted twice is found by counting occurrences of a `refund_id`. A settlement
header disagreeing with its own lines is found by adding the lines up. Putting
a model anywhere near these would make a certain answer probabilistic, slower,
and impossible to hand to an analyst as arithmetic they can redo. **77% of
defects are closed before any model exists in the pipeline.**

### Not used: deciding a match of record

The single rule the whole design rests on:

> **An LLM never decides a match of record.**

It may propose which credits belong to a settlement. Whether that proposal
becomes a finding is decided by adding them up. When a match is accepted it is
accepted *because the credits sum to the settlement net* — the model only
suggested which ones to add.

This is not caution for its own sake. The worst bug in this project's history
(`FAILURES.md` #2) was a **deterministic** pass closing a ₹1,10,272 settlement
against a ₹36,757 credit at confidence 1.00, because it matched on an
identifier and never checked the money. If hand-written code makes that mistake
on the happy path, a model will make it on the strange one.

### Not used: the model's own confidence

Read and discarded. A model asked how sure it is answers fluently and without
calibration, and a number that looks like evidence but isn't is worse than no
number. Confidence in `baaki.match.guardrails` is computed from which checks
passed.

The first live call made against this pipeline asked the tail model which model
family it belonged to. It answered `"GPT-4"`, in valid JSON, confidently, and
wrongly — it is `gpt-oss-120b`. Well-formed, assured and false, and no schema
validation would ever have caught it.

### Used: semantic judgement that arithmetic cannot reach

Two cases, both provable rather than assumed:

**Splits wider than the bounded search.** The combination search stops at three
parts because past that the search space grows faster than the evidence does. A
model can look at four credits and propose they belong together; the guardrail
then adds them up.

**Candidates identical in value and date.** Here arithmetic has nothing left to
say — *both* sum correctly. The discriminator is the text: one narration reads
`RAZORPAY SOFTWARE PVT LTD SETTLEMENT`, the other `ARORA TEXTILES PRIVATE
LIMITED INV4097`. Telling a gateway payout from a customer's invoice payment is
semantic, and it is the one job here a regular expression genuinely cannot do.

**Cost of that judgement: 6 calls across 12,158 records, 0.0494%.** Its entire
measured contribution is precision 96.9% → 100%. It changes no recall and no
money.

### Used: writing the exception up for a human

The analyst-facing dossier — what broke, which rows prove it, what to do next.
No money impact, so a poor sentence costs a sentence.

---

## The guardrail battery

Every tail proposal runs a fixed set of checks. Four are fatal.

| check | fatal | what it catches |
|---|---|---|
| `SCHEMA_INVALID` | ✓ | malformed object |
| `UNKNOWN_REASON` | ✓ | a reason code invented outside the taxonomy |
| `UNGROUNDED_ENTITY` | ✓ | citing a record it was not shown |
| `ARITHMETIC_FAILED` | ✓ | proposed credits do not sum to the settlement |
| `NO_EVIDENCE` | | a claim with nothing cited |
| `EXCEEDS_AUTONOMY` | | above ₹25,000 — sound, but a person signs it |
| `LOW_CONFIDENCE` | | below threshold *after* our own scoring |

**Grounding is checked against the prompt scope, not the corpus.** A model
citing a real settlement it was never shown has still guessed; on a
50,000-record book, plausible identifiers are cheap to guess.

Nine adversarial tests cover this, none of which need an API key, because the
guardrails are pure functions over a proposal. That separation is deliberate:
the part that must never be wrong is also the part that is cheapest to test.

---

## Money

Every amount is an `int` count of paise. No floats anywhere, `Decimal` only at
the CSV ingest boundary. Fee and GST tolerances are **zero** — fees follow a
documented rule with documented rounding, so any difference is real, and a
tolerance would be a place for a systematic overcharge to hide.

Recoverable principal and non-recoverable items (late settlements, split
credits, held settlements) are totalled separately. Folding an SLA breach into
a "money found" headline would inflate the number that matters most.

---

## Audit and replay

Every finding is appended to a decision log with its stage, its **named rule**
(`arithmetic.check_fees/rate_vs_contract`), the records cited, and the rupee
value claimed. A finding becomes a claim against a gateway; "the system said
so" does not survive that conversation.

Reproducibility is split, deliberately:

- **Offline stages** are pure functions of the books. The decision stream hashes
  to a fingerprint that `baaki verify` re-derives and compares. The hash is
  order-independent across stages but sensitive to every rupee.
- **The tail stage** is not bit-reproducible even at temperature zero. Its
  decisions are logged with model id and prompt hash, and *excluded* from the
  fingerprint rather than claimed as replayable.

That split is the honest claim: the part that decides money deterministically
can be replayed and verified, and the part that cannot be replayed is confined
to proposals arithmetic had to confirm.

---

## Layout

```
engine/baaki/
  money.py            integer paise, Indian formatting, bps arithmetic
  models.py           domain entities, 12 reason codes, severity and actions
  contract.py         the merchant's rate card — shared by generator and engine
  corpus/
    generate.py       the adversary: four sources + ground truth
    defects.py        11 fault injectors + 4 ambiguity traps
    io.py             CSV round-trip; answer key written but never readable
  match/
    arithmetic.py     stage 1
    deterministic.py  stage 2
    fuzzy.py          stage 3 — UTR recovery, subset sums
    guardrails.py     what the model may do, and how it is checked
    llm.py            stage 4 — retracts escalations, never adds accusations
    pipeline.py       orchestration
    findings.py       Finding, Evidence, Residue
  llm/
    budget.py         TPM sliding window, key pool  (from HelioOps)
    client.py         retries, rotation, truncation detection
  evaluation/score.py scoring; raises on corrupt ground truth
  audit/ledger.py     append-only decisions, fingerprint, replay
  cli.py              generate / run / eval / verify / doctor
```

`contract.py` is shared ground on purpose: the generator applies the merchant's
agreed terms and the engine verifies against them. That is not circular, it is
what reconciliation *is*. What never crosses from `corpus/` into `match/` is
the adversary's knowledge of how the books were built — which bank line paid
which settlement, which narration template was used, where a fault was planted.
