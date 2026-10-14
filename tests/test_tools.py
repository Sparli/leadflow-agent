"""Tools: bad arguments never reach HubSpot, and create tools only propose."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from leadflow.errors import HubSpotError
from leadflow.ledger import Ledger
from leadflow.tools import ToolBox


class FakeHubSpot:
    """Records calls instead of making them. Any call is visible in `calls`."""

    def __init__(self, search_results: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._search_results = search_results or []
        self._next_id = 100
        self.fail_with: HubSpotError | None = None
        self.fail_associate: HubSpotError | None = None

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
        if self.fail_associate:
            raise self.fail_associate
        self.calls.append(("associate", from_type, from_id, to_type, to_id))

    def writes(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] != "search"]


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


def test_create_tools_propose_and_never_write(
    toolbox: ToolBox, hubspot: FakeHubSpot, ledger: Ledger
) -> None:
    company = json.loads(toolbox.execute("create_company", {"name": "Acme"}).content)
    contact = json.loads(
        toolbox.execute(
            "create_contact", {"firstname": "Jonas", "company_id": company["company_id"]}
        ).content
    )
    deal = toolbox.execute(
        "create_deal",
        {
            "dealname": "Acme solar",
            "company_id": company["company_id"],
            "contact_id": contact["contact_id"],
            "amount_eur": 60000,
        },
    )

    assert not deal.is_error
    assert company["company_id"].startswith("pending-")
    assert hubspot.calls == []
    assert ledger.created_records() == []
    assert [p.tool for p in ledger.proposals("run1")] == [
        "create_company",
        "create_contact",
        "create_deal",
    ]


def test_links_must_point_at_ids_this_run_has_seen(toolbox: ToolBox, ledger: Ledger) -> None:
    # An id the model never received, e.g. copied from the enquiry text.
    result = toolbox.execute("create_deal", {"dealname": "x", "company_id": "999"})

    assert result.is_error
    assert "999" in result.content
    assert ledger.proposals("run1") == []


def test_deal_must_link_the_contact_this_run_has(toolbox: ToolBox) -> None:
    company = json.loads(toolbox.execute("create_company", {"name": "Acme"}).content)
    toolbox.execute("create_contact", {"firstname": "Anke", "company_id": company["company_id"]})

    result = toolbox.execute(
        "create_deal", {"dealname": "Acme solar", "company_id": company["company_id"]}
    )

    assert result.is_error
    assert "contact_id" in result.content


def test_found_ids_can_be_linked(ledger: Ledger) -> None:
    hubspot = FakeHubSpot(search_results=[{"id": "42", "properties": {"domain": "acme.de"}}])
    toolbox = ToolBox(hubspot, ledger, "run1")  # type: ignore[arg-type]
    toolbox.execute("find_company", {"domain": "acme.de"})

    result = toolbox.execute("create_contact", {"firstname": "Jonas", "company_id": "42"})

    assert not result.is_error


def test_search_failure_becomes_error_result(toolbox: ToolBox, hubspot: FakeHubSpot) -> None:
    def broken(*_: Any) -> list[dict[str, Any]]:
        raise HubSpotError("unavailable", status_code=503, retryable=True)

    hubspot.search = broken  # type: ignore[method-assign,assignment]
    result = toolbox.execute("find_company", {"domain": "acme.de"})

    assert result.is_error
    assert "503" in result.content


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
