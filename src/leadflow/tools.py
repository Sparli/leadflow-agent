"""The agent's tools: the only way the model can affect the CRM.

Each tool is a pydantic input model plus a handler. The model's arguments are
validated against the input model *before* the handler runs, so a bad argument
(a negative deal amount, an email without an @, a field the tool doesn't have)
never reaches HubSpot. The validation error goes back to the model as an error
result, and the model corrects itself on the next step.

Prompt instructions are requests; this layer is enforcement.

The find tools read HubSpot directly; reads change nothing. The create tools
never write: they record a proposal and return a placeholder id. A human
approves the plan and `approval.Approver` performs the writes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from leadflow.errors import HubSpotError
from leadflow.hubspot import HubSpotClient
from leadflow.ledger import Ledger, Proposal

log = structlog.get_logger(__name__)

# Every record the agent creates carries this marker, so a person browsing
# HubSpot can see at a glance which records came from the demo agent.
DEMO_MARKER = "[leadflow demo]"

CREATE_TOOLS = frozenset({"create_company", "create_contact", "create_deal"})

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^(?!-)[a-z0-9-]+(\.[a-z0-9-]+)+$")


class _ToolInput(BaseModel):
    # extra="forbid": an argument the tool does not define is an error, not
    # something silently dropped. Models occasionally invent plausible fields.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _clean_domain(value: str) -> str:
    domain = value.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    domain = domain.split("/")[0]
    if not _DOMAIN.match(domain):
        raise ValueError(f"not a valid domain: {value!r}")
    return domain


class FindCompany(_ToolInput):
    domain: str = Field(description="Company website domain, e.g. mueller-baeckerei.de")

    _normalise = field_validator("domain")(_clean_domain)


class FindContact(_ToolInput):
    email: str = Field(description="Exact email address")

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        if not _EMAIL.match(value):
            raise ValueError(f"not a valid email address: {value!r}")
        return value.lower()


class CreateCompany(_ToolInput):
    name: str = Field(min_length=1, max_length=200)
    domain: str | None = Field(default=None, description="Website domain if known")
    city: str | None = None

    @field_validator("domain")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        return None if value is None else _clean_domain(value)


class CreateContact(_ToolInput):
    firstname: str = Field(min_length=1, max_length=100)
    lastname: str | None = None
    email: str | None = None
    phone: str | None = None
    company_id: str | None = Field(default=None, description="HubSpot id of the contact's company")

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL.match(value):
            raise ValueError(f"not a valid email address: {value!r}")
        return None if value is None else value.lower()


class CreateDeal(_ToolInput):
    dealname: str = Field(min_length=1, max_length=200)
    company_id: str = Field(description="HubSpot id of the company this deal belongs to")
    contact_id: str | None = None
    amount_eur: float | None = Field(
        default=None,
        gt=0,
        le=10_000_000,
        description="Estimated value in EUR, only if stated or clearly implied",
    )
    notes: str | None = Field(
        default=None, max_length=2000, description="Key facts: roof size, timeline, needs"
    )


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    input_model: type[_ToolInput]
    handler: Callable[[Any], dict[str, Any]]


class ToolBox:
    """Owns the tools for one run. Bound to a run_id so every proposal is attributable."""

    def __init__(self, hubspot: HubSpotClient, ledger: Ledger, run_id: str) -> None:
        self._hubspot = hubspot
        self._ledger = ledger
        self._run_id = run_id
        # Ids this run has legitimately seen, per object type: found by a search or
        # returned as a placeholder by a proposal.
        self._known: dict[str, set[str]] = {"companies": set(), "contacts": set()}
        self._tools = {
            t.name: t
            for t in (
                _Tool(
                    "find_company",
                    "Look up an existing company by website domain. Always do this before creating a company.",
                    FindCompany,
                    self._find_company,
                ),
                _Tool(
                    "find_contact",
                    "Look up an existing contact by email. Always do this before creating a contact.",
                    FindContact,
                    self._find_contact,
                ),
                _Tool(
                    "create_company",
                    "Propose creating a company. Only if find_company found nothing.",
                    CreateCompany,
                    self._create_company,
                ),
                _Tool(
                    "create_contact",
                    "Propose creating a contact, linked to its company. Only if find_contact found nothing.",
                    CreateContact,
                    self._create_contact,
                ),
                _Tool(
                    "create_deal",
                    "Propose creating a sales deal for a company, linked to the contact.",
                    CreateDeal,
                    self._create_deal,
                ),
            )
        }

    def definitions(self) -> list[dict[str, Any]]:
        """Tool definitions in the shape the Messages API expects, generated from the input models."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_model.model_json_schema(),
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, raw_input: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"unknown tool {name!r}", is_error=True)
        try:
            args = tool.input_model.model_validate(raw_input)
        except ValidationError as exc:
            # Readable field-level errors, so the model knows exactly what to fix.
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            log.info("tool.invalid_input", tool=name, problems=problems)
            return ToolResult(f"invalid input, nothing was written: {problems}", is_error=True)
        try:
            output = tool.handler(args)
        except UnknownReferenceError as exc:
            return ToolResult(f"invalid input, nothing was written: {exc}", is_error=True)
        except HubSpotError as exc:
            log.warning("tool.hubspot_error", tool=name, status=exc.status_code)
            return ToolResult(f"HubSpot error ({exc.status_code}): {exc}", is_error=True)
        return ToolResult(json.dumps(output, sort_keys=True))

    # --- handlers --------------------------------------------------------

    def _find_company(self, args: FindCompany) -> dict[str, Any]:
        found = self._hubspot.search("companies", "domain", args.domain)
        self._known["companies"].update(r["id"] for r in found)
        return {"matches": [{"id": r["id"], **r.get("properties", {})} for r in found]}

    def _find_contact(self, args: FindContact) -> dict[str, Any]:
        found = self._hubspot.search("contacts", "email", args.email)
        self._known["contacts"].update(r["id"] for r in found)
        return {"matches": [{"id": r["id"], **r.get("properties", {})} for r in found]}

    def _create_company(self, args: CreateCompany) -> dict[str, Any]:
        proposal = self._propose("create_company", args)
        self._known["companies"].add(proposal.placeholder)
        return {"company_id": proposal.placeholder, "status": _PROPOSED}

    def _create_contact(self, args: CreateContact) -> dict[str, Any]:
        self._require_known("companies", args.company_id)
        proposal = self._propose("create_contact", args)
        self._known["contacts"].add(proposal.placeholder)
        return {"contact_id": proposal.placeholder, "status": _PROPOSED}

    def _create_deal(self, args: CreateDeal) -> dict[str, Any]:
        self._require_known("companies", args.company_id)
        self._require_known("contacts", args.contact_id)
        if args.contact_id is None and self._known["contacts"]:
            # Seen live: a model proposing contact and deal in one parallel step has no
            # contact id yet, and the deal ends up linked to nobody.
            raise UnknownReferenceError(
                "this run has a contact "
                f"({', '.join(sorted(self._known['contacts']))}); pass it as contact_id"
            )
        proposal = self._propose("create_deal", args)
        return {"deal_id": proposal.placeholder, "status": _PROPOSED}

    def _propose(self, tool: str, args: _ToolInput) -> Proposal:
        proposal = self._ledger.add_proposal(self._run_id, tool, args.model_dump(exclude_none=True))
        log.info("tool.proposed", tool=tool, proposal_id=proposal.id)
        return proposal

    def _require_known(self, object_type: str, object_id: str | None) -> None:
        # Links may only point at records this run found or proposed. Without this,
        # an id copied from the enquiry text could attach a deal to someone else's company.
        if object_id is not None and object_id not in self._known[object_type]:
            raise UnknownReferenceError(
                f"{object_type} id {object_id!r} was not returned by a tool in this run; "
                f"use an id from find_* or create_* results"
            )


_PROPOSED = "proposed; written to HubSpot only after a human approves. Use this id in later calls."


class UnknownReferenceError(ValueError):
    """The model referenced a record id it never received from a tool."""
