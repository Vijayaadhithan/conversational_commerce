"""Literal recipient aliases shared by extraction and preference validation."""

import re

from app.commerce_text import negated, normalized

DEPARTMENT_ALIASES = {
    "mens": ("man", "men", "man's", "men's", "mens", "male", "males"),
    "womens": ("woman", "women", "woman's", "women's", "womens", "female", "females"),
    "boys": ("boy", "boys", "boy's", "boys'"),
    "girls": ("girl", "girls", "girl's", "girls'"),
}
RECIPIENT_PATTERN = re.compile(
    r"\b(?:adult\s+)?(?:"
    + "|".join(
        re.escape(alias)
        for alias in sorted(
            {alias for aliases in DEPARTMENT_ALIASES.values() for alias in aliases},
            key=len,
            reverse=True,
        )
    )
    + r"|child|children|kid|kids)(?![-\w])",
    re.I,
)


def recipient_evidence(message):
    """Return one unambiguous literal recipient; never infer from negation or a list."""
    matches = list(RECIPIENT_PATTERN.finditer(message))
    if len(matches) != 1:
        return None
    literal = matches[0].group()
    alias = re.sub(r"^adult\s+", "", normalized(literal))
    if negated(literal, message) or negated(alias, message):
        return None
    department = next(
        (key for key, aliases in DEPARTMENT_ALIASES.items() if alias in aliases), None
    )
    return (literal, department) if department else None
