"""Approval: the only code path that writes to HubSpot.

The agent's run ends with a plan of proposals in the ledger. A human reviews it,
and `Approver.approve` executes it in order:

- Placeholder ids ("pending-12") are swapped for the real HubSpot ids created
  earlier in the same plan.
- Companies and contacts are checked for duplicates again, because time has
  passed since the agent looked. If one now exists, it is reused, not duplicated.
- The first failure stops the plan: later steps usually depend on earlier ones.
  Approving again resumes where it stopped and never creates a record twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from leadflow.errors import HubSpotError
from leadflow.hubspot import HubSpotClient
from leadflow.ledger import PLACEHOLDER_PREFIX, Ledger, Proposal
from leadflow.tools import DEMO_MARKER

log = structlog.get_logger(__name__)

_OBJECT_TYPE = {"create_company": "companies", "create_contact": "contacts", "create_deal": "deals"}
_DUPLICATE_KEY = {"create_company": "domain", "create_contact": "email"}


@dataclass(frozen=True)
class Outcome:
    proposal_id: int
    tool: str
    status: str  # executed | reused | failed
    object_id: str | None = None
    detail: str = ""


class Approver:
    def __init__(self, hubspot: HubSpotClient, ledger: Ledger) -> None:
        self._hubspot = hubspot
        self._ledger = ledger

    def approve(self, run_id: str) -> list[Outcome]:
        proposals = self._ledger.proposals(run_id)
        real_ids = {
            p.placeholder: p.object_id for p in proposals if p.status in ("executed", "reused")
        }
        outcomes: list[Outcome] = []
        for proposal in proposals:
            if proposal.status not in ("pending", "failed"):
                continue
            outcome = self._execute(proposal, real_ids)
            outcomes.append(outcome)
            if outcome.status == "failed":
                break
            real_ids[proposal.placeholder] = outcome.object_id
        return outcomes

    def reject(self, run_id: str) -> int:
        """Reject everything still undecided. Records already written are left alone."""
        rejected = 0
        for proposal in self._ledger.proposals(run_id):
            if proposal.status in ("pending", "failed") and proposal.object_id is None:
                self._ledger.decide(proposal.id, "rejected")
                rejected += 1
        return rejected

    def _execute(self, proposal: Proposal, real_ids: dict[str, str | None]) -> Outcome:
        try:
            args = _resolve(proposal.args, real_ids)
        except _UnresolvedReferenceError as exc:
            return Outcome(proposal.id, proposal.tool, "failed", detail=str(exc))
        try:
            existing = self._find_duplicate(proposal.tool, args)
            if existing is not None:
                self._ledger.decide(proposal.id, "reused", object_id=existing)
                log.info("approval.reused", tool=proposal.tool, object_id=existing)
                return Outcome(proposal.id, proposal.tool, "reused", existing, "already existed")
            object_id = self._write(proposal, args)
        except HubSpotError as exc:
            self._ledger.decide(proposal.id, "failed", object_id=proposal.object_id, error=str(exc))
            log.warning("approval.failed", tool=proposal.tool, status=exc.status_code)
            return Outcome(proposal.id, proposal.tool, "failed", proposal.object_id, str(exc))
        self._ledger.decide(proposal.id, "executed", object_id=object_id)
        return Outcome(proposal.id, proposal.tool, "executed", object_id)

    def _find_duplicate(self, tool: str, args: dict[str, Any]) -> str | None:
        key = _DUPLICATE_KEY.get(tool)
        if key is None or key not in args:
            return None
        found = self._hubspot.search(_OBJECT_TYPE[tool], key, args[key])  # type: ignore[arg-type]
        return str(found[0]["id"]) if found else None

    def _write(self, proposal: Proposal, args: dict[str, Any]) -> str:
        object_type = _OBJECT_TYPE[proposal.tool]
        object_id = proposal.object_id
        if object_id is None:
            object_id = self._hubspot.create(object_type, _properties(proposal.tool, args))  # type: ignore[arg-type]
            self._ledger.record_write(proposal.run_id, object_type, object_id, "create", args)
            # Saved before the links are made: if linking fails, a retry links this
            # record instead of creating a second one.
            self._ledger.decide(proposal.id, "pending", object_id=object_id)
        for to_type, key in (("companies", "company_id"), ("contacts", "contact_id")):
            if args.get(key):
                # HubSpot's default association is idempotent, so a retry is safe.
                self._hubspot.associate(object_type, object_id, to_type, args[key])  # type: ignore[arg-type]
                self._ledger.record_write(
                    proposal.run_id,
                    object_type,
                    object_id,
                    "associate",
                    {"to": to_type, "to_id": args[key]},
                )
        log.info("approval.created", object_type=object_type, object_id=object_id)
        return object_id


class _UnresolvedReferenceError(Exception):
    pass


def _resolve(args: dict[str, Any], real_ids: dict[str, str | None]) -> dict[str, Any]:
    resolved = dict(args)
    for key in ("company_id", "contact_id"):
        value = resolved.get(key)
        if isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX):
            real = real_ids.get(value)
            if real is None:
                raise _UnresolvedReferenceError(
                    f"{key} {value} was not created, so this step cannot run"
                )
            resolved[key] = real
    return resolved


def _properties(tool: str, args: dict[str, Any]) -> dict[str, str]:
    """Map validated tool arguments to HubSpot properties. Links are not properties."""
    if tool == "create_company":
        return _props(
            name=args["name"],
            domain=args.get("domain"),
            city=args.get("city"),
            description=DEMO_MARKER,
        )
    if tool == "create_contact":
        return _props(
            firstname=args["firstname"],
            lastname=args.get("lastname"),
            email=args.get("email"),
            phone=args.get("phone"),
        )
    notes = args.get("notes")
    amount = args.get("amount_eur")
    return _props(
        dealname=args["dealname"],
        amount=None if amount is None else f"{amount:.2f}",
        pipeline="default",
        dealstage="appointmentscheduled",
        description=f"{DEMO_MARKER} {notes}" if notes else DEMO_MARKER,
    )


def _props(**values: str | None) -> dict[str, str]:
    """Drop unset fields: sending an empty string would overwrite real data on updates."""
    return {k: v for k, v in values.items() if v is not None}
