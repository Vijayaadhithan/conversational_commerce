"""Shared client credentials. Never include token values in diagnostics."""
import os
from pathlib import Path
from dotenv import load_dotenv


def authorization_headers():
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / '.env')
    token = os.getenv('COMMERCE_API_TOKEN', '')
    token_file = os.getenv('COMMERCE_API_TOKEN_FILE', '')
    if not token and token_file:
        path = Path(token_file).expanduser()
        token = (path if path.is_absolute() else root / path).read_text().strip()
    return {'Authorization': 'Bearer ' + token} if token else {}
