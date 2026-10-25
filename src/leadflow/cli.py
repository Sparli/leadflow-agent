"""Command line: run, approve, reject, history, cleanup.

`run` never writes to HubSpot on its own. It shows the agent's plan and asks;
`--yes` approves without asking, for scripted use.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anthropic
import typer

from leadflow.agent import Agent, RunResult
from leadflow.approval import Approver, Outcome
from leadflow.config import Settings, load_settings
from leadflow.errors import ConfigError, HubSpotError, LeadRejectedError
from leadflow.hubspot import HubSpotClient
from leadflow.ledger import Ledger, Proposal
from leadflow.logging import configure_logging
from leadflow.tools import ToolBox

app = typer.Typer(add_completion=False, help="Turn raw sales enquiries into clean HubSpot records.")


@app.command()
def run(
    lead: str = typer.Argument(None, help="The enquiry text. Omit to read from --file or stdin."),
    file: Path | None = typer.Option(None, "--file", "-f", help="Read the enquiry from a file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve the plan without asking."),
) -> None:
    """Turn one enquiry into a plan of HubSpot records, then ask before writing it."""
    configure_logging()
    text = _read_lead(lead, file)
    settings = _settings()
    hubspot = _hubspot(settings)
    ledger = Ledger(settings.ledger_path)
    headers = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id}
        if settings.anthropic_workspace_id
        else None
    )
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        default_headers=headers,
        # The SDK default waits up to 10 minutes per request. One step of this
        # agent takes seconds; a minute means something is wrong. The SDK retries
        # 429/5xx itself, with backoff.
        timeout=60.0,
        max_retries=2,
    )
    agent = Agent(
        client.beta.messages.create,
        lambda run_id: ToolBox(hubspot, ledger, run_id),
        ledger,
        model=settings.model,
        max_steps=settings.max_agent_steps,
        max_lead_chars=settings.max_lead_chars,
    )
    try:
        result = agent.run(text)
    except LeadRejectedError as exc:
        typer.echo(f"enquiry rejected: {exc}", err=True)
        raise typer.Exit(2) from exc
    except anthropic.APIStatusError as exc:
        typer.echo(f"model API error ({exc.status_code}): {exc.message}", err=True)
        raise typer.Exit(1) from exc
    except anthropic.APIConnectionError as exc:
        typer.echo("could not reach the model API; check the network and retry.", err=True)
        raise typer.Exit(1) from exc
    _print_result(result)
    if result.outcome != "completed":
        ledger.close()
        raise typer.Exit(1)
    if ledger.proposals(result.run_id):
        _decide(Approver(hubspot, ledger), ledger, result.run_id, ask=not yes)
    ledger.close()


@app.command()
def approve(
    run_id: str = typer.Argument(..., help="The run whose plan to write to HubSpot."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Write a run's proposed records to HubSpot. Safe to repeat after a failure."""
    configure_logging()
    settings = _settings()
    ledger = Ledger(settings.ledger_path)
    _decide(Approver(_hubspot(settings), ledger), ledger, run_id, ask=not yes)
    ledger.close()


@app.command()
def reject(run_id: str = typer.Argument(..., help="The run whose plan to discard.")) -> None:
    """Discard a run's undecided proposals. Nothing is written."""
    settings = _settings()
    ledger = Ledger(settings.ledger_path)
    count = Approver(_hubspot(settings), ledger).reject(run_id)
    typer.echo(f"Rejected {count} proposals. Nothing was written to HubSpot.")
    ledger.close()


def _decide(approver: Approver, ledger: Ledger, run_id: str, *, ask: bool) -> None:
    open_items = [p for p in ledger.proposals(run_id) if p.status in ("pending", "failed")]
    if not open_items:
        typer.echo(f"Run {run_id} has nothing waiting for approval.")
        return
    typer.echo("\nPlan (nothing has been written yet):")
    for p in open_items:
        typer.echo(f"  {p.placeholder:<12} {_describe(p)}")
    if ask:
        if not sys.stdin.isatty():
            typer.echo(
                f"\nApprove with: leadflow approve {run_id}   Discard with: leadflow reject {run_id}"
            )
            return
        if not typer.confirm(f"\nWrite these {len(open_items)} records to HubSpot?", default=False):
            typer.echo(
                f"Left pending. Later: leadflow approve {run_id}  or  leadflow reject {run_id}"
            )
            return
    outcomes = approver.approve(run_id)
    typer.echo("")
    for o in outcomes:
        typer.echo(_format_outcome(o))
    if any(o.status == "failed" for o in outcomes):
        typer.echo(
            f"\nStopped at the first failure. Fix the cause, then: leadflow approve {run_id}",
            err=True,
        )
        raise typer.Exit(1)


@app.command()
def history(limit: int = typer.Option(10, help="How many runs to show.")) -> None:
    """Show recent runs with outcome, steps and cost."""
    settings = load_settings()
    ledger = Ledger(settings.ledger_path)
    waiting = ledger.pending_counts()
    for run_id, started, outcome, steps, cost in ledger.recent_runs(limit):
        note = f"  {waiting[run_id]} awaiting approval" if run_id in waiting else ""
        typer.echo(
            f"{started[:19]}  {run_id}  {outcome or 'running':<10}  {steps or 0:>2} steps  ${cost or 0:.4f}{note}"
        )
    ledger.close()


@app.command()
def cleanup(
    run_id: str | None = typer.Option(
        None, help="Only this run. Default: every record the agent created."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Archive records the agent created. Never touches records it did not create."""
    configure_logging()
    settings = load_settings()
    ledger = Ledger(settings.ledger_path)
    records = ledger.created_records(run_id)
    if not records:
        typer.echo("Nothing to clean up.")
        return
    typer.echo(
        f"{len(records)} records created by the agent will be archived (recoverable in HubSpot for 90 days)."
    )
    if not yes:
        typer.confirm("Continue?", abort=True)
    hubspot = _hubspot(settings)
    for rec in records:
        try:
            hubspot.archive(rec.object_type, rec.object_id)  # type: ignore[arg-type]
        except HubSpotError as exc:
            if exc.status_code != 404:  # already gone is fine
                typer.echo(f"  failed {rec.object_type}/{rec.object_id}: {exc}", err=True)
                continue
        ledger.mark_archived(rec.object_type, rec.object_id)
        typer.echo(f"  archived {rec.object_type}/{rec.object_id}")
    ledger.close()


def _settings() -> Settings:
    try:
        return load_settings()
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _hubspot(settings: Settings) -> HubSpotClient:
    return HubSpotClient.from_key(
        settings.hubspot_service_key.get_secret_value(), settings.hubspot_base_url
    )


def _describe(p: Proposal) -> str:
    a = p.args
    if p.tool == "create_company":
        return f"company  {a['name']}" + (f" ({a['domain']})" if "domain" in a else "")
    if p.tool == "create_contact":
        name = " ".join(x for x in (a["firstname"], a.get("lastname")) if x)
        return (
            f"contact  {name}"
            + (f" <{a['email']}>" if "email" in a else "")
            + (f" → company {a['company_id']}" if "company_id" in a else "")
        )
    amount = f"  €{a['amount_eur']:,.0f}" if "amount_eur" in a else ""
    contact = f", contact {a['contact_id']}" if "contact_id" in a else ""
    return f"deal     {a['dealname']}{amount} → company {a['company_id']}{contact}"


def _format_outcome(o: Outcome) -> str:
    mark = {"executed": "✓ created", "reused": "↺ reused ", "failed": "✗ failed "}[o.status]
    target = f"{o.object_id}" if o.object_id else ""
    detail = f"  ({o.detail})" if o.detail else ""
    return f"  {mark} {o.tool.removeprefix('create_'):<8} {target}{detail}"


def _read_lead(lead: str | None, file: Path | None) -> str:
    if lead:
        return lead
    if file:
        return file.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    typer.echo("Provide the enquiry as an argument, with --file, or on stdin.", err=True)
    raise typer.Exit(2)


def _print_result(result: RunResult) -> None:
    typer.echo("")
    for i, step in enumerate(result.steps, 1):
        mark = "✗" if step.is_error else "✓"
        typer.echo(f"  {i}. {mark} {step.tool}({_short(step.input)})")
        typer.echo(f"       → {step.result[:160]}")
    typer.echo("")
    typer.echo(result.summary)
    typer.echo("")
    typer.echo(
        f"run {result.run_id} · {result.outcome} · {len(result.steps)} tool calls · "
        f"{result.usage.input_tokens + result.usage.output_tokens} tokens · "
        f"${result.usage.cost_usd:.4f} · {result.seconds}s"
    )


def _short(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items())[:120]
