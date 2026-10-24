"""Connect the sibling Querix frontend locally without printing or committing secrets."""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values

from app.config import ROOT, Settings
from app.conversation_store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", type=Path, default=ROOT.parent / "querixco")
    args = parser.parse_args()
    frontend = args.frontend.resolve()
    if not (frontend / "src/features/commerce/gateway.server.ts").is_file():
        parser.error("Expected the Querix frontend with the commerce gateway installed")
    target = frontend / ".dev.vars"
    existing = dotenv_values(target) if target.exists() else {}
    names = ("COMMERCE_API_URL", "COMMERCE_API_TOKEN", "COMMERCE_SESSION_SECRET")
    if any(existing.get(name) for name in names):
        if all(existing.get(name) for name in names):
            print("Existing commerce bindings preserved. Restart the frontend to use them.")
            return
        parser.error(
            "Partial commerce bindings exist; complete them explicitly without overwriting"
        )
    database = Path(Settings().database)
    store = Store(database if database.is_absolute() else ROOT / database)
    token_path = ROOT / "data/credentials/querix-demo.token"
    if token_path.exists():
        token = token_path.read_text().strip()
        identity = store.authenticate(token)
        if not identity or identity["role"] != "user":
            parser.error("Existing demo token is revoked or not a non-admin identity")
    else:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            _, token = store.issue_token("querix-local-demo", "user")
            output.write(token + "\n")
    values = {
        "COMMERCE_API_URL": "http://127.0.0.1:8000",
        "COMMERCE_API_TOKEN": token,
        "COMMERCE_SESSION_SECRET": secrets.token_urlsafe(48),
    }
    # Preserve unrelated Worker bindings. The credential file is ignored by Querix Git.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "a") as output:
        output.write("\n# Local commerce demo: server-only bindings. Never commit this file.\n")
        for key, value in values.items():
            output.write(f"{key}={json.dumps(value)}\n")
    print("Local frontend connection configured with a dedicated non-admin demo identity.")
    print("Credentials are in ignored, owner-readable files; no secret values are printed.")


if __name__ == "__main__":
    main()
