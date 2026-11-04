"""Saved-session navigation does not create or mutate conversations."""

import importlib
import io
import json
from pathlib import Path


def client_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("chat")
    monkeypatch.setattr(module, "authorization_headers", lambda: {})
    return module


def test_list_sessions_does_not_create_a_new_conversation(monkeypatch, capsys):
    client = client_module(monkeypatch)
    calls = []

    def response(request, **kwargs):
        calls.append((request.method, request.full_url))
        return io.BytesIO(json.dumps({"conversations": []}).encode())

    monkeypatch.setattr(client, "urlopen", response)
    monkeypatch.setattr("sys.argv", ["chat.py", "--list-sessions", "--page", "2"])
    client.main()
    assert calls == [("GET", "http://127.0.0.1:8000/conversations?limit=20&offset=20")]
    assert "No saved conversations" in capsys.readouterr().out


def test_resume_displays_history_and_routes_next_message_to_selected_session(monkeypatch, capsys):
    client = client_module(monkeypatch)
    first = "00000000-0000-0000-0000-000000000001"
    second = "00000000-0000-0000-0000-000000000002"
    posts = []

    def response(request, **kwargs):
        path = request.full_url.removeprefix("http://127.0.0.1:8000")
        if request.method == "POST":
            posts.append(path)
            value = {"reply": "Resumed successfully"}
        elif "/messages?" in path:
            value = {"messages": [], "next_after": 7}
            if "after=0&" in path:
                value["messages"] = [
                    {"role": "assistant", "content": "Saved history " + path.split("/")[2]}
                ]
        else:
            value = {"mode": "commerce"}
        return io.BytesIO(json.dumps(value).encode())

    commands = iter(["/resume " + second, "Continue", "/exit"])
    monkeypatch.setattr(client, "urlopen", response)
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    monkeypatch.setattr("sys.argv", ["chat.py", "--session", first])
    client.main()
    text = capsys.readouterr().out
    assert "Saved history " + first in text and "Saved history " + second in text
    assert posts == [f"/conversations/{second}/messages"]
