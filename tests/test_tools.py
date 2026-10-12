"""Tool input validation: bad arguments from the model never reach HubSpot."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from leadflow.errors import HubSpotError
from leadflow.ledger import Ledger
from leadflow.tools import DEMO_MARKER, ToolBox


class FakeHubSpot:
    """Records calls instead of making them. Any call is visible in `calls`."""

    def __init__(self, search_results: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._search_results = search_results or []
        self._next_id = 100
        self.fail_with: HubSpotError | None = None

    def search(self, object_type: str, prop: str, value: str) -> list[dict[str, Any]]:
        self.calls.append(("search", object_type, prop, value))
        return self._search_results

    def create(self, object_type: str, properties: dict[str, str]) -> str:
        if self.fail_with:
            raise self.fail_with
        self.calls.append(("create", object_type, json.dumps(properties, sort_keys=True)))
        self._next_id += 1
        return str(self._next_id)

    def associate(self, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
        self.calls.append(("associate", from_type, from_id, to_type, to_id))


@pytest.fixture
def ledger() -> Iterator[Ledger]:
    led = Ledger(":memory:")
    led.start_run("run1", "lead", "test-model")
    yield led
    led.close()


@pytest.fixture
def hubspot() -> FakeHubSpot:
    return FakeHubSpot()


@pytest.fixture
def toolbox(hubspot: FakeHubSpot, ledger: Ledger) -> ToolBox:
    return ToolBox(hubspot, ledger, "run1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("tool", "bad_input", "expected"),
    [
        ("create_deal", {"dealname": "x", "company_id": "1", "amount_eur": -5000}, "amount_eur"),
        (
            "create_deal",
            {"dealname": "x", "company_id": "1", "amount_eur": "about fifty thousand"},
            "amount_eur",
        ),
        ("create_deal", {"dealname": "x"}, "company_id"),
        ("create_contact", {"firstname": "Jonas", "email": "not-an-email"}, "email"),
        ("find_company", {"domain": "not a domain"}, "domain"),
        # A field the tool does not define is rejected, not silently dropped.
        ("create_company", {"name": "Acme", "delete_all": True}, "delete_all"),
    ],
)
def test_invalid_input_is_rejected_before_hubspot(
    toolbox: ToolBox, hubspot: FakeHubSpot, tool: str, bad_input: dict[str, Any], expected: str
) -> None:
    result = toolbox.execute(tool, bad_input)

    assert result.is_error
    assert expected in result.content
    assert "nothing was written" in result.content
    assert hubspot.calls == []


def test_unknown_tool_is_an_error(toolbox: ToolBox) -> None:
    result = toolbox.execute("delete_everything", {})
    assert result.is_error


def test_domain_is_normalised(toolbox: ToolBox, hubspot: FakeHubSpot) -> None:
    toolbox.execute("find_company", {"domain": "https://www.Mueller-Baeckerei.de/kontakt"})
    assert hubspot.calls == [("search", "companies", "domain", "mueller-baeckerei.de")]


def test_create_deal_writes_marker_links_records_and_ledger(
    toolbox: ToolBox, hubspot: FakeHubSpot, ledger: Ledger
) -> None:
    result = toolbox.execute(
        "create_deal",
        {
            "dealname": "Müller solar",
            "company_id": "7",
            "contact_id": "8",
            "amount_eur": 60000,
            "notes": "400m2",
        },
    )

    assert not result.is_error
    deal_id = json.loads(result.content)["deal_id"]
    created = json.loads(hubspot.calls[0][2])
    assert created["amount"] == "60000.00"
    assert created["description"].startswith(DEMO_MARKER)
    assert ("associate", "deals", deal_id, "companies", "7") in hubspot.calls
    assert ("associate", "deals", deal_id, "contacts", "8") in hubspot.calls
    assert [r.object_id for r in ledger.created_records("run1")] == [deal_id]


def test_unset_fields_are_not_sent(toolbox: ToolBox, hubspot: FakeHubSpot) -> None:
    toolbox.execute("create_contact", {"firstname": "Jonas"})
    assert json.loads(hubspot.calls[0][2]) == {"firstname": "Jonas"}


def test_hubspot_failure_becomes_error_result_and_nothing_is_ledgered(
    toolbox: ToolBox, hubspot: FakeHubSpot, ledger: Ledger
) -> None:
    hubspot.fail_with = HubSpotError("conflict", status_code=409, retryable=False)

    result = toolbox.execute("create_company", {"name": "Acme"})

    assert result.is_error
    assert "409" in result.content
    assert ledger.created_records() == []


def test_definitions_are_generated_from_input_models(toolbox: ToolBox) -> None:
    defs = {d["name"]: d for d in toolbox.definitions()}
    assert set(defs) == {
        "find_company",
        "find_contact",
        "create_company",
        "create_contact",
        "create_deal",
    }
    deal_schema = defs["create_deal"]["input_schema"]
    assert deal_schema["additionalProperties"] is False
    assert set(deal_schema["required"]) == {"dealname", "company_id"}
