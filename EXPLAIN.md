# baaki, explained simply

No jargon that isn't explained. Every example uses real numbers from a real run.

---

## 1. The situation

Aarti runs an online store. In July she sold about ₹42 lakh of goods through
Razorpay.

At the end of the month she has to answer one question:

> **Did all that money actually reach my bank account?**

Sounds easy. It isn't, because the money doesn't travel in one step.

---

## 2. Follow one single sale

A customer buys a ₹1,000 item with a Visa card. Here's every place that sale
shows up.

### Step 1 — the order (Aarti's own system)

```
order_id     order_0034000123
amount       ₹1,000.00
```

### Step 2 — the payment (Razorpay's payment report)

Razorpay takes the money from the customer, and takes its cut:

```
payment_id   pay_0034000123
amount       ₹1,000.00
fee          ₹20.00      ← 2% of ₹1,000, the agreed rate for Visa
GST on fee   ₹3.60       ← 18% of the ₹20 fee
```

So Aarti should get **₹1,000 − ₹20 − ₹3.60 = ₹976.40**.

### Step 3 — the settlement line (Razorpay's settlement report)

Razorpay doesn't send ₹976.40 on its own. It bundles a whole day together.

```
settlement_id   setl_003400012
entity_id       pay_0034000123
net             ₹976.40     ← this sale's share
```

That day's bundle might total **₹1,36,458.25** across 140 sales.

### Step 4 — the bank credit (Aarti's bank statement)

Two days later, one line appears in her bank:

```
date        2026-07-15
amount      ₹1,36,458.25
narration   NEFT-HDFCN20260715142969271-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT
```

That long code `HDFCN20260715142969271` is the **UTR** — a transfer's tracking
number, like a courier's consignment number.

### The full chain

```
Order ──▶ Payment ──▶ Settlement line ──▶ Settlement ──▶ Bank credit
₹1,000     ₹1,000       ₹976.40         ₹1,36,458.25    ₹1,36,458.25
           −₹23.60 fees                 (140 sales)     (should match)
```

**Every arrow can break.** baaki checks all four arrows, for every sale.

---

## 3. What actually goes wrong

Twelve things, each costing a different kind of money. Here's each one in plain
English, with the real total baaki found in a 12,158-record month.

### Money you can claim back

**Fee charged above the agreed rate** — `MDR_OVERCHARGE` · ₹240 over 40 payments
> The contract says 2% on Visa. Razorpay billed 2.45%. On a ₹1,000 sale that's
> ₹4.50. Nobody notices ₹4.50. It happened 40 times.
>
> This is why it's dangerous: **it's invisible per transaction and only visible
> in aggregate.** Exactly what a human reviewer will never catch.

**Tax computed wrongly** — `GST_MISCALC` · ₹3,660 over 18 payments
> GST is 18% **of the fee** (18% of ₹20 = ₹3.60).
> Someone charged 18% **of the sale** (18% of ₹1,000 = ₹180).
> That's ₹176.40 too much — and your tax filing is now wrong too.

**Customer paid, money never came** — `ORDER_PAID_NOT_SETTLED` · ₹13,664 over 12
> The payment was captured. It appears on no settlement. The customer's card was
> charged and Aarti never received it.

**Settlement sent, never arrived** — `SETTLED_NOT_IN_BANK` · ₹6,89,951 over 5
> Razorpay says "we sent ₹1,70,828 with UTR SBINN2026...". No such credit exists
> in the bank. The biggest single number on the board.

**Refund deducted twice** — `REFUND_DOUBLE_COUNTED` · ₹10,349 over 8
> Aarti refunded a customer ₹1,200. It was subtracted from her settlement on the
> 8th — and subtracted again on the 19th. She paid for one refund twice.

**Chargeback deducted twice** — `CHARGEBACK_NETTED_TWICE` · ₹463 over 2
> Same thing, for disputed transactions.

**Settlement total doesn't match its own lines** — `SETTLEMENT_AMOUNT_MISMATCH` · ₹5,127 over 3
> The settlement's header says ₹1,36,458. Add up its own 140 lines and you get
> ₹1,38,000. Razorpay paid the header amount, so ₹1,542 is unexplained.

### Not lost, but you need to know

**One order charged twice** — `DUPLICATE_PAYMENT` · ₹14,618 over 10
> The customer clicked Pay twice. Both went through. Refund it before they call
> their bank, because then it becomes a chargeback and costs more.
>
> Note: **nothing in Razorpay's own reports looks wrong here.** Both payments
> are perfectly valid. It's only visible against Aarti's order list. That's the
> argument for reading all four files instead of two.

**Paid late** — `LATE_SETTLEMENT` · ₹0
> Contract says money lands 2 days after the sale (called **T+2**). This one took
> 6 days. No money lost — but Aarti's cash was stuck for 4 extra days.
> **Reported as ₹0 impact on purpose.** Calling this "money found" would inflate
> the number that matters.

**Money on hold** — `SETTLEMENT_ON_HOLD` · ₹2,63,702 over 2
> Razorpay is holding it deliberately. Not missing. But it's not spendable cash
> either, so Aarti should know.

**Payment split into pieces** — `PARTIAL_BANK_CREDIT` · ₹0
> One ₹1,10,272 settlement arrived as two bank credits of ₹36,757 and ₹73,514.
> Nothing wrong — but naive matching sees neither amount matching and panics.

**Money in the bank that isn't from Razorpay** — `BANK_CREDIT_UNIDENTIFIED` · ₹8,19,749 over 23
> A ₹1,884 credit reading `SUNRISE LOGISTICS PRIVATE LIMITED-INV4097`. That's a
> customer paying an invoice directly, not a Razorpay settlement.
> **Not an error — but it must not be counted as gateway revenue.**

---

## 4. How baaki finds all this

Four stages. Each one is cheaper and more certain than the next, so each gets
first crack at the problem.

### Stage 1 — redo the maths

For every payment, recompute the fee and tax from the agreed rate card, and
compare.

```
Sale             ₹1,000.00
Contract rate    2.00%  (Visa)
Fee should be    ₹20.00
Fee charged      ₹24.50   ← doesn't match
Overcharged      ₹4.50
```

No cleverness. Just arithmetic Aarti could redo on paper.

> **One nice detail:** UPI has **zero** fee in India by law. So *any* fee on a
> UPI payment is automatically wrong. baaki knows that.

### Stage 2 — check the chain joins up

Simple list comparisons:

- Payments with no settlement line → *someone paid and you didn't get it*
- One `refund_id` appearing on two settlements → *charged twice*
- Two payments on one `order_id` → *customer double-charged*

**Stages 1 and 2 catch 77% of all problems** — with zero AI.

### Stage 3 — match settlements to the bank

This is the hard part, because the bank writes in messy free text.

**Easy:** find the UTR in the narration, match it.
```
NEFT-HDFCN20260715142969271-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT
     └────── found it, match ──────┘
```

**Harder — bank cut the code short:**
```
NEFT-HDFCN2026071514-RAZORPAY SOFT
```
Match on the first part, but only if it points to exactly one settlement.

**Harder — no code at all:**
```
NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT PAYOUT
```
Fall back to: same amount, same day, and only one possible match.

**Hardest — split payment:**
```
₹36,757.47  +  ₹73,514.94  =  ₹1,10,272.41  ✓ matches the settlement
```

**When two possibilities exist, baaki refuses to choose.** It flags it for a
human. Guessing has a 50% chance of marking a real settlement "received"
against someone else's money — and then it's gone silently.

### Stage 4 — the AI, on the leftovers only

By now almost everything is solved. What's left is two situations:

**A) Split into four pieces.** baaki's search only tries combinations of up to 3.
Four is beyond it.

**B) Two credits, identical amount, identical day.** Maths cannot help — *both*
add up correctly:
```
₹1,15,215.55  NEFT-...-RAZORPAY SOFTWARE PVT LTD-SETTLEMENT
₹1,15,215.55  NEFT-...-ARORA TEXTILES PRIVATE LIMITED-INV4097
```
Only **reading the words** tells you which is the gateway payout and which is a
customer's invoice payment. That is the one job here that arithmetic genuinely
cannot do.

**How much AI is used: 6 calls across 12,158 records. 0.05%.**

And even then — **the AI doesn't decide.** It suggests which credits go together.
Then code adds them up. If they don't total exactly right, the suggestion is
thrown away, no matter how confident the AI sounds.

> Why so strict? Because the worst bug in this project's history was written by
> *me*, not by an AI: code that matched a ₹1,10,272 settlement to a ₹36,757
> credit and marked it "done", because it matched the tracking number and never
> checked the amount. If handwritten code makes that mistake on the easy case, a
> language model will make it on the hard one.

---

## 5. What the results mean

```
12,158 records reconciled in 0.02s
₹7,23,457.53 recoverable across 88 findings
₹10,98,071.28 in timing and unattributed items (not a loss)
131 exceptions, 28 need a person (78.6% resolved automatically)
```

| number | plain meaning |
|---|---|
| **₹7,23,457 recoverable** | money Aarti can actually claim back |
| **₹10,98,071 timing/unattributed** | not lost — held, late, or needs identifying. **Kept separate on purpose**, so the first number stays honest |
| **131 exceptions** | things needing a look |
| **28 need a person** | baaki refused to guess on these |
| **78.6% automatic** | the rest closed without a human |

### How we know it's actually right

We built a **fake merchant designed to fool us**:

1. Generate a clean, correct month of books.
2. Plant **127 known errors** and write down exactly what was planted.
3. Run baaki, which **cannot read that answer sheet**.
4. Compare.

Result: **found 127 of 127. Zero false alarms.** On seven different randomly
generated months it had never seen.

Two rules keep this honest:
- The error-planting code and the error-finding code **share nothing**. The
  planter writes bank text from templates; the finder parses it with completely
  separate code that has never seen those templates.
- **Deleting the answer sheet changes nothing** in the output. That's tested.

### The most important table

Stages added one at a time:

| after this stage | problems found | **money found** |
|---|---|---|
| 1. arithmetic | 48% | **1.8%** |
| 2. + chain checks | 77% | **6.5%** |
| 3. + bank matching | 100% | **100%** |
| 4. + AI | 100% | 100% |

Look at those two columns disagreeing.

After stage 2 you've found **77% of the problems but 6.5% of the money.** Because
fee overcharges are many and tiny, while one missing settlement is a single row
worth ₹1.7 lakh.

**This is why baaki never reports a "match rate".** A tool proudly announcing
"77% reconciled" here has actually located **one rupee in every fifteen**.

---

## 6. Can you trust it?

**Every finding shows its evidence.** Not "this looks wrong" — the actual rows:

```
payments[pay_0034000123].amount     = ₹1,000.00
payments[pay_0034000123].fee        = ₹24.50
contract[card/visa].rate_bps        = 200
computed[pay_0034000123].expected   = ₹20.00
```

Aarti can check that by hand, or paste it into a support ticket.

**Everything is recorded.** Each finding is logged with the exact rule that
produced it. `baaki verify` re-runs the whole thing and confirms it produces
identical results:

```
corpus     match  a93c1f0aeb8f524c vs a93c1f0aeb8f524c
decisions  match  219bf4762df241cd vs 219bf4762df241cd
offline decisions reproduce exactly
```

**The AI part is honestly excluded from that.** An AI won't produce byte-identical
output twice, so instead of pretending, we log which model was used and what it
was asked, and leave it out of the guarantee. The part that decides money
deterministically can be replayed. The part that can't be replayed only ever
*suggested* something that arithmetic then had to confirm.

**No money is ever counted using decimals.** Everything is whole paise as integers.
Computers get `0.1 + 0.2 = 0.30000000000000004`, and being one paise wrong across
50,000 rows fills the exception list with noise until the analyst stops trusting it.

---

## 7. One paragraph, if someone asks

> Money crosses four systems between a customer paying and a merchant getting
> paid, and every hop can break in a way that costs a different kind of money.
> baaki reads all four, and reports **how much you can get back** with the rows
> that prove it — not a "% reconciled" figure that hides where the money went.
> 12,158 records in 0.02 seconds, ₹7.2 lakh found, and an AI is used on 0.05% of
> records — only where arithmetic provably cannot decide, and never allowed to
> decide anything on its own.
