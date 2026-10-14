"""Approval: the plan is written in order, never twice, and stops at the first failure."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from leadflow.approval import Approver
from leadflow.errors import HubSpotError
from leadflow.ledger import Ledger
from leadflow.tools import DEMO_MARKER, ToolBox
from tests.test_tools import FakeHubSpot


@pytest.fixture
def ledger() -> Iterator[Ledger]:
    led = Ledger(":memory:")
    led.start_run("run1", "lead", "test-model")
    yield led
    led.close()


def _propose_full_plan(ledger: Ledger) -> None:
    """What the agent does for a new lead: company, contact, deal, linked by placeholders."""
    toolbox = ToolBox(FakeHubSpot(), ledger, "run1")  # type: ignore[arg-type]
    company = json.loads(
        toolbox.execute("create_company", {"name": "Acme", "domain": "acme.de"}).content
    )["company_id"]
    contact = json.loads(
        toolbox.execute(
            "create_contact",
            {"firstname": "Jonas", "email": "j@acme.de", "company_id": company},
        ).content
    )["contact_id"]
    toolbox.execute(
        "create_deal",
        {
            "dealname": "Acme solar",
            "company_id": company,
            "contact_id": contact,
            "amount_eur": 60000,
            "notes": "400m2",
        },
    )


def test_approve_writes_in_order_with_real_ids(ledger: Ledger) -> None:
    _propose_full_plan(ledger)
    hubspot = FakeHubSpot()

    outcomes = Approver(hubspot, ledger).approve("run1")  # type: ignore[arg-type]

    assert [o.status for o in outcomes] == ["executed"] * 3
    company_id, contact_id, deal_id = (o.object_id for o in outcomes)
    assert ("associate", "contacts", contact_id, "companies", company_id) in hubspot.calls
    assert ("associate", "deals", deal_id, "companies", company_id) in hubspot.calls
    assert ("associate", "deals", deal_id, "contacts", contact_id) in hubspot.calls
    deal_props = json.loads(next(c for c in hubspot.calls if c[:2] == ("create", "deals"))[2])
    assert deal_props["amount"] == "60000.00"
    assert deal_props["description"] == f"{DEMO_MARKER} 400m2"
    assert len(ledger.created_records("run1")) == 3


def test_duplicate_created_after_the_proposal_is_reused(ledger: Ledger) -> None:
    _propose_full_plan(ledger)
    # Someone created the company in HubSpot between the proposal and the approval.
    hubspot = FakeHubSpot(search_results=[{"id": "77"}])

    outcomes = Approver(hubspot, ledger).approve("run1")  # type: ignore[arg-type]

    assert outcomes[0].status == "reused"
    assert outcomes[0].object_id == "77"
    assert not any(c[:2] == ("create", "companies") for c in hubspot.calls)


def test_failure_stops_the_plan_and_retry_resumes_without_duplicates(ledger: Ledger) -> None:
    _propose_full_plan(ledger)
    hubspot = FakeHubSpot()
    hubspot.fail_associate = HubSpotError("unavailable", status_code=503, retryable=True)

    first = Approver(hubspot, ledger).approve("run1")  # type: ignore[arg-type]

    # The company is created; linking the contact fails, so the deal is never attempted.
    assert [o.status for o in first] == ["executed", "failed"]

    hubspot.fail_associate = None
    second = Approver(hubspot, ledger).approve("run1")  # type: ignore[arg-type]

    assert [o.status for o in second] == ["executed", "executed"]
    creates = [c[1] for c in hubspot.calls if c[0] == "create"]
    assert creates == ["companies", "contacts", "deals"]  # the contact was not created twice


def test_reject_writes_nothing(ledger: Ledger) -> None:
    _propose_full_plan(ledger)
    hubspot = FakeHubSpot()
    approver = Approver(hubspot, ledger)  # type: ignore[arg-type]

    assert approver.reject("run1") == 3
    assert approver.approve("run1") == []
    assert hubspot.calls == []
