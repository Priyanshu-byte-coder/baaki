"""Command line interface.

    baaki generate   write a month of books to disk as CSVs
    baaki run        reconcile a corpus and print the exception queue
    baaki eval       score against the answer key across seeds
    baaki verify     replay a ledger and confirm the fingerprint still matches
    baaki doctor     check what is configured and what will be skipped
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __name__ as _pkg  # noqa: F401
from .audit.ledger import Ledger, corpus_fingerprint
from .corpus import io
from .corpus.defects import DEFAULT_PLAN, HARD_PLAN, inject
from .corpus.generate import generate
from .evaluation.score import score
from .match import pipeline
from .models import REASON_META, Reason
from .money import rupees

# The rupee sign is not in cp1252, which is what a Windows console defaults to.
# Forcing UTF-8 keeps a rupee report a rupee report rather than degrading the
# output to "Rs." because of a terminal setting.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(add_completion=False, help="Settlement reconciliation and audit engine.")
console = Console()

PLANS = {"default": DEFAULT_PLAN, "hard": HARD_PLAN, "clean": {}}


def _load_env() -> None:
    """Read .env from the project root if present. No dependency on python-dotenv."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
        return


def _client(enabled: bool):
    if not enabled:
        return None
    _load_env()
    from .llm.client import LLMClient

    client = LLMClient()
    if not client.available:
        console.print("[yellow]no API keys found; tail stage will be skipped[/yellow]")
        return None
    return client


@app.command()
def generate_(
    seed: int = typer.Option(7, "--seed"),
    orders: int = typer.Option(4_000, "--orders"),
    plan: str = typer.Option("hard", "--plan", help="clean, default or hard"),
    out: Path = typer.Option(Path("data/generated/dev"), "--out"),
) -> None:
    """Write a month of books to disk, with the answer key alongside."""
    if plan not in PLANS:
        raise typer.BadParameter(f"plan must be one of {sorted(PLANS)}")

    g = generate(seed=seed, n_orders=orders)
    if PLANS[plan]:
        g = inject(g, seed=seed, plan=PLANS[plan])
    io.save(g.corpus, out, truth=g.truth)

    console.print(
        f"wrote [bold]{g.corpus.record_count():,}[/bold] records to [cyan]{out}[/cyan] "
        f"(seed {seed}, plan {plan}, {len(g.truth)} planted finding(s))"
    )


app.command(name="generate")(generate_)


@app.command()
def run(
    corpus_dir: Path = typer.Option(..., "--corpus", help="directory written by generate"),
    tail: bool = typer.Option(False, "--tail/--no-tail", help="enable the model stage"),
    ledger_out: Path = typer.Option(None, "--ledger", help="write the decision log here"),
    report_out: Path = typer.Option(None, "--report", help="write a self-contained HTML statement"),
    limit: int = typer.Option(15, "--limit", help="rows of the exception queue to print"),
) -> None:
    """Reconcile a corpus and print what does not add up."""
    corpus = io.load(corpus_dir)
    result = pipeline.run(corpus, client=_client(tail))

    recoverable = sum(f.impact_paise for f in result.findings if f.recoverable)
    other = sum(f.impact_paise for f in result.findings if not f.recoverable)
    escalated = sum(1 for f in result.findings if f.requires_human)

    console.print()
    console.print(f"[bold]{corpus.record_count():,}[/bold] records reconciled in "
                  f"[bold]{result.elapsed_total:.2f}s[/bold]")
    console.print(f"[bold red]{rupees(recoverable)}[/bold red] recoverable across "
                  f"{sum(1 for f in result.findings if f.recoverable)} finding(s)")
    console.print(f"{rupees(other)} in timing and unattributed items "
                  f"(not a loss of principal)")
    console.print(f"{len(result.findings)} exception(s), {escalated} need a person "
                  f"({100 * result.coverage():.1f}% resolved automatically)")
    if result.tail.skipped:
        console.print("[dim]tail stage skipped; residue left escalated[/dim]")
    else:
        console.print(
            f"[dim]tail: {result.tail.calls} call(s), {result.tail.accepted} accepted, "
            f"{result.tail.rejected} rejected by guardrails[/dim]"
        )

    by_reason: dict[Reason, list] = {}
    for f in result.findings:
        by_reason.setdefault(f.reason, []).append(f)

    table = Table(title="\nexception queue", title_justify="left", header_style="bold")
    table.add_column("reason")
    table.add_column("n", justify="right")
    table.add_column("impact", justify="right")
    table.add_column("severity")
    table.add_column("action", overflow="fold")
    for reason, group in sorted(
        by_reason.items(), key=lambda kv: -sum(f.impact_paise for f in kv[1])
    ):
        total = sum(f.impact_paise for f in group)
        table.add_row(
            reason.value,
            str(len(group)),
            rupees(total),
            REASON_META[reason]["severity"].value,
            REASON_META[reason]["action"],
        )
    console.print(table)

    worst = sorted(result.findings, key=lambda f: -f.impact_paise)[:limit]
    detail = Table(title="\nlargest individual findings", title_justify="left",
                   header_style="bold")
    detail.add_column("entity", no_wrap=True)
    detail.add_column("reason", no_wrap=True)
    detail.add_column("impact", justify="right", no_wrap=True)
    detail.add_column("stage", no_wrap=True)
    detail.add_column("why", overflow="ellipsis", max_width=72, no_wrap=True)
    for f in worst:
        detail.add_row(f.entity_id, f.reason.value, rupees(f.impact_paise),
                       f.stage.value, " ".join(f.explanation.split()))
    console.print(detail)

    ledger = Ledger(run_id=f"run_{os.getpid()}", corpus_sha=corpus_fingerprint(corpus))
    ledger.extend(result.findings)
    console.print(f"\noffline fingerprint [cyan]{ledger.fingerprint()[:16]}[/cyan]")
    if ledger_out:
        ledger.write(ledger_out)
        console.print(f"decision log written to [cyan]{ledger_out}[/cyan]")
    if report_out:
        from . import report as report_module

        report_module.write(result, corpus, report_out, ledger=ledger)
        console.print(f"statement written to [cyan]{report_out}[/cyan]")


@app.command()
def eval_(
    seeds: str = typer.Option("7,13,21,34,55,89,101", "--seeds"),
    orders: int = typer.Option(4_000, "--orders"),
    plan: str = typer.Option("hard", "--plan"),
    tail: bool = typer.Option(False, "--tail/--no-tail"),
) -> None:
    """Score the engine against the answer key across seeds."""
    client = _client(tail)
    table = Table(header_style="bold")
    for column in ("seed", "records", "planted", "recall", "precision", "money", "coverage", "time"):
        table.add_column(column, justify="right" if column != "seed" else "left")

    for seed in [int(s) for s in seeds.split(",") if s.strip()]:
        g = inject(generate(seed=seed, n_orders=orders), seed=seed, plan=PLANS[plan])
        result = pipeline.run(g.corpus, client=client)
        s = score(result.findings, g.truth)
        planted, found = s.money(recoverable_only=True)
        pct = 100 * found / planted if planted else float("nan")
        table.add_row(
            str(seed),
            f"{g.corpus.record_count():,}",
            str(s.planted),
            f"{100 * s.recall:.1f}%",
            f"{100 * s.precision:.1f}%",
            f"{pct:.1f}%",
            f"{100 * result.coverage():.1f}%",
            f"{result.elapsed_total:.2f}s",
        )
    console.print(table)
    console.print(
        "[dim]recall and precision are reported separately and never averaged. "
        "A missed defect costs an analyst time; a false auto-match writes off "
        "real money.[/dim]"
    )


app.command(name="eval")(eval_)


@app.command()
def verify(
    ledger_path: Path = typer.Option(..., "--ledger"),
    corpus_dir: Path = typer.Option(..., "--corpus"),
) -> None:
    """Re-run the offline stages and confirm the ledger still reproduces."""
    header, _decisions = Ledger.read(ledger_path)
    corpus = io.load(corpus_dir)

    result = pipeline.run_offline(corpus)
    replay = Ledger(run_id="replay", corpus_sha=corpus_fingerprint(corpus))
    replay.extend(result.findings)

    same_corpus = header["corpus_sha"] == replay.corpus_sha
    same_decisions = header["offline_fingerprint"] == replay.fingerprint()

    console.print(f"corpus     {'match' if same_corpus else 'DIFFERENT'}  "
                  f"{header['corpus_sha'][:16]} vs {replay.corpus_sha[:16]}")
    console.print(f"decisions  {'match' if same_decisions else 'DIFFERENT'}  "
                  f"{header['offline_fingerprint'][:16]} vs {replay.fingerprint()[:16]}")
    if header.get("tail_fingerprint", "none") != "none":
        console.print(
            "[dim]tail decisions are excluded from the fingerprint: a model call is "
            "not bit-reproducible, so they are logged with model and prompt hash "
            "instead of claimed as replayable.[/dim]"
        )
    if not (same_corpus and same_decisions):
        raise typer.Exit(code=1)
    console.print("[green]offline decisions reproduce exactly[/green]")


@app.command()
def doctor() -> None:
    """Report what is configured and what will therefore be skipped."""
    _load_env()
    from .llm.client import LLMClient

    client = LLMClient()
    console.print(f"offline stages   [green]ready[/green] (no configuration needed)")
    if client.available:
        console.print(f"tail stage       [green]ready[/green] — "
                      f"{len(client.pool.keys)} key(s), model {client.model}, "
                      f"{client.tpm_limit} TPM per key")
    else:
        console.print("tail stage       [yellow]unavailable[/yellow] — no keys in "
                      "BAAKI_GROQ_API_KEYS; residue will be left escalated")
    console.print(f"reason codes     {len(list(Reason))} in the taxonomy, "
                  f"{sum(1 for m in REASON_META.values() if m['recoverable'])} recoverable")


if __name__ == "__main__":
    app()
