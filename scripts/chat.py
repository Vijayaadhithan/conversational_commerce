"""Optional terminal client for the backend. No model logic or frontend."""

import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from api_credentials import authorization_headers


def show_history(request, session):
    after = 0
    while True:
        page = request("GET", f"/conversations/{session}/messages?after={after}&limit=500")
        for message in page["messages"]:
            print(f"{message['role']}: {message['content']}")
        if not page["messages"]:
            break
        after = page["next_after"]


def show_sessions(request, page=1):
    if not 1 <= page <= 10000:
        raise ValueError("Page must be between 1 and 10000")
    records = request("GET", f"/conversations?limit=20&offset={(page - 1) * 20}")["conversations"]
    if not records:
        print("No saved conversations on this page for your account.")
        return
    print(f"Saved conversations — page {page}:")
    for record in records:
        title = json.dumps(record["title"], ensure_ascii=False)
        print(f"{record['id']}  {record['mode']}  {record['updated_at']}  {title}")
    print("Use /resume CONVERSATION_ID to reopen one.")
    if len(records) == 20:
        print(f"Next page: /sessions {page + 1}")


def main():
    parser = argparse.ArgumentParser(description="Talk to your local conversational API")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--session",
        type=lambda value: str(UUID(value)),
        help="Resume a saved conversation ID and display its history",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List saved conversations without creating a new one",
    )
    parser.add_argument(
        "--page", type=int, default=1, help="Page for --list-sessions (20 conversations per page)"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--commerce", action="store_true", help="Create a product recommendation conversation"
    )
    modes.add_argument(
        "--general-chat",
        action="store_true",
        help="General chat; requires COMMERCE_ONLY=0 on the server",
    )
    args = parser.parse_args()
    if not 1 <= args.page <= 10000:
        parser.error("--page must be between 1 and 10000")

    def request(method, path, body=None, request_key=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", **authorization_headers()}
        if method == "POST":
            headers["Idempotency-Key"] = request_key or str(uuid4())
        req = Request(args.url.rstrip("/") + path, data=data, method=method, headers=headers)
        for attempt in range(3):
            try:
                with urlopen(req, timeout=900) as response:
                    return json.load(response)
            except HTTPError as exc:
                if (
                    attempt == 2
                    or exc.code not in {429, 503, 504}
                    and not (exc.code == 409 and exc.headers.get("Retry-After"))
                ):
                    raise
                time.sleep(min(10, max(1, int(exc.headers.get("Retry-After", "2")))))
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(2)

    try:
        if args.list_sessions:
            show_sessions(request, args.page)
            return
        session = (
            args.session
            or request(
                "POST",
                "/conversations",
                {"mode": "commerce"}
                if args.commerce
                else {"mode": "chat"}
                if args.general_chat
                else {},
            )["id"]
        )
        mode = request("GET", f"/conversations/{session}")["mode"]
        print(
            f"Conversation: {session} ({mode})\nCommands: /exit, /new, /sessions [PAGE], /resume ID, /history, /preferences, /retry"
        )
        if args.session:
            show_history(request, session)
        pending = None
        while True:
            text = input("\nYou: ").strip()
            if text == "/exit":
                break
            if not text:
                continue
            try:
                command, _, argument = text.partition(" ")
                if command in {"/new", "/resume"} and pending is not None:
                    print("Resolve the pending message with /retry before switching conversations.")
                    continue
                if text == "/new":
                    session = request("POST", "/conversations", {"mode": mode})["id"]
                    print(f"Conversation: {session}")
                elif command == "/sessions":
                    try:
                        show_sessions(request, int(argument) if argument else 1)
                    except ValueError:
                        print("Use /sessions or /sessions PAGE (1-10000).")
                elif command == "/resume":
                    try:
                        selected = str(UUID(argument.strip()))
                    except ValueError:
                        print("Use /resume CONVERSATION_ID with an ID from /sessions.")
                        continue
                    record = request("GET", f"/conversations/{selected}")
                    session, mode = selected, record["mode"]
                    print(f"Conversation: {session} ({mode})")
                    show_history(request, session)
                elif text == "/preferences":
                    print(
                        json.dumps(
                            request("GET", f"/conversations/{session}/preferences"), indent=2
                        )
                    )
                elif text == "/history":
                    show_history(request, session)
                else:
                    if text == "/retry":
                        if pending is None:
                            print("No pending message to retry.")
                            continue
                    else:
                        pending = (session, {"message": text}, str(uuid4()))
                    result = request(
                        "POST", f"/conversations/{pending[0]}/messages", pending[1], pending[2]
                    )
                    pending = None
                    print(f"\nAI: {result['reply']}")
            except HTTPError as exc:
                print(f"API error {exc.code}: {exc.read().decode()}")
            except (URLError, TimeoutError):
                print("Connection failed. Use /retry to reuse the same request key.")
        print(f"Resume: python scripts/chat.py --session {session}")
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Your completed turns remain saved.")
    except (HTTPError, URLError) as exc:
        parser.exit(1, f"Cannot open conversation: {exc}. Start the backend first.\n")


if __name__ == "__main__":
    main()
