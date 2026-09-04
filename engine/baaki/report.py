"""Render a reconciliation run as a self-contained HTML statement.

No server, no build step, no external requests. One file an analyst can open,
mail to their accountant, or attach to a ticket raised with the gateway --
which is the point, since a finding only matters once somebody acts on it.

The page is written for a person deciding what to do on a Monday morning, not
for a machine dumping its state. So it answers, in order: *am I all right*,
*how much can I get back*, *what do I do first*, and only then *show me
everything*. The exception table is the last section, not the first.

Two colours carry meaning and nothing else is coloured: a deep ledger red for
money that can be claimed back, a deep green for money that came back.
Accounting has had a colour convention for four hundred years and there is no
reason to invent another. Committed to a light ground on purpose -- this is a
statement, and statements are printed on paper.
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

#: Rows of the exception queue shown before the reveal. Enough to see the shape
#: of the problem; not so many that the page becomes a data dump.
QUEUE_PREVIEW = 12

_CSS = """
:root {
  --paper:     #fafaf8;
  --card:      #ffffff;
  --ink:       #191c1f;
  --ink-soft:  #5a6470;
  --ink-faint: #949ca6;
  --rule:      #e6e8ea;
  --rule-soft: #f0f1f2;
  --debit:     #a3271a;
  --debit-wash:#fdf5f3;
  --credit:    #2c6a4a;
  --credit-wash:#f2f8f4;
  --review:    #8a6416;
  --shade:     #f6f6f4;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

.sheet {
  max-width: 940px;
  margin: 0 auto;
  padding: 48px 32px 120px;
  display: flex;
  flex-direction: column;
  gap: 64px;
}

.num {
  font-family: ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
}

.serif {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
}

.label {
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
}

/* masthead ------------------------------------------------------------- */

.masthead {
  display: flex;
  flex-wrap: wrap;
  gap: 20px 40px;
  align-items: baseline;
  justify-content: space-between;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--rule);
}
.mark {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: 26px;
  margin: 0;
  font-weight: 600;
}
.mark span { color: var(--ink-faint); font-weight: 400; font-style: italic; font-size: 15px; }
.meta { display: flex; gap: 28px; flex-wrap: wrap; }
.meta div { display: flex; flex-direction: column; gap: 2px; }
.meta .v { font-size: 13px; color: var(--ink-soft); }

/* the verdict ---------------------------------------------------------- */

.verdict { display: flex; flex-direction: column; gap: 18px; }
.headline {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: clamp(30px, 5vw, 46px);
  line-height: 1.18;
  margin: 0;
  font-weight: 600;
  letter-spacing: -0.015em;
  text-wrap: balance;
  max-width: 22ch;
}
.headline .amount { color: var(--debit); display: block; }
.headline .amount.zero { color: var(--credit); }
.subhead {
  margin: 0;
  font-size: 18px;
  color: var(--ink-soft);
  max-width: 56ch;
  line-height: 1.55;
}

.split { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }
.split > div {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 22px 24px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.split .claim { background: var(--debit-wash); border-color: #f0dcd7; }
.split .fig { font-size: 26px; line-height: 1.15; letter-spacing: -0.01em; }
.split .claim .fig { color: var(--debit); }
.split p { margin: 0; font-size: 14px; color: var(--ink-soft); line-height: 1.5; }

/* sections ------------------------------------------------------------- */

section { display: flex; flex-direction: column; gap: 20px; }
h2 {
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: 24px;
  font-weight: 600;
  margin: 0;
  letter-spacing: -0.01em;
}
.lede { margin: -8px 0 0; color: var(--ink-soft); font-size: 15px; max-width: 64ch; }
.note { margin: 0; color: var(--ink-faint); font-size: 13.5px; max-width: 68ch; line-height: 1.55; }

/* do this first -------------------------------------------------------- */

.actions { display: flex; flex-direction: column; gap: 0; }
.action {
  display: grid;
  grid-template-columns: 34px 1fr auto;
  gap: 4px 18px;
  padding: 20px 0;
  border-bottom: 1px solid var(--rule);
  align-items: baseline;
}
.action:first-child { border-top: 1px solid var(--rule); }
.rank {
  font-family: ui-serif, Georgia, serif;
  font-size: 21px;
  color: var(--ink-faint);
  line-height: 1.2;
}
.action h3 { margin: 0; font-size: 17px; font-weight: 600; line-height: 1.35; }
.action .money { font-size: 18px; white-space: nowrap; color: var(--debit); }
.action .money.neutral { color: var(--ink-soft); }
.action .why { grid-column: 2 / -1; margin: 0; font-size: 14.5px; color: var(--ink-soft); max-width: 62ch; }
.action .count { grid-column: 2 / -1; margin: 0; font-size: 13px; color: var(--ink-faint); }

/* tables --------------------------------------------------------------- */

.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14.5px; }
th {
  text-align: left;
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-faint);
  font-weight: 600;
  padding: 0 14px 10px 0;
  border-bottom: 1px solid var(--rule);
  white-space: nowrap;
}
td { padding: 12px 14px 12px 0; border-bottom: 1px solid var(--rule-soft); vertical-align: top; }
th:last-child, td:last-child { padding-right: 0; }
th.r, td.r { text-align: right; }
tbody tr:last-child td { border-bottom: 1px solid var(--rule); }

.reason-name { font-weight: 500; }
.kind { font-size: 13px; color: var(--ink-faint); }
.claimable { color: var(--debit); }
.recovered { color: var(--credit); }

/* match rate ----------------------------------------------------------- */

.hops { display: flex; flex-direction: column; }
.hop {
  display: grid;
  grid-template-columns: 1fr 120px 74px;
  gap: 18px;
  align-items: center;
  padding: 13px 0;
  border-bottom: 1px solid var(--rule-soft);
}
.hop:last-child { border-bottom: none; border-top: 1px solid var(--rule); font-weight: 600; }
.hop .bar { height: 6px; background: var(--shade); border-radius: 3px; overflow: hidden; }
.hop .bar span { display: block; height: 100%; background: var(--ink-faint); }
.hop:last-child .bar span { background: var(--ink); }
.hop .pct { text-align: right; font-size: 15px; }
.hop .of { font-size: 13px; color: var(--ink-faint); }

/* recovery funnel ------------------------------------------------------ */

.funnel { display: flex; flex-direction: column; }
.fstage {
  display: grid;
  grid-template-columns: 190px 1fr 170px;
  gap: 24px;
  align-items: center;
  padding: 16px 0;
  border-bottom: 1px solid var(--rule-soft);
}
.fstage:last-child { border-bottom: none; }
.fbar { height: 10px; background: var(--shade); border-radius: 5px; overflow: hidden; }
.fbar span { display: block; height: 100%; background: #c9ccd0; }
.fstage:last-child .fbar span { background: var(--credit); }
.fval { text-align: right; font-size: 17px; }
.fstage:last-child .fval { color: var(--credit); font-weight: 600; }
.fname { font-size: 15px; color: var(--ink); font-weight: 500; }
.fname .label { display: block; margin-top: 1px; font-weight: 600; }

.callout {
  background: var(--card);
  border: 1px solid var(--rule);
  border-left: 3px solid var(--ink-faint);
  border-radius: 3px;
  padding: 18px 22px;
  font-size: 14.5px;
  color: var(--ink-soft);
  line-height: 1.6;
}
.callout b { color: var(--ink); font-weight: 600; }

/* exception queue ------------------------------------------------------ */

.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
button {
  font: inherit;
  font-size: 13.5px;
  color: var(--ink-soft);
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 6px 14px;
  cursor: pointer;
}
button:hover { border-color: var(--ink-faint); }
button[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
button:focus-visible, tr.row:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }

tr.row { cursor: pointer; }
tr.row:hover td { background: var(--shade); }
tr.row .disclose { color: var(--ink-faint); font-family: ui-monospace, monospace; margin-right: 10px; }
tr.row.open .disclose::before { content: "\\2013"; }
tr.row .disclose::before { content: "+"; }
tr.ev.hidden, tr.row.hidden { display: none; }
tr.ev > td { background: var(--shade); padding: 4px 14px 22px 30px; border-bottom: 1px solid var(--rule); }
.ev-why { margin: 6px 0 16px; max-width: 78ch; font-size: 14.5px; color: var(--ink); }
.ev-list { display: flex; flex-direction: column; gap: 5px; margin: 8px 0 0; padding: 0; list-style: none; }
.ev-list li { font-size: 13px; color: var(--ink-soft); }
.ev-act { margin: 16px 0 0; font-size: 14px; }
.ev-act b { font-weight: 600; }
.more { align-self: flex-start; }

.sev { display: inline-block; width: 3px; height: 14px; vertical-align: -2px; margin-right: 10px; border-radius: 1px; }
.sev-critical { background: var(--debit); }
.sev-high     { background: var(--review); }
.sev-medium   { background: var(--ink-faint); }
.sev-low, .sev-info { background: #d4d8dc; }

.stage-tag { font-size: 11px; letter-spacing: 0.07em; text-transform: uppercase; color: var(--ink-faint); }
.flag { color: var(--review); font-size: 12px; letter-spacing: 0.05em; text-transform: uppercase; white-space: nowrap; }

footer {
  border-top: 1px solid var(--rule);
  padding-top: 22px;
  color: var(--ink-faint);
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  line-height: 1.6;
}

@media (max-width: 620px) {
  .sheet { padding: 32px 20px 80px; gap: 48px; }
  .action { grid-template-columns: 26px 1fr; }
  .action .money { grid-column: 2; }
  .hop { grid-template-columns: 1fr 80px; }
  .fstage { grid-template-columns: 1fr 130px; }
  .hop .bar, .fbar { display: none; }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
@media print {
  .filters, button { display: none; }
  tr.ev { display: table-row !important; }
  .sheet { max-width: none; padding: 0; }
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

  var expanded = false;
  var more = document.getElementById('more');
  if (more) {
    more.addEventListener('click', function () {
      expanded = !expanded;
      rows.forEach(function (row) {
        if (parseInt(row.dataset.idx, 10) >= """ + str(QUEUE_PREVIEW) + """) {
          row.classList.toggle('hidden', !expanded);
        }
      });
      more.textContent = expanded ? 'Show fewer' : more.dataset.label;
    });
  }

  var buttons = Array.prototype.slice.call(document.querySelectorAll('button.f'));
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      buttons.forEach(function (b) { b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'); });
      var want = btn.dataset.filter;
      expanded = true;
      if (more) { more.textContent = 'Show fewer'; }
      rows.forEach(function (row) {
        var show = want === 'all'
          || (want === 'review' ? row.dataset.review === '1' : row.dataset.sev === want);
        row.classList.toggle('hidden', !show);
        var ev = document.getElementById('ev-' + row.dataset.idx);
        if (ev) { ev.classList.add('hidden'); row.classList.remove('open'); }
      });
    });
  });
})();
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _verdict(findings, recoverable: int, needs_human: int) -> tuple[str, str]:
    """The one sentence a person reads first."""
    if not findings:
        return (
            '<span class="amount zero">Everything reconciles.</span>',
            "Every payment traced to a settlement, and every settlement to a bank credit. "
            "Nothing needs your attention.",
        )
    claimable = sum(1 for f in findings if f.recoverable)
    head = (
        f'<span class="amount num">{_esc(rupees(recoverable))}</span>'
        f"is recoverable."
    )
    sub = (
        f"Across {claimable} finding{'s' if claimable != 1 else ''} where the money does "
        f"not add up, each with the records that prove it. "
        f"{needs_human} item{'s' if needs_human != 1 else ''} could not be resolved "
        f"automatically and {'need' if needs_human != 1 else 'needs'} a person."
    )
    return head, sub


def _actions(by_reason: dict) -> str:
    """The three things worth doing first, ranked by money.

    Ranked because it is genuinely a priority order -- the number means "do this
    one before that one", not decoration.
    """
    ranked = sorted(
        by_reason.items(), key=lambda kv: -sum(f.impact_paise for f in kv[1])
    )[:3]
    out = []
    for i, (reason, group) in enumerate(ranked, start=1):
        meta = REASON_META[reason]
        total = sum(f.impact_paise for f in group)
        money_class = "money num" if meta["recoverable"] else "money num neutral"
        out.append(
            f'<div class="action">'
            f'<div class="rank serif">{i}</div>'
            f"<h3>{_esc(meta['action'])}</h3>"
            f'<div class="{money_class}">{_esc(rupees(total))}</div>'
            f'<p class="why">{_esc(meta["title"])}.'
            f'{"" if meta["recoverable"] else " Not a loss of principal."}</p>'
            f'<p class="count num">{len(group)} item{"s" if len(group) != 1 else ""} '
            f"&middot; {_esc(reason.value.lower().replace('_', ' '))}</p>"
            f"</div>"
        )
    return "".join(out)


def _recovery_section(claims) -> str:
    """What was claimed, and what actually came back."""
    if claims is None or not claims.claims:
        return ""

    from .recovery.claims import ClaimState

    totals = claims.totals()
    by_reason = claims.recovery_by_reason()
    states = totals["by_state"]
    not_pursued = [c for c in claims.claims.values() if c.state == ClaimState.NOT_PURSUED.value]
    not_pursued_value = sum(c.claimed_paise for c in not_pursued)

    stages = [
        ("Found", totals["claimed_paise"], "claims opened"),
        ("Filed", totals["pursued_claimed_paise"], "worth the cost of asking"),
        ("Recovered", totals["recovered_paise"], "found in a later settlement"),
    ]
    widest = max((v for _l, v, _n in stages), default=1) or 1
    funnel = "".join(
        f'<div class="fstage">'
        f'<div class="fname">{_esc(label)}<br><span class="label">{_esc(note)}</span></div>'
        f'<div class="fbar"><span style="width:{100 * value / widest:.1f}%"></span></div>'
        f'<div class="fval num">{_esc(rupees(value))}</div>'
        f"</div>"
        for label, value, note in stages
    )

    rows = "".join(
        f"<tr>"
        f'<td class="reason-name">{_esc(reason.lower().replace("_", " "))}</td>'
        f'<td class="r num">{row["claims"]}</td>'
        f'<td class="r num">{_esc(rupees(row["claimed_paise"]))}</td>'
        f'<td class="r num recovered">{_esc(rupees(row["recovered_paise"]))}</td>'
        f'<td class="r num">{100 * row["rate"]:.0f}%</td>'
        f"</tr>"
        for reason, row in sorted(by_reason.items(), key=lambda kv: -kv[1]["claimed_paise"])
    )

    return f"""
  <section>
    <h2>What came back</h2>
    <p class="lede">A claim counts as recovered only when the rupees are found again in a
    later settlement &mdash; never because it was filed, and never because the gateway
    said so.</p>

    <div class="funnel">{funnel}</div>

    <div class="scroll">
      <table>
        <thead><tr>
          <th>reason</th><th class="r">claims</th><th class="r">claimed</th>
          <th class="r">recovered</th><th class="r">rate</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    <p class="note">Recovery rate per reason tells you which claims are worth making.
    {states.get("filed", 0) + states.get("partial", 0)} are still open.</p>

    <div class="callout">
      <b>{len(not_pursued)} claims worth {_esc(rupees(not_pursued_value))} were
      deliberately not pursued.</b> Chasing them would cost more in analyst time than
      they return, so they are closed with the arithmetic on the record rather than left
      to pad the queue. One claim from each declined group is still filed as a probe
      &mdash; a policy that only files what it expects to win never finds out it was
      wrong.
    </div>
  </section>
"""


def render(result, corpus, *, ledger=None, claims=None, title: str = "Baaki Statement") -> str:
    """Build the full HTML document for a completed run."""
    findings = sorted(
        result.findings, key=lambda f: (SEVERITY_ORDER[f.severity], -f.impact_paise)
    )

    recoverable = sum(f.impact_paise for f in findings if f.recoverable)
    other = sum(f.impact_paise for f in findings if not f.recoverable)
    needs_human = sum(1 for f in findings if f.requires_human)

    by_reason: dict[Reason, list] = {}
    for f in findings:
        by_reason.setdefault(f.reason, []).append(f)

    head, sub = _verdict(findings, recoverable, needs_human)
    rates = result.rates

    reason_rows = "".join(
        f"<tr>"
        f'<td><span class="sev sev-{REASON_META[reason]["severity"].value}"></span>'
        f'<span class="reason-name">{_esc(reason.value.lower().replace("_", " "))}</span></td>'
        f'<td class="r num">{len(group)}</td>'
        f'<td class="r num{" claimable" if REASON_META[reason]["recoverable"] else ""}">'
        f"{_esc(rupees(sum(f.impact_paise for f in group)))}</td>"
        f'<td class="kind">{"claimable" if REASON_META[reason]["recoverable"] else "timing / attribution"}</td>'
        f"</tr>"
        for reason, group in sorted(
            by_reason.items(), key=lambda kv: -sum(f.impact_paise for f in kv[1])
        )
    )

    hops = [
        ("Payment to settlement line", rates.payments_settled, rates.payments_total,
         rates.payment_to_settlement),
        ("Settlement to bank credit", rates.settlements_banked, rates.settlements_total,
         rates.settlement_to_bank),
        ("Bank credit attributed", rates.credits_attributed, rates.credits_total,
         rates.bank_attribution),
        ("Records in no exception", rates.records_clean, rates.records_total, rates.overall),
    ]
    hop_rows = "".join(
        f'<div class="hop">'
        f'<div>{_esc(name)} <span class="of num">{part:,} of {whole:,}</span></div>'
        f'<div class="bar"><span style="width:{100 * rate:.1f}%"></span></div>'
        f'<div class="pct num">{100 * rate:.1f}%</div>'
        f"</div>"
        for name, part, whole, rate in hops
    )

    queue = []
    for idx, f in enumerate(findings):
        meta = REASON_META[f.reason]
        evidence = "".join(f"<li>{_esc(e.render())}</li>" for e in f.evidence)
        hidden = " hidden" if idx >= QUEUE_PREVIEW else ""
        queue.append(
            f'<tr class="row{hidden}" tabindex="0" role="button" aria-expanded="false" '
            f'data-idx="{idx}" data-sev="{meta["severity"].value}" '
            f'data-review="{1 if f.requires_human else 0}">'
            f'<td><span class="disclose"></span>'
            f'<span class="sev sev-{meta["severity"].value}"></span>'
            f'<span class="num">{_esc(f.entity_id)}</span></td>'
            f'<td class="reason-name">{_esc(f.reason.value.lower().replace("_", " "))}</td>'
            f'<td class="r num{" claimable" if f.recoverable and f.impact_paise else ""}">'
            f"{_esc(rupees(f.impact_paise))}</td>"
            f'<td class="stage-tag">{_esc(f.stage.value)}</td>'
            f'<td>{"<span class=\'flag\'>needs a person</span>" if f.requires_human else ""}</td>'
            f"</tr>"
            f'<tr class="ev hidden" id="ev-{idx}"><td colspan="5">'
            f'<p class="ev-why">{_esc(f.explanation)}</p>'
            f'<div class="label">the records this came from</div>'
            f'<ul class="ev-list num">{evidence}</ul>'
            f'<p class="ev-act"><b>Do this.</b> {_esc(meta["action"])}</p>'
            f"</td></tr>"
        )

    hidden_count = max(0, len(findings) - QUEUE_PREVIEW)
    more_button = (
        f'<button class="more" id="more" data-label="Show all {len(findings)} exceptions">'
        f"Show all {len(findings)} exceptions</button>"
        if hidden_count
        else ""
    )

    tail = result.tail
    tail_note = (
        "No model was used in this run. Every figure above comes from arithmetic and "
        "exact joins."
        if tail.skipped
        else (
            f"A model was consulted {tail.calls} time{'s' if tail.calls != 1 else ''} across "
            f"{corpus.record_count():,} records "
            f"({100 * tail.calls / max(1, corpus.record_count()):.3f}%), on cases arithmetic "
            f"could not settle. Of {tail.proposals} proposal(s) it made, {tail.accepted} "
            f"passed the guardrails and {tail.rejected} were rejected. A proposed match is "
            f"accepted because the credits sum to the settlement, never because the model "
            f"was confident."
        )
    )

    fingerprint = ledger.fingerprint()[:16] if ledger else None
    fingerprint_line = (
        f'Offline decisions replay to fingerprint <span class="num">{_esc(fingerprint)}</span>; '
        f'run <span class="num">baaki verify</span> to confirm.'
        if fingerprint
        else "Run <span class=\"num\">baaki verify</span> to replay these decisions."
    )

    # The charset declaration is not optional. Opened as a local file, with no
    # HTTP header to say otherwise, a browser falls back to latin-1 and every
    # rupee sign renders as "â‚¹" and the Devanagari wordmark as mojibake. The
    # file is correct UTF-8 on disk; without this line nothing tells the browser
    # that, and the whole statement reads as garbage to the one person it was
    # written for.
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
<div class="sheet">

  <header class="masthead">
    <h1 class="mark">बाकी <span>&mdash; the remainder</span></h1>
    <div class="meta">
      <div><span class="label">records</span>
<span class="v num">{corpus.record_count():,}</span></div>
      <div><span class="label">reconciled in</span>
<span class="v num">{result.elapsed_total:.2f}s</span></div>
      <div><span class="label">exceptions</span>
<span class="v num">{len(findings)}</span></div>
    </div>
  </header>

  <div class="verdict">
    <h2 class="headline">{head}</h2>
    <p class="subhead">{sub}</p>
  </div>

  <div class="split">
    <div class="claim">
      <span class="label">you can claim this back</span>
      <span class="fig num">{_esc(rupees(recoverable))}</span>
      <p>Fees billed above the contracted rate, tax computed wrongly, deductions taken
      twice, payments that never settled, settlements that never reached the bank.</p>
    </div>
    <div>
      <span class="label">timing and attribution</span>
      <span class="fig num">{_esc(rupees(other))}</span>
      <p>Not a loss of principal. Money held by the gateway, settlements that arrived
      late, and credits that need identifying before they are booked as revenue. Kept
      separate so the figure on the left stays honest.</p>
    </div>
  </div>

  <section>
    <h2>Do this first</h2>
    <p class="lede">Ordered by how much money is behind each one, not by how many rows.</p>
    <div class="actions">{_actions(by_reason)}</div>
  </section>
{_recovery_section(claims)}
  <section>
    <h2>Everything, by reason</h2>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>reason</th><th class="r">items</th><th class="r">amount</th><th>kind</th>
        </tr></thead>
        <tbody>{reason_rows}</tbody>
      </table>
    </div>
    <p class="note">Fee overcharges are numerous and individually small; one settlement
    that never reached the bank is a single row worth six figures. Sorting by money
    rather than by count is the difference between a useful queue and a long one.</p>
  </section>

  <section>
    <h2>How much matched</h2>
    <p class="lede">Broken out per hop of the chain. A single blended figure would hide
    which join is failing, and the failing join is the diagnosis.</p>
    <div class="hops">{hop_rows}</div>
    <p class="note">Notice how far these diverge. Nearly every payment reaches a
    settlement, while only a minority of bank credits attribute to a gateway settlement
    &mdash; because most of the rest are genuinely somebody else's money, and saying so
    is the finding.</p>
  </section>

  <section>
    <h2>Every exception</h2>
    <p class="lede">Most serious first. Open any row to see the records behind it &mdash;
    enough to check the arithmetic by hand, or to attach to a ticket.</p>
    <div class="filters">
      <span class="label" style="margin-right:6px">Show</span>
      <button class="f" data-filter="all" aria-pressed="true">Everything</button>
      <button class="f" data-filter="critical" aria-pressed="false">Critical</button>
      <button class="f" data-filter="high" aria-pressed="false">High</button>
      <button class="f" data-filter="review" aria-pressed="false">Needs a person</button>
    </div>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>record</th><th>reason</th><th class="r">amount</th><th>decided by</th><th></th>
        </tr></thead>
        <tbody>{"".join(queue)}</tbody>
      </table>
    </div>
    {more_button}
  </section>

  <footer>
    <div>{tail_note}</div>
    <div>{fingerprint_line}</div>
    <div>Generated {datetime.now(UTC):%d %B %Y, %H:%M} UTC by baaki.</div>
  </footer>

</div>
<script>{_JS}</script>
"""


def write(result, corpus, out: Path, *, ledger=None, claims=None,
          title: str = "Baaki Statement") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render(result, corpus, ledger=ledger, claims=claims, title=title),
        encoding="utf-8",
    )
    return out
