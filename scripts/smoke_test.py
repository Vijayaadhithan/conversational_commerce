"""Check real model recall, including the summary boundary. Backend must be running."""

import json
from urllib.request import Request, urlopen


def request(method, path, body=None):
    req = Request("http://127.0.0.1:8000" + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"}, method=method)
    with urlopen(req, timeout=600) as response:
        return json.load(response) if response.status != 204 else None


def main():
    print(request("GET", "/health"), flush=True)
    session = request("POST", "/conversations", {"title": "Temporary backend smoke test"})["id"]
    path = f"/conversations/{session}"
    try:
        prompts = ["My name is Adhi and my favourite colour is cobalt blue. Reply only OK."]
        prompts += ["Reply only OK." for _ in range(7)]
        prompts += ["What is my name and my favourite colour? Answer in one short sentence."]
        for index, text in enumerate(prompts, 1):
            result = request("POST", path + "/messages", {"message": text})
            print(f'Turn {index}: {result["reply"]}', flush=True)
        reply = result["reply"].lower()
        assert "adhi" in reply and "cobalt blue" in reply, "Model did not recall the saved facts"
        record = request("GET", path)
        assert record["summarized_through"] > 0 and record["summary"], "Compaction did not run"
        assert len(request("GET", path + "/messages")["messages"]) == 18
        print("PASS: actual local model retained context after summary compaction.", flush=True)
    finally:
        request("DELETE", path)


if __name__ == "__main__":
    main()
