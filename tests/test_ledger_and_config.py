"""Ledger cleanup ordering, secret redaction, and config failure messages."""

from __future__ import annotations

from pathlib import Path

import pytest

from leadflow.config import load_settings
from leadflow.errors import ConfigError
from leadflow.ledger import Ledger
from leadflow.logging import REDACTED, redact_secrets


def test_cleanup_order_is_newest_first_and_skips_archived() -> None:
    ledger = Ledger(":memory:")
    ledger.start_run("r", "lead", "m")
    ledger.record_write("r", "companies", "1", "create", {})
    ledger.record_write("r", "contacts", "2", "create", {})
    ledger.record_write("r", "contacts", "2", "associate", {"to": "companies"})
    ledger.record_write("r", "deals", "3", "create", {})
    ledger.mark_archived("contacts", "2")

    ids = [r.object_id for r in ledger.created_records()]

    assert ids == ["3", "1"]
    ledger.close()


def test_secrets_are_redacted_by_key_and_by_value() -> None:
    event = {
        "event": "call failed: Bearer NOT-A-REAL-KEY-test-fixture-only rejected",
        "api_key": "anything",
        "nested": {"token": "x", "safe": "visible"},
    }
    out = redact_secrets(None, "info", event)
    assert "NOT-A-REAL-KEY" not in out["event"]
    assert out["api_key"] == REDACTED
    assert out["nested"] == {"token": REDACTED, "safe": "visible"}


def test_missing_settings_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("HUBSPOT_SERVICE_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "NOT-A-REAL-KEY-test-fixture-only")
    with pytest.raises(ConfigError, match="HUBSPOT_SERVICE_KEY"):
        load_settings()
