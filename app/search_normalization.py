"""Small reviewed search aliases. Never change saved user evidence or hard filters."""

import re

ALIASES = {
    "dandruf": "dandruff",
    "dandruff": "dandruff",
    "shampo": "shampoo",
    "moisturiser": "moisturizer",
    "sneekers": "sneakers",
    "jewellery": "jewelry",
}


def normalize_search(text):
    return re.sub(
        r"\b(?:" + "|".join(ALIASES) + r")\b",
        lambda match: ALIASES[match.group().lower()],
        text,
        flags=re.I,
    )
