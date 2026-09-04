# Sample output

Two statements produced by the commands in the project README, committed so you
can read the output without running anything. Open either file in a browser.

| file | what it is |
|---|---|
| `statement.html` | One month reconciled — 12,158 records, the exception queue, and the evidence behind every finding. |
| `recovery.html` | Three settlement cycles of the recovery loop — what was claimed, what was filed, and what actually came back. |

Regenerate them with:

```bash
baaki generate --seed 34 --orders 4000 --plan hard --out data/demo/july
baaki run --corpus data/demo/july --ledger data/demo/july.jsonl --report samples/statement.html
baaki cycle --seed 34 --cycles 3 --out data/demo/loop --report samples/recovery.html
```

Everything is seeded, so these files reproduce byte for byte apart from the
generation timestamp in the footer.
