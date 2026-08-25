"""Render a reconciliation run as a self-contained HTML statement.

No server, no build step, no external requests. The output is one file an
analyst can open, mail to their accountant, or attach to a ticket raised with
the gateway -- which is the point, since a finding only matters once somebody
acts on it.

The visual language is deliberately a ledger rather than a dashboard: hairline
rules, square corners, every figure in a tabular monospace face, and red
reserved for one thing only -- recoverable principal. Accounting has had a
colour convention for money for four hundred years and there is no reason to
invent another.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from pathlib import Path

from .models import REASON_META, Reason, Severity
from .money import rupees

SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}

_CSS = """
:root {
  --paper:      #fcfcfa;
  --surface:    #ffffff;
  --ink:        #16202b;
  --ink-soft:   #55636f;
  --ink-faint:  #8b97a2;
  --rule:       #dde3e8;
  --rule-firm:  #c3ccd4;
  --debit:      #b3341e;
  --credit:     #2f6b4f;
  --review:     #96661c;
  --shade:      #f2f4f6;
  --focus:      #16202b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper:     #0f1620;
    --surface:   #141d28;
    --ink:       #e4e9ee;
    --ink-soft:  #9aa7b3;
    --ink-faint: #6b7887;
    --rule:      #24313f;
    --rule-firm: #35465a;
    --debit:     #e4674c;
    --credit:    #5fa37e;
    --review:    #c9964a;
    --shade:     #18222e;
    --focus:     #e4e9ee;
  }
}
:root[data-theme="dark"] {
  --paper:     #0f1620;
  --surface:   #141d28;
  --ink:       #e4e9ee;
  --ink-soft:  #9aa7b3;
  --ink-faint: #6b7887;
  --rule:      #24313f;
  --rule-firm: #35465a;
  --debit:     #e4674c;
  --credit:    #5fa37e;
  --review:    #c9964a;
  --shade:     #18222e;
  --focus:     #e4e9ee;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.sheet {
  max-width: 1180px;
  margin: 0 auto;
  padding: 40px 28px 96px;
  display: flex;
  flex-direction: column;
  gap: 40px;
}

.num {
  font-family: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
}

.label {
  font-size: 10.5px;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
}

/* masthead ------------------------------------------------------------- */

.masthead {
  border-top: 2px solid var(--ink);
  border-bottom: 1px solid var(--rule-firm);
  padding: 18px 0 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 28px;
  align-items: baseline;
  justify-content: space-between;
}
.wordmark {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: 34px;
  line-height: 1;
  margin: 0;
  letter-spacing: -0.01em;
}
.wordmark small {
  display: block;
  font-family: inherit;
  font-size: 13px;
  font-style: italic;
  color: var(--ink-soft);
  letter-spacing: 0;
  margin-top: 6px;
}
.stamp { display: flex; gap: 26px; flex-wrap: wrap; text-align: right; }
.stamp div { display: flex; flex-direction: column; gap: 3px; }
.stamp .v { font-size: 13px; }

/* money line ----------------------------------------------------------- */

.money {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule);
}
.money > div { background: var(--surface); padding: 22px 24px; display: flex; flex-direction: column; gap: 8px; }
.money .fig { font-size: 30px; line-height: 1.1; letter-spacing: -0.02em; }
.money .fig.debit { color: var(--debit); }
.money .note { font-size: 12.5px; color: var(--ink-soft); }

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule);
}
.tiles > div { background: var(--surface); padding: 14px 16px; display: flex; flex-direction: column; gap: 5px; }
.tiles .v { font-size: 19px; }

/* sections ------------------------------------------------------------- */

section { display: flex; flex-direction: column; gap: 14px; }
h2 {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: 20px;
  font-weight: 600;
  margin: 0;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--rule-firm);
  text-wrap: balance;
}
.lede { margin: 0; color: var(--ink-soft); font-size: 13.5px; max-width: 68ch; }

.scroll { overflow-x: auto; }

table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th {
  text-align: left;
  font-size: 10.5px;
  letter-spacing: 0.11em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
  padding: 0 12px 8px;
  border-bottom: 1px solid var(--rule-firm);
  white-space: nowrap;
}
td { padding: 9px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th.r, td.r { text-align: right; }
tbody tr:hover { background: var(--shade); }

.sev { display: inline-block; width: 3px; height: 13px; vertical-align: -2px; margin-right: 8px; }
.sev-critical { background: var(--debit); }
.sev-high     { background: var(--review); }
.sev-medium   { background: var(--ink-faint); }
.sev-low      { background: var(--rule-firm); }
.sev-info     { background: var(--rule-firm); }

.code { font-size: 12px; letter-spacing: 0.02em; }
.stage {
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-soft);
  border: 1px solid var(--rule-firm);
  padding: 1px 5px;
  white-space: nowrap;
}
.flag { color: var(--review); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }

/* queue ---------------------------------------------------------------- */

.filters { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
button.f {
  font: inherit;
  font-size: 12px;
  color: var(--ink-soft);
  background: transparent;
  border: 1px solid var(--rule-firm);
  padding: 4px 11px;
  cursor: pointer;
}
button.f[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
button.f:focus-visible, tr.row:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

tr.row { cursor: pointer; }
tr.row td:first-child::before {
  content: "+";
  color: var(--ink-faint);
  margin-right: 9px;
  font-family: ui-monospace, monospace;
}
tr.row.open td:first-child::before { content: "\\2212"; }
tr.ev > td { background: var(--shade); padding: 0 12px 16px 40px; }
tr.ev.hidden { display: none; }
.ev-why { margin: 12px 0 12px; max-width: 84ch; font-size: 13px; }
.ev-list { display: flex; flex-direction: column; gap: 4px; margin: 0; padding: 0; list-style: none; }
.ev-list li { font-size: 12px; color: var(--ink-soft); }
.ev-act { margin-top: 12px; font-size: 12.5px; }
.ev-act b { font-weight: 600; }

footer {
  border-top: 1px solid var(--rule-firm);
  padding-top: 16px;
  color: var(--ink-faint);
  font-size: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
"""

_JS = """
(function () {
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr.row'));
  function toggle(row) {
    var ev = document.getElementById('ev-' + row.dataset.idx);
    if (!ev) return;
    var open = row.classList.toggle('open');
    ev.classList.toggle('hidden', !open);
    row.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  rows.forEach(function (row) {
    row.addEventListener('click', function () { toggle(row); });
    row.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(row); }
    });
  });

  var buttons = Array.prototype.slice.call(document.querySelectorAll('button.f'));
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'); });
      var want = btn.dataset.filter;
      rows.forEach(function (row) {
        var show = want === 'all'
          || (want === 'review' ? row.dataset.review === '1' : row.dataset.sev === want);
        row.style.display = show ? '' : 'none';
        var ev = document.getElementById('ev-' + row.dataset.idx);
        if (ev && !show) { ev.classList.add('hidden'); row.classList.remove('open'); }
        if (ev && show && !row.classList.contains('open')) { ev.classList.add('hidden'); }
      });
    });
  });
})();
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render(result, corpus, *, ledger=None, title: str = "Baaki Statement") -> str:
    """Build the full HTML document for a completed run."""
    findings = sorted(
        result.findings,
        key=lambda f: (SEVERITY_ORDER[f.severity], -f.impact_paise),
    )

    recoverable = sum(f.impact_paise for f in findings if f.recoverable)
    other = sum(f.impact_paise for f in findings if not f.recoverable)
    recoverable_n = sum(1 for f in findings if f.recoverable)
    needs_human = sum(1 for f in findings if f.requires_human)

    by_reason: dict[Reason, list] = {}
    for f in findings:
        by_reason.setdefault(f.reason, []).append(f)

    by_stage: dict[str, int] = {}
    for f in findings:
        by_stage[f.stage.value] = by_stage.get(f.stage.value, 0) + 1

    fingerprint = ledger.fingerprint()[:16] if ledger else "not recorded"
    corpus_sha = ledger.corpus_sha[:16] if ledger else "not recorded"

    reason_rows = []
    for reason, group in sorted(
        by_reason.items(), key=lambda kv: -sum(f.impact_paise for f in kv[1])
    ):
        meta = REASON_META[reason]
        total = sum(f.impact_paise for f in group)
        reason_rows.append(
            f"<tr>"
            f'<td><span class="sev sev-{meta["severity"].value}"></span>'
            f'<span class="num code">{_esc(reason.value)}</span></td>'
            f'<td class="r num">{len(group)}</td>'
            f'<td class="r num"{" style=\"color:var(--debit)\"" if meta["recoverable"] and total else ""}>'
            f"{_esc(rupees(total))}</td>"
            f'<td>{"recoverable" if meta["recoverable"] else "timing / attribution"}</td>'
            f"<td>{_esc(meta['action'])}</td>"
            f"</tr>"
        )

    queue_rows = []
    for idx, f in enumerate(findings):
        meta = REASON_META[f.reason]
        evidence = "".join(f"<li>{_esc(e.render())}</li>" for e in f.evidence)
        flag = '<span class="flag">needs a person</span>' if f.requires_human else ""
        amount_style = ' style="color:var(--debit)"' if f.recoverable and f.impact_paise else ""
        queue_rows.append(
            f'<tr class="row" tabindex="0" role="button" aria-expanded="false" '
            f'data-idx="{idx}" data-sev="{meta["severity"].value}" '
            f'data-review="{1 if f.requires_human else 0}">'
            f'<td><span class="sev sev-{meta["severity"].value}"></span>'
            f'<span class="num code">{_esc(f.entity_id)}</span></td>'
            f'<td class="num code">{_esc(f.reason.value)}</td>'
            f'<td class="r num"{amount_style}>{_esc(rupees(f.impact_paise))}</td>'
            f'<td><span class="stage">{_esc(f.stage.value)}</span></td>'
            f'<td class="r num">{f.confidence:.2f}</td>'
            f"<td>{flag}</td>"
            f"</tr>"
            f'<tr class="ev hidden" id="ev-{idx}"><td colspan="6">'
            f'<p class="ev-why">{_esc(f.explanation)}</p>'
            f'<div class="label">evidence</div>'
            f'<ul class="ev-list num">{evidence}</ul>'
            f'<p class="ev-act"><b>Next step.</b> {_esc(meta["action"])}</p>'
            f"</td></tr>"
        )

    tail = result.tail
    if tail.skipped:
        tail_note = (
            "The tail stage did not run, so items it might have resolved are left "
            "escalated. Every figure above comes from arithmetic and exact joins."
        )
    else:
        tail_note = (
            f"The tail stage made {tail.calls} model call(s) against "
            f"{corpus.record_count():,} records "
            f"({100 * tail.calls / max(1, corpus.record_count()):.4f}%), producing "
            f"{tail.proposals} proposal(s) of which {tail.accepted} passed the "
            f"guardrails and {tail.rejected} were rejected. A proposed match is "
            f"accepted because the credits sum to the settlement, never because the "
            f"model was confident."
        )

    stage_line = ", ".join(f"{n} {stage}" for stage, n in sorted(by_stage.items()))

    return f"""<title>{_esc(title)}</title>
<style>{_CSS}</style>
<div class="sheet">

  <header class="masthead">
    <h1 class="wordmark">बाकी
      <small>the remainder &mdash; what the books cannot account for</small>
    </h1>
    <div class="stamp">
      <div><span class="label">records</span>
<span class="v num">{corpus.record_count():,}</span></div>
      <div><span class="label">reconciled in</span>
<span class="v num">{result.elapsed_total:.2f}s</span></div>
      <div><span class="label">ledger fingerprint</span>
<span class="v num">{_esc(fingerprint)}</span></div>
      <div><span class="label">corpus</span>
<span class="v num">{_esc(corpus_sha)}</span></div>
    </div>
  </header>

  <div class="money">
    <div>
      <span class="label">recoverable principal</span>
      <span class="fig num debit">{_esc(rupees(recoverable))}</span>
      <span class="note">Across {recoverable_n} finding(s). Money the merchant can
      claim back: fees above contract, tax miscalculated, deductions taken twice,
      payments that never settled.</span>
    </div>
    <div>
      <span class="label">timing and attribution</span>
      <span class="fig num">{_esc(rupees(other))}</span>
      <span class="note">Not a loss of principal. Held settlements, late payouts and
      credits that need identifying before they are booked as revenue. Reported
      separately so the figure on the left stays honest.</span>
    </div>
  </div>

  <div class="tiles">
    <div><span class="label">exceptions</span><span class="v num">{len(findings)}</span></div>
    <div><span class="label">need a person</span><span class="v num">{needs_human}</span></div>
    <div><span class="label">resolved automatically</span>
<span class="v num">{100 * result.coverage():.1f}%</span></div>
    <div><span class="label">reason codes hit</span>
<span class="v num">{len(by_reason)} of {len(list(Reason))}</span></div>
    <div><span class="label">model calls</span>
<span class="v num">{0 if tail.skipped else tail.calls}</span></div>
  </div>

  <section>
    <h2>By reason</h2>
    <p class="lede">Ordered by rupee impact rather than by count. The two are not the
    same: fee overcharges are numerous and individually trivial, while a settlement
    that never reached the bank is a single row worth six figures.</p>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>reason</th><th class="r">n</th><th class="r">impact</th>
          <th>kind</th><th>next step</th>
        </tr></thead>
        <tbody>{"".join(reason_rows)}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Exception queue</h2>
    <p class="lede">Most severe first. Open a row for the records behind it &mdash;
    every finding cites the rows an analyst would need to check the arithmetic by
    hand, or to attach to a ticket.</p>
    <div class="filters">
      <span class="label" style="margin-right:4px">show</span>
      <button class="f" data-filter="all" aria-pressed="true">all</button>
      <button class="f" data-filter="critical" aria-pressed="false">critical</button>
      <button class="f" data-filter="high" aria-pressed="false">high</button>
      <button class="f" data-filter="medium" aria-pressed="false">medium</button>
      <button class="f" data-filter="review" aria-pressed="false">needs a person</button>
    </div>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>entity</th><th>reason</th><th class="r">impact</th>
          <th>decided by</th><th class="r">conf.</th><th></th>
        </tr></thead>
        <tbody>{"".join(queue_rows)}</tbody>
      </table>
    </div>
  </section>

  <footer>
    <div>Decided by: {_esc(stage_line)}.</div>
    <div>{_esc(tail_note)}</div>
    <div>Generated {datetime.now(UTC):%d %b %Y %H:%M} UTC. Offline stages are a pure
    function of the books and replay to the fingerprint above; run
    <span class="num">baaki verify</span> to confirm.</div>
  </footer>

</div>
<script>{_JS}</script>
"""


def write(result, corpus, out: Path, *, ledger=None, title: str = "Baaki Statement") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result, corpus, ledger=ledger, title=title), encoding="utf-8")
    return out
