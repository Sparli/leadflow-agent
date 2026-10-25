"""HubSpot client: retries what is transient, fails fast on what is not."""

from __future__ import annotations

import httpx
import pytest
import respx

from leadflow.errors import HubSpotError
from leadflow.hubspot import HubSpotClient

BASE = "https://api.hubapi.test"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> HubSpotClient:
    return HubSpotClient(httpx.Client(base_url=BASE), sleep=sleeps.append)


@respx.mock
def test_create_returns_id(client: HubSpotClient) -> None:
    route = respx.post(f"{BASE}/crm/v3/objects/companies").respond(201, json={"id": "42"})
    assert client.create("companies", {"name": "Acme"}) == "42"
    assert route.calls.last.request.content == b'{"properties":{"name":"Acme"}}'


@respx.mock
def test_rate_limit_is_retried_honouring_retry_after(
    client: HubSpotClient, sleeps: list[float]
) -> None:
    respx.post(f"{BASE}/crm/v3/objects/contacts").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "slow down"}),
            httpx.Response(201, json={"id": "7"}),
        ]
    )
    assert client.create("contacts", {"firstname": "J"}) == "7"
    assert sleeps == [2.0]


@respx.mock
def test_reads_retry_server_errors_then_give_up(client: HubSpotClient, sleeps: list[float]) -> None:
    respx.post(f"{BASE}/crm/v3/objects/deals/search").respond(503, json={"message": "unavailable"})
    with pytest.raises(HubSpotError) as exc:
        client.search("deals", "dealname", "x")
    assert exc.value.status_code == 503
    assert exc.value.retryable
    assert len(sleeps) == 3  # four attempts, three waits


@respx.mock
def test_client_errors_are_not_retried(client: HubSpotClient, sleeps: list[float]) -> None:
    route = respx.post(f"{BASE}/crm/v3/objects/deals").respond(
        400, json={"message": "Property values were not valid"}
    )
    with pytest.raises(HubSpotError, match="Property values were not valid") as exc:
        client.create("deals", {"dealname": "x"})
    assert not exc.value.retryable
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_search_sends_exact_match_filter(client: HubSpotClient) -> None:
    route = respx.post(f"{BASE}/crm/v3/objects/companies/search").respond(
        200, json={"results": [{"id": "1", "properties": {"domain": "acme.de"}}]}
    )
    results = client.search("companies", "domain", "acme.de")
    assert results[0]["id"] == "1"
    body = route.calls.last.request.read()
    assert b'"operator":"EQ"' in body
    assert b'"value":"acme.de"' in body
