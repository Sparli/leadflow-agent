"""A deliberately small HubSpot CRM client.

Only the five calls the agent needs. The HTTP client is injected so tests can
swap in a mock transport and never touch the network, and `sleep` is injected
so retry tests run instantly instead of waiting out real backoff.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx
import structlog

from leadflow.errors import HubSpotError

log = structlog.get_logger(__name__)

ObjectType = Literal["companies", "contacts", "deals"]

_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


class HubSpotClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._sleep = sleep

    @classmethod
    def from_key(cls, service_key: str, base_url: str = "https://api.hubapi.com") -> HubSpotClient:
        http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {service_key}"},
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        return cls(http)

    def search(self, object_type: ObjectType, prop: str, value: str) -> list[dict[str, Any]]:
        """Exact-match search on one property. Used for duplicate checks."""
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}
            ],
            "properties": _DEFAULT_PROPERTIES[object_type],
            "limit": 5,
        }
        data = self._request("POST", f"/crm/v3/objects/{object_type}/search", json=body)
        results: list[dict[str, Any]] = data.get("results", [])
        return results

    def create(self, object_type: ObjectType, properties: dict[str, str]) -> str:
        data = self._request(
            "POST", f"/crm/v3/objects/{object_type}", json={"properties": properties}
        )
        return str(data["id"])

    def associate(
        self, from_type: ObjectType, from_id: str, to_type: ObjectType, to_id: str
    ) -> None:
        # The "default" association endpoint picks HubSpot's standard label, so the
        # code never hard-codes numeric association type IDs.
        self._request(
            "PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
        )

    def archive(self, object_type: ObjectType, object_id: str) -> None:
        """Move a record to HubSpot's recycle bin (recoverable for 90 days)."""
        self._request("DELETE", f"/crm/v3/objects/{object_type}/{object_id}")

    def _request(self, method: str, path: str, *, json: Any = None) -> dict[str, Any]:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._http.request(method, path, json=json)
            except httpx.TransportError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise HubSpotError(
                        f"network error: {exc}", status_code=None, retryable=True
                    ) from exc
                self._backoff(attempt, retry_after=None)
                continue

            if response.status_code < 400:
                return response.json() if response.content else {}

            retryable = response.status_code in _RETRYABLE
            if not retryable or attempt == _MAX_ATTEMPTS:
                raise HubSpotError(
                    f"{method} {path} -> {response.status_code}: {_error_message(response)}",
                    status_code=response.status_code,
                    retryable=retryable,
                )
            log.warning("hubspot.retry", status=response.status_code, attempt=attempt, path=path)
            self._backoff(attempt, retry_after=response.headers.get("Retry-After"))

        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int, *, retry_after: str | None) -> None:
        # Honour the server's own Retry-After when it sends one; otherwise
        # exponential backoff with jitter so parallel runs don't retry in lockstep.
        if retry_after is not None:
            try:
                self._sleep(min(float(retry_after), 30.0))
            except ValueError:
                pass
            else:
                return
        self._sleep(min(2**attempt, 16) * (0.5 + random.random()))  # noqa: S311 - jitter, not crypto


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", response.text[:200]))
    except ValueError:
        return response.text[:200]


_DEFAULT_PROPERTIES: dict[ObjectType, list[str]] = {
    "companies": ["name", "domain", "city"],
    "contacts": ["firstname", "lastname", "email", "phone"],
    "deals": ["dealname", "amount", "dealstage"],
}
