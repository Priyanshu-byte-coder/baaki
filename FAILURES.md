# What broke

A running log of the failures worth learning from, in the order they happened.
Each entry is the symptom, what actually caused it, the fix, and what now stops
it coming back.

---

## 1. The evaluation harness scored a perfect run over an empty denominator

**Symptom.** After wiring up fault injection I printed planted-versus-requested
counts per reason code, expecting a full board. Instead only `MDR_OVERCHARGE`
had anything in it — 32 out of a requested 40 — and the other ten injectors had
planted **zero**. Total ground truth was 50 findings, of which 18 were the
statement noise the generator emits anyway.

**Cause.** Every injector reserved the entities it touched so that no two faults
could land on the same thing and make recall ambiguous. Payment-level injectors
were reserving both the payment *and its enclosing `settlement_id`*:

```python
if not self._claim(payment.payment_id, row.settlement_id):
    continue
```

That looked harmless. It is not, because of the shape of the data: 4,000 orders
produce roughly 3,600 captured payments but only **43 settlements**, since a
settlement batches a whole day at a time. `mdr_overcharge` ran first, walked a
shuffled list of payments, and after 40 claims it had reserved 40 of the 43
settlements in the entire month. Every injector that ran after it found almost
nothing left to claim and silently planted nothing.

**Why it was dangerous rather than merely wrong.** The failure was silent and it
biased in the flattering direction. Per-code recall is `found / planted`. With
`planted == 0` for ten of eleven codes, any recall calculation over those codes
either divides by zero or, with the usual guard, reports 100%. I would have had
a scoreboard showing near-perfect recall across the taxonomy, produced by an
engine that had never been shown a single instance of nine of those defects. A
metric that fails towards *looking good* is worse than one that crashes.

**Fix.** Namespace the claims by entity kind — `pay:`, `order:`, `setl:`,
`ent:` — so a payment-level fault reserves only the payment. Two MDR overcharges
inside one settlement are not ambiguous; they are attributed to different
payments and the propagation is additive. Only genuinely settlement-scoped
faults reserve the settlement.

That exposed a second, real ordering constraint underneath. `partial_bank_credit`
splits one bank credit into two, and it is the only injector that changes how
many bank lines a settlement maps to. Anything that propagates to that
settlement afterwards adjusts one half of a split credit and quietly breaks the
invariant that the parts sum to the header. So the fault plan is applied in
insertion order: entity-level faults first while each settlement still has
exactly one credit to absorb them, then settlement-level faults, with
`partial_bank_credit` last.

**What stops it recurring.**

- `test_every_injector_plants_its_full_requested_count` asserts planted equals
  requested for every entry in the plan. A starved injector now fails the build
  instead of quietly reporting a perfect score.
- `test_only_declared_mismatches_break_the_header_invariant` asserts that the
  set of settlements whose header disagrees with the sum of their rows is
  *exactly* the set deliberately planted as `SETTLEMENT_AMOUNT_MISMATCH`. This
  is the general guard: no injector may create a defect it did not declare. If
  ordering breaks propagation again, this test catches it as a set difference
  rather than as a mysterious accuracy drop weeks later.

**The transferable lesson.** The bug was not in the reservation logic, which did
exactly what it said. It was in assuming the entities being reserved were
roughly equinumerous. Payments outnumber settlements by about 85 to 1 here, so
reserving a settlement to protect a payment was spending a scarce resource to
protect an abundant one. When a scarce key and an abundant key share a
namespace, the scarce one is the budget, and it runs out first.

---

## 2. The matcher closed a ₹1,10,272 settlement against a ₹36,757 credit, at full confidence

**Symptom.** With the bank-matching stage wired up, the scoreboard read 98.4%
recall and 98.4% precision. Two false positives, both bank credits reported as
unidentified, and two missed `PARTIAL_BANK_CREDIT` defects. Four errors, and the
counts on each side matching that neatly is usually one bug wearing two hats.

**Cause.** It was. The first matching pass recovers a UTR from the bank
narration and joins it to the settlement quoting the same UTR:

```python
for candidate in extract_utrs(txn.narration):
    owners = by_utr.get(candidate)
    if owners and len(owners) == 1 and owners[0] in self.open_settlements:
        self._commit(owners[0], [bank_id], "exact_utr", 1.00)
        break
```

When a settlement is paid out as two bank credits, **both credits quote the same
UTR** — they are two halves of one transfer. This loop took whichever half it
reached first, committed the settlement against it at confidence `1.00`, removed
the settlement from the open set, and moved on. The other half then matched
nothing and was reported as an unidentified credit.

Concretely, on seed 7: settlement `setl_000700030`, net **₹1,10,272.41**, was
marked reconciled against a single credit of **₹36,757.47**. The engine declared
that settlement fully accounted for while ₹73,514.94 of it sat unexplained three
rows further down the same statement.

**Why this is the worst bug in the project so far.** Every other defect makes
the queue longer. This one makes it *shorter*, and wrongly. The finding that
should have said "₹73,514 of this settlement never arrived" was instead a green
tick plus an unrelated-looking orphan credit that an analyst would plausibly
write off as someone else's transfer. It also carried `confidence = 1.00`, so
nothing downstream had any reason to question it. This is precisely the failure
mode the module docstring was written to warn about, and I shipped it into the
pass I trusted most.

**Cause behind the cause.** Matching on an identifier proves two records *refer*
to each other. It does not prove the money arrived. I had treated a strong
reference match as sufficient and never asserted the amounts agreed, because on
the happy path they always do — a settlement has one credit, the credit is the
full value, and the check looks redundant. It is redundant right up until the
one-to-one assumption fails, which is exactly the case the pass most needed to
handle correctly.

**Fix.** Reference resolution now gathers *every* open credit quoting a
reference and commits the group only if it sums to the settlement net:

- sum equals net, one credit → an ordinary match
- sum equals net, several credits → a split payout, reported as
  `PARTIAL_BANK_CREDIT`
- sum does not equal net → nothing is committed. The settlement stays open for
  later passes, and the group is pushed onto the ambiguous residue for a human
  or the LLM stage. A right reference with the wrong money is a question, not
  an answer.

That single change took the run from 98.4/98.4 to 100/100 on seed 7, because
both the false positives and both the missed splits were this one defect.

**What stops it recurring.** The rule is now stated once, in
`_resolve_by_reference`, and both UTR passes go through it — there is no second
place to forget the amount check. The invariant to hold onto: **no pass may
close a settlement without demonstrating that the credits it matched sum to the
settlement value.** Confidence describes how sure the engine is that it found
the right records, never that the money is right; the money is arithmetic and
gets checked every time.

---

## 3. A find-and-replace with `count=1` corrupted the ground truth, and the scorer hid it

**Symptom.** The tail stage run printed `recall 99.2%` alongside `misses 0`.
Those cannot both be true. Recall is `found / planted`; if nothing was missed,
recall is 100%.

**Cause, first layer.** Two defects in the injection log shared a
`(reason, entity)` pair — `PARTIAL_BANK_CREDIT` planted twice on
`setl_000700030`. Scoring matches on that pair, so the two collapsed into one
matchable key while `planted` still counted both. The denominator was one
larger than the number of things that could ever be matched, which caps recall
below 100% with an empty missed list.

**Cause, second layer.** When I namespaced the claim keys while fixing failure
1, I applied the rename with a script that used `str.replace(old, new, 1)`.
Five settlement-scoped injectors contained the byte-identical line

```python
if not self._claim(settlement.settlement_id):
```

and only the first was rewritten to `f"setl:{...}"`. The other four kept
claiming the raw id.

Nothing broke, because the four raw-id claimers were still mutually exclusive
*with each other* — they all used the same key format. The inconsistency was
invisible until hard mode added injectors using the `setl:` prefix. Those
claimed `"setl:setl_000700030"` while `partial_bank_credit` had claimed
`"setl_000700030"`, so the reservation check compared two different strings,
found no conflict, and let both plant on the same settlement.

**Why it survived a green test suite.** The isolation test existed and passed.
It ran `DEFAULT_PLAN` only, where every settlement-scoped injector shared the
raw format and mutual exclusion still held. The bug lived exactly in the gap
between the two plans, which is the one combination nothing exercised.

**Fix.** Three separate changes, because there were three separate defects:

1. Namespace all five call sites, with a replace that has no count argument.
2. `partial_bank_credit` claimed its settlement *before* checking the credit
   was large enough to split, then bailed out — burning a reservation on a
   settlement it planted nothing in. Guard first, claim second.
3. **The scorer now refuses to score a corrupt ground truth.** Duplicate
   `(reason, entity)` keys raise instead of quietly deflating recall.

That third one matters most. The first two were ordinary bugs. The third is
the reason I saw this at all: the number was *wrong in a way that was visible*
only because I printed misses next to recall. Had the report shown recall
alone, 99.2% would have looked like an honest result and I would have shipped
it.

**What stops it recurring.** `test_no_two_defects_share_a_reason_and_entity` is
parametrised across both plans and all seven sweep seeds — fourteen
combinations, where the old test covered one. The scorer raising on duplicates
means the failure is now loud at the point of measurement, not silent.

**The transferable lesson, and it is not "be careful with replace".** It is
that a metric which can only be checked against itself will not tell you when
it is broken. Recall looked plausible. It took a second, redundant number
printed beside it — the missed list — for the contradiction to become visible.
Every headline number in `EVAL.md` now has a companion that must agree with it:
recall against misses, precision against the false-positive list, money
identified against money planted. Redundancy in a report is not clutter; it is
the only error detection a metric has.

---

## 4. Rupee output crashed the Windows console

**Symptom.** The first smoke test of the money formatter died on
`UnicodeEncodeError: 'charmap' codec can't encode character '₹'`.

**Cause.** Windows consoles default to cp1252, which has no code point for `₹`.
Nothing to do with the formatting logic — the string was correct and the
terminal could not render it.

**Fix.** Force UTF-8 on the process rather than degrading the output to `Rs.`.
The CLI sets its own stream encoding so a rupee report is a rupee report on
every platform. Worth recording because the instinct is to "fix" it by changing
the data, and the data was never wrong.
