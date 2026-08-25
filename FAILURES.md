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

## 2. Rupee output crashed the Windows console

**Symptom.** The first smoke test of the money formatter died on
`UnicodeEncodeError: 'charmap' codec can't encode character '₹'`.

**Cause.** Windows consoles default to cp1252, which has no code point for `₹`.
Nothing to do with the formatting logic — the string was correct and the
terminal could not render it.

**Fix.** Force UTF-8 on the process rather than degrading the output to `Rs.`.
The CLI sets its own stream encoding so a rupee report is a rupee report on
every platform. Worth recording because the instinct is to "fix" it by changing
the data, and the data was never wrong.
