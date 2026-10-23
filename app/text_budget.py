"""UTF-8 prompt sizing and bounded text chunks."""

import json


def byte_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def clip(text, limit):
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def chunks(text, limit):
    while text:
        part = clip(text, limit)
        yield part
        text = text[len(part) :]
