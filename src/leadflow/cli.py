"""Command line: `leadflow run`, `leadflow history`, `leadflow cleanup`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anthropic
import typer

from leadflow.agent import Agent, RunResult
from leadflow.config import load_settings
from leadflow.errors import ConfigError, HubSpotError
from leadflow.hubspot import HubSpotClient
from leadflow.ledger import Ledger
from leadflow.logging import configure_logging
from leadflow.tools import ToolBox

app = typer.Typer(add_completion=False, help="Turn raw sales enquiries into clean HubSpot records.")


@app.command()
def run(
    lead: str = typer.Argument(None, help="The enquiry text. Omit to read from --file or stdin."),
    file: Path | None = typer.Option(None, "--file", "-f", help="Read the enquiry from a file."),
) -> None:
    """Process one enquiry into HubSpot company, contact and deal records."""
    configure_logging()
    text = _read_lead(lead, file)
    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc

    hubspot = HubSpotClient.from_key(
        settings.hubspot_service_key.get_secret_value(), settings.hubspot_base_url
    )
    ledger = Ledger(settings.ledger_path)
    headers = (
        {"anthropic-workspace-id": settings.anthropic_workspace_id}
        if settings.anthropic_workspace_id
        else None
    )
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), default_headers=headers
    )
    agent = Agent(
        client.beta.messages.create,
        lambda run_id: ToolBox(hubspot, ledger, run_id),
        ledger,
        model=settings.model,
        max_steps=settings.max_agent_steps,
    )
    try:
        result = agent.run(text)
    except anthropic.APIStatusError as exc:
        typer.echo(f"model API error ({exc.status_code}): {exc.message}", err=True)
        raise typer.Exit(1) from exc
    except anthropic.APIConnectionError as exc:
        typer.echo("could not reach the model API; check the network and retry.", err=True)
        raise typer.Exit(1) from exc
    _print_result(result)
    ledger.close()
    if result.outcome != "completed":
        raise typer.Exit(1)


@app.command()
def history(limit: int = typer.Option(10, help="How many runs to show.")) -> None:
    """Show recent runs with outcome, steps and cost."""
    settings = load_settings()
    ledger = Ledger(settings.ledger_path)
    for run_id, started, outcome, steps, cost in ledger.recent_runs(limit):
        typer.echo(
            f"{started[:19]}  {run_id}  {outcome or 'running':<10}  {steps or 0:>2} steps  ${cost or 0:.4f}"
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
    hubspot = HubSpotClient.from_key(
        settings.hubspot_service_key.get_secret_value(), settings.hubspot_base_url
    )
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
