"""Text for validated shopping conversations."""

import re
import unicodedata
from decimal import Decimal

from app.commerce_taxonomy import FAMILIES


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def evidence_ok(quote, message):
    return bool(quote.strip()) and normalized(quote) in normalized(message)


def negated(value, message):
    text, value = (
        normalized(message).replace("-", " "),
        re.escape(normalized(value).replace("-", " ")),
    )
    return bool(
        re.search(
            r"\b(?:not|no|without|avoid|exclude|except|rather than|don.t want|do not want|don.t need|do not need)\s+(?:any\s+|a\s+|an\s+)?"
            + value
            + r"\b",
            text,
        )
        or re.search(value + r"(?: is)? (?:not required|not wanted|not necessary)\b", text)
    )


ONES = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
        range(20),
    )
)


TENS = dict(
    zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10))
)


def number_text(text):
    text = normalized(text)
    words = {**ONES, **TENS, "one hundred": 100, "a hundred": 100}
    words.update(
        {
            f"{ten} {one}": base + amount
            for ten, base in TENS.items()
            for one, amount in ONES.items()
            if 0 < amount < 10
        }
    )
    pattern = (
        r"\b(?:" + "|".join(re.escape(key) for key in sorted(words, key=len, reverse=True)) + r")\b"
    )
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    return re.sub(pattern, lambda match: str(words[match.group()]), text)


def amounts(text):
    return {
        Decimal(value)
        for value in re.findall(r"(?<!\w)[+-]?\d+(?:\.\d{1,2})?(?![\w.])", number_text(text))
    }


FOREIGN_CURRENCY = r"(?:₹|€|£|\b(?:INR|EUR|GBP|CAD|AUD|JPY|CNY|SGD|rupees)\b|\bRs\.?\s*\d)"


def product_mentions(text):
    matches = []
    for family, (_, terms) in FAMILIES.items():
        for term in terms:
            suffix = r"\w*" if term in {"moisturi", "accessor"} else r"(?:s|es)?\b"
            for match in re.finditer(r"\b" + re.escape(term) + suffix, text, re.I):
                matches.append((match.start(), match.end(), family, match.group()))
    # Prefer 'makeup brush' to the overlapping 'makeup', for example.
    selected = []
    for item in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0])):
        if not any(item[0] < other[1] and other[0] < item[1] for other in selected):
            selected.append(item)
    return sorted(selected)


def matching_families(text):
    return list(dict.fromkeys(item[2] for item in product_mentions(text)))


def product_keys(text):
    return {normalized(item[3]).rstrip("s") for item in product_mentions(text)}


def task_matches(task, message):
    return bool(
        task.query
        and (
            normalized(task.query) in normalized(message)
            or product_keys(task.query) & product_keys(message)
        )
    )


def budget_overrides(message):
    text = number_text(message)
    number = r"\$?\s*(\d+(?:\.\d{1,2})?)(?![\d.])"
    result = {}
    for pattern, key, step in [
        (r"(?:under|below|less than)\s*" + number, "max_price", Decimal("-0.01")),
        (
            r"(?:up to|at most|maximum(?: budget)?(?: is)?|max(?: budget)?(?: is)?|budget(?: is| of)?)\s*"
            + number,
            "max_price",
            Decimal(0),
        ),
        (r"(?:at least|minimum(?: is)?)\s*" + number, "min_price", Decimal(0)),
        (r"(?:over|more than)\s*" + number, "min_price", Decimal("0.01")),
    ]:
        matches = list(re.finditer(r"\b" + pattern, text))
        if matches:
            result[key] = str(Decimal(matches[-1].group(1)) + step)
    match = re.search(r"between\s*" + number + r"\s*and\s*" + number, text)
    if match:
        result.update(min_price=match.group(1), max_price=match.group(2))
    return result


def can_clear(field, quote):
    quote = normalized(quote).replace("-", " ")
    if re.search(
        r"don.t (?:remove|ignore|drop|forget|clear)|do not (?:remove|ignore|drop|forget|clear)|never (?:remove|ignore|drop|forget|clear)",
        quote,
    ):
        return False
    aliases = {
        "query": r"search|product|start over",
        "need": r"need|skin|hair|concern",
        "recipient": r"recipient|who|person",
        "domain": r"category|domain",
        "family": r"category|type|family",
        "brand": r"brand",
        "min_price": r"budget|price|minimum",
        "max_price": r"budget|price|maximum",
        "unsupported": r"requirement|constraint|that|stock|delivery|allergy|safe|currency",
    }
    target = aliases.get(field, re.escape(field.removeprefix("attribute.").replace("_", " ")))
    return bool(
        re.search(target, quote)
        and re.search(
            r"any|remove|ignore|drop|forget|no longer|not required|not necessary|no preference|doesn.t matter|don.t (?:care|need|require)|without|no budget",
            quote,
        )
    )


def reference_positions(message, count):
    text = re.sub(r"\btask\s+[1-5]\b", "", normalized(message))
    values = []
    for word, end in (("two", 2), ("three", 3), ("four", 4), ("five", 5)):
        if "first " + word in text:
            values.extend(range(1, end + 1))
    for word, position in [("first", 1), ("second", 2), ("third", 3), ("fourth", 4), ("fifth", 5)]:
        if re.search(r"\b" + word + r"\b", text):
            values.append(position)
    values.extend(int(value) for value in re.findall(r"\b([1-5])\b", text))
    if "last one" in text and count:
        values.append(count)
    return list(dict.fromkeys(values))
