"""Optional terminal client for the backend. No model logic or frontend."""

import argparse
import json
import time
from uuid import uuid4
from api_credentials import authorization_headers
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description="Talk to your local conversational API")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--session", help="Resume a saved conversation ID")
    parser.add_argument("--commerce", action="store_true", help="Create a product recommendation conversation")
    args = parser.parse_args()

    def request(method, path, body=None, request_key=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", **authorization_headers()}
        if method == "POST": headers["Idempotency-Key"] = request_key or str(uuid4())
        req = Request(args.url.rstrip("/") + path, data=data, method=method,
                      headers=headers)
        for attempt in range(3):
            try:
                with urlopen(req, timeout=900) as response: return json.load(response)
            except HTTPError as exc:
                if attempt == 2 or exc.code not in {429, 503, 504} and not (exc.code == 409 and exc.headers.get('Retry-After')): raise
                time.sleep(min(10, max(1, int(exc.headers.get('Retry-After', '2')))))
            except (URLError, TimeoutError):
                if attempt == 2: raise
                time.sleep(2)

    try:
        session = args.session or request("POST", "/conversations", {"mode": "commerce" if args.commerce else "chat"})["id"]
        mode = request("GET", f"/conversations/{session}")["mode"]
        print(f"Conversation: {session} ({mode})\nCommands: /exit, /new, /history, /preferences, /retry")
        pending = None
        while True:
            text = input("\nYou: ").strip()
            if text == "/exit":
                break
            if not text:
                continue
            try:
                if text == "/new":
                    session = request("POST", "/conversations", {"mode": mode})["id"]
                    print(f"Conversation: {session}")
                elif text == "/preferences":
                    print(json.dumps(request("GET", f"/conversations/{session}/preferences"), indent=2))
                elif text == "/history":
                    after = 0
                    while True:
                        page = request("GET", f"/conversations/{session}/messages?after={after}&limit=500")
                        for message in page["messages"]:
                            print(f'{message["role"]}: {message["content"]}')
                        if not page["messages"]:
                            break
                        after = page["next_after"]
                else:
                    if text == '/retry':
                        if pending is None:
                            print('No pending message to retry.')
                            continue
                    else:
                        pending = (session, {"message": text}, str(uuid4()))
                    result = request("POST", f"/conversations/{pending[0]}/messages", pending[1], pending[2])
                    pending = None
                    print(f'\nAI: {result["reply"]}')
            except HTTPError as exc:
                print(f"API error {exc.code}: {exc.read().decode()}")
            except (URLError, TimeoutError) as exc:
                print("Connection failed. Use /retry to reuse the same request key.")
        print(f"Resume: python scripts/chat.py --session {session}")
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Your completed turns remain saved.")
    except (HTTPError, URLError) as exc:
        parser.exit(1, f"Cannot open conversation: {exc}. Start the backend first.\n")


if __name__ == "__main__":
    main()
