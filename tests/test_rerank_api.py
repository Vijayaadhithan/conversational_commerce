from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import Settings, create_app


def test_rerank_validates_and_passes_documents_without_reordering(tmp_path):
    with TestClient(
        create_app(replace(Settings(), database=str(tmp_path / "test.sqlite3")))
    ) as client:

        class Reranker:
            def rank(self, query, documents, top_k):
                assert query == "blue shirt"
                assert documents == ["Red bag", "Blue cotton shirt"]
                assert top_k == 1
                return {
                    "device": "cpu",
                    "results": [{"index": 1, "text": documents[1], "score": 8.0}],
                }

        client.app.state.reranker = Reranker()
        response = client.post(
            "/rerank",
            json={"query": "blue shirt", "documents": ["Red bag", "Blue cotton shirt"], "top_k": 1},
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["index"] == 1
        for documents in [[], ["  "], ["🙂" * 1001], ["shirt"] * 51]:
            assert (
                client.post("/rerank", json={"query": "shirt", "documents": documents}).status_code
                == 422
            )


def test_missing_reranker_returns_actionable_unavailable_status(tmp_path, monkeypatch):
    monkeypatch.setenv("RERANK_MODEL_PATH", str(tmp_path / "missing"))
    with TestClient(
        create_app(replace(Settings(), database=str(tmp_path / "test.sqlite3")))
    ) as client:
        response = client.post("/rerank", json={"query": "shirt", "documents": ["Blue shirt"]})
        assert response.status_code == 503
        assert "reranker is missing" in response.json()["detail"]
