from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException

from app.reranker import VoyageReranker
from app.reranker_fallback import RerankerWithFallback


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_api_failure_uses_local_without_retry(monkeypatch, status):
    monkeypatch.setenv("VOYAGE_API_KEY", "test-secret")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "private provider detail"})

    primary = VoyageReranker(transport=httpx.MockTransport(handler))
    fallback = Mock()
    fallback.rank.return_value = {"results": [{"index": 1, "score": 4}]}
    reranker = RerankerWithFallback(primary, fallback)
    result = reranker.rank("query", ["a", "b"], 1, preserve_query=True)
    assert result == fallback.rank.return_value
    assert len(calls) == 1
    fallback.rank.assert_called_once_with("query", ["a", "b"], 1, preserve_query=True)
    assert reranker.fallback_calls == 1 and reranker.last_backend == "local"


@pytest.mark.parametrize("error", [RuntimeError("failed"), HTTPException(503, "circuit open")])
def test_failure_then_primary_recovers(error):
    primary = Mock(model="voyage")
    primary.rank.side_effect = [error, {"results": []}]
    fallback = Mock()
    reranker = RerankerWithFallback(primary, fallback)
    reranker.rank("q", ["doc"], 1)
    assert reranker.rank("q", ["doc"], 1) == {"results": []}
    assert fallback.rank.call_count == 1 and reranker.last_backend == "voyage"


@pytest.mark.parametrize("status", [429, 504])
def test_application_budget_and_deadline_not_bypassed(status):
    primary = Mock(model="voyage")
    primary.rank.side_effect = HTTPException(status, "application limit")
    fallback = Mock()
    with pytest.raises(HTTPException) as error:
        RerankerWithFallback(primary, fallback).rank("q", ["doc"], 1)
    assert error.value.status_code == status
    fallback.rank.assert_not_called()


def test_success_does_not_load_fallback():
    primary = Mock(model="voyage")
    primary.rank.return_value = {"results": []}
    fallback = Mock()
    assert RerankerWithFallback(primary, fallback).rank("q", ["doc"], 1) == {"results": []}
    fallback.rank.assert_not_called()


def test_fallback_failure_is_not_hidden():
    primary, fallback = Mock(model="voyage"), Mock()
    primary.rank.side_effect = RuntimeError("api failed")
    fallback.rank.side_effect = RuntimeError("local failed")
    with pytest.raises(RuntimeError, match="local failed"):
        RerankerWithFallback(primary, fallback).rank("q", ["doc"], 1)
