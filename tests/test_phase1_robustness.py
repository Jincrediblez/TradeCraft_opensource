#!/usr/bin/env python3
"""Phase 1 stability tests: robust JSON reads + Kimi API retry/fallback."""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from app import state_store
from app import llm_reporter


# ---------------------------------------------------------------------------
# state_store.read_json — corruption must not crash callers
# ---------------------------------------------------------------------------
def test_read_json_missing_returns_default(tmp_path):
    assert state_store.read_json(tmp_path / "nope.json", {"d": 1}) == {"d": 1}


def test_read_json_corrupted_returns_default_and_logs(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with mock.patch.object(state_store, "app_log") as logged:
        result = state_store.read_json(bad, [])
    assert result == []
    assert logged.called
    # logged at error level so a corrupt state file is visible, not silent
    assert logged.call_args.kwargs.get("level") == "error"


def test_read_json_valid_roundtrip(tmp_path):
    path = tmp_path / "ok.json"
    state_store.atomic_write_text(path, '{"a": 1}')
    assert state_store.read_json(path, {}) == {"a": 1}


# ---------------------------------------------------------------------------
# Kimi API retry/backoff via tenacity
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


_GOOD_PAYLOAD = {"choices": [{"message": {"content": "Audit conclusion"}}]}


def test_kimi_retries_then_succeeds():
    responses = [_FakeResp(429, text="rate limit"), _FakeResp(200, _GOOD_PAYLOAD)]
    with mock.patch.object(llm_reporter.requests, "post", side_effect=responses) as post, \
            mock.patch("time.sleep"):  # make tenacity backoff instant
        content, err = llm_reporter.call_kimi_api_with_error("prompt", "key")
    assert content == "Audit conclusion"
    assert err is None
    assert post.call_count == 2


def test_kimi_does_not_retry_on_auth_failure():
    with mock.patch.object(llm_reporter.requests, "post",
                           return_value=_FakeResp(401, text="invalid key")) as post, \
            mock.patch("time.sleep"):
        content, err = llm_reporter.call_kimi_api_with_error("prompt", "key")
    assert content is None
    assert err  # user-facing message present
    assert post.call_count == 1  # auth failures are fatal — no retry


def test_kimi_does_not_retry_on_quota_exhausted():
    with mock.patch.object(llm_reporter.requests, "post",
                           return_value=_FakeResp(429, text="insufficient balance")) as post, \
            mock.patch("time.sleep"):
        content, err = llm_reporter.call_kimi_api_with_error("prompt", "key")
    assert content is None
    assert post.call_count == 1  # quota is fatal, not transient


def test_kimi_exhausts_retries_on_server_error():
    with mock.patch.object(llm_reporter.requests, "post",
                           return_value=_FakeResp(500, text="boom")) as post, \
            mock.patch("time.sleep"):
        content, err = llm_reporter.call_kimi_api_with_error("prompt", "key")
    assert content is None
    assert post.call_count == llm_reporter.KIMI_MAX_ATTEMPTS


def test_kimi_malformed_body_is_retryable():
    with mock.patch.object(llm_reporter.requests, "post",
                           return_value=_FakeResp(200, {"choices": []})) as post, \
            mock.patch("time.sleep"):
        content, err = llm_reporter.call_kimi_api_with_error("prompt", "key")
    assert content is None
    assert post.call_count == llm_reporter.KIMI_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# cap_context_json — oversized prompts get truncated
# ---------------------------------------------------------------------------
def test_cap_context_json_passthrough_when_small():
    small = '{"a": 1}'
    assert llm_reporter.cap_context_json(small) == small


def test_cap_context_json_truncates_when_oversized():
    big = "x" * (llm_reporter.MAX_CONTEXT_CHARS + 100)
    out = llm_reporter.cap_context_json(big)
    assert len(out) < len(big)
    assert "truncated" in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
