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
    # A measured regression: BM25 treated this misspelled preference as a match
    # for "Kessa" exfoliating gloves. Correct only the complete known phrase;
    # do not rewrite brand/product names containing "Kess" or infer ingredients.
    text = re.sub(r"\bkess\s+chemincal\b", "less chemical", text, flags=re.I)
    return re.sub(
        r"\b(?:" + "|".join(ALIASES) + r")\b",
        lambda match: ALIASES[match.group().lower()],
        text,
        flags=re.I,
    )
