"""Preferences for validated shopping conversations."""

import re
from decimal import Decimal
from difflib import SequenceMatcher

from app.commerce_errors import IntentError, PreferenceConflict
from app.commerce_models import (
    Change,
    Concern,
)
from app.commerce_taxonomy import FAMILIES
from app.commerce_text import (
    FOREIGN_CURRENCY,
    amounts,
    budget_overrides,
    can_clear,
    evidence_ok,
    matching_families,
    negated,
    normalized,
    product_mentions,
)
from app.search_models import ProductFilters


def apply_changes(task, plan, message):
    fields = task.filters.model_dump(mode="json")
    changed = set()
    effective = []
    # Older sessions stored one concern string. Preserve it and its original evidence.
    if task.need and not task.needs:
        task.needs = [
            Concern(
                value=task.need, evidence=task.sources.get("need", {}).get("evidence", task.need)
            )
        ]
    for change in plan.changes:
        key, value = change.field, change.value
        if (
            key == "need"
            and not evidence_ok(change.evidence, message)
            and any(
                evidence_ok(change.evidence, item.evidence)
                or evidence_ok(change.evidence, item.value)
                for item in task.needs
            )
        ):
            # Old quoted concerns are already retained; they are not fresh changes.
            continue
        if (
            key in {"domain", "family"}
            and isinstance(value, str)
            and not task.query
            and not matching_families(message)
            and re.search(SKIN_CONCERN, task.need + " " + message, re.I)
            and normalized(value.replace("_", " ")) not in normalized(message)
        ):
            # Taxonomy inferred from a concern is owned by guided discovery below.
            continue
        current = (
            getattr(task, key)
            if key in {"query", "need", "recipient"}
            else fields["attributes"].get(key.split(".", 1)[1])
            if key.startswith("attribute.")
            else fields.get(key)
        )
        # A repair may emit null for a rejected, never-applied optional attribute.
        # Ignoring that no-op cannot remove any saved requirement.
        if key.startswith("attribute.") and value is None and current is None:
            continue
        # Restating an existing fact is not a new preference or new evidence.
        # Discard these no-ops rather than attributing old facts to the latest message.
        denied = (isinstance(value, str) and negated(value, message)) or (
            key == "attribute.fragrance_free"
            and value is True
            and negated("fragrance free", message)
        )
        if (
            key == "attribute.style"
            and isinstance(value, str)
            and normalized(value) in {"silver", "gold"}
        ):
            raise IntentError(
                "Silver/gold alone is not a style. Preserve the literal phrase in query/need; use color or material filters only when explicitly specified"
            )
        if key == "attribute.size" and isinstance(value, str):
            system = re.search(r"\b(US|UK|EU)\b", message)
            if system and system.group().casefold() not in normalized(value).split():
                raise IntentError("Required sizing system was omitted")
        same_concern = (
            key == "need"
            and isinstance(value, str)
            and any(concern_covered(value, item.value) for item in task.needs)
        )
        if value is not None and (current == value or same_concern) and not denied:
            continue
        if (key in changed and key not in {"need", "unsupported"}) or not evidence_ok(
            change.evidence, message
        ):
            raise IntentError("Preference change lacks unique current-message evidence")
        changed.add(key)
        if denied:
            raise IntentError("Negated preference cannot become a positive requirement")
        # A model may explicitly encode an unknown initial product type as null.
        # This is a no-op, never permission to clear an existing product goal.
        if key == "query" and value is None and not task.query:
            continue
        if value is None:
            if not can_clear(key, change.evidence):
                raise IntentError("Constraint removal was not explicit")
        elif isinstance(value, str) and len(value.encode()) > 1000:
            raise IntentError("Extracted preference exceeds supported size")
        if key in {"domain", "family"} and isinstance(value, str):
            mentions = product_mentions(message)
            families = list(dict.fromkeys(m[2] for m in mentions))
            if len(families) == 1:
                mapped = families[0] if key == "family" else FAMILIES[families[0]][0]
                # Taxonomy codes are application-owned mappings. Repair an invented
                # code, but do not erase a conflicting category the user stated.
                if value == mapped or normalized(value.replace("_", " ")) not in normalized(
                    message
                ):
                    value = change.value = mapped
                    change.evidence = mentions[0][3]
        quote = normalized(change.evidence)
        if (
            key == "query"
            and isinstance(value, str)
            and normalized(value)
            in {
                "product",
                "products",
                "item",
                "items",
                "something",
                "stuff",
                "thing",
                "things",
                "prduct",
                "prdct",
            }
        ):
            # These words do not select a product type. Keep the concern and clarify.
            continue
        if key in {"query", "need", "recipient"}:
            if value is not None and not isinstance(value, str):
                raise IntentError("Text preference required")
            if value and not evidence_ok(value, change.evidence):
                raise IntentError("Text preference was not stated")
            if value and negated(value, message):
                raise IntentError("Negated preference cannot become a positive requirement")
            if key == "query" and value:
                proposed, supported = matching_families(value), matching_families(change.evidence)
                if not proposed or not set(proposed) & set(supported):
                    raise IntentError("Product goal was not stated by the user")
                if not task.query and len(supported) == 1:
                    family = supported[0]
                    fields.update(domain=FAMILIES[family][0], family=family)
                    for category, mapped in (("domain", FAMILIES[family][0]), ("family", family)):
                        task.sources[category] = {
                            "value": mapped,
                            "evidence": change.evidence,
                            "source": "product_type_mapping",
                        }
            if key == "need":
                if value is None:
                    if len(task.needs) > 1 and re.search(r"\b(?:except|keep|but)\b", quote):
                        raise PreferenceConflict(
                            "Please name the exact concern to remove so I can keep the others unchanged."
                        )
                    all_concerns = re.search(r"\ball\b|forget everything|start over", quote)
                    targets = [item for item in task.needs if normalized(item.value) in quote]
                    if len(task.needs) > 1 and not all_concerns and not targets:
                        raise PreferenceConflict(
                            "Which saved concern should I remove? Quote that concern, or explicitly say 'clear all concerns'."
                        )
                    task.needs = (
                        [item for item in task.needs if item not in targets]
                        if targets and not all_concerns
                        else []
                    )
                else:
                    if len(task.needs) >= 20:
                        raise PreferenceConflict(
                            "There are 20 saved concerns. Which concerns should I remove before adding another?"
                        )
                    task.needs.append(Concern(value=value, evidence=change.evidence))
                task.need = "; ".join(item.value for item in task.needs)
            else:
                setattr(task, key, value or "")
        elif key == "unsupported":
            if value is None:
                task.unsupported = []
            elif isinstance(value, str) and evidence_ok(value, message):
                if value not in task.unsupported:
                    if len(task.unsupported) >= 10:
                        raise PreferenceConflict(
                            "There are ten unsupported requirements. Which should I remove before adding another?"
                        )
                    task.unsupported.append(value)
            else:
                raise IntentError("Unsupported requirement lacks evidence")
        elif key in {"min_price", "max_price"}:
            if re.search(FOREIGN_CURRENCY, message, re.I):
                continue
            if value is not None:
                amount = (
                    re.fullmatch(
                        r"(?:USD\s*|\$\s*)?([+-]?\d+(?:\.\d{1,2})?)\s*(?:USD|dollars?|\$)?",
                        value.strip(),
                        re.I,
                    )
                    if isinstance(value, str)
                    else None
                )
                computed = budget_overrides(change.evidence).get(key)
                if not amount or (
                    Decimal(amount.group(1)) not in amounts(change.evidence)
                    and (computed is None or Decimal(amount.group(1)) != Decimal(computed))
                ):
                    raise IntentError("Budget must contain one explicitly stated USD amount")
                value = change.value = amount.group(1)
            fields[key] = value
        elif key == "domain":
            if value is not None and not isinstance(value, str):
                raise IntentError("Text domain required")
            if (
                value is not None
                and value not in {FAMILIES[name][0] for name in matching_families(change.evidence)}
                and value not in quote
            ):
                raise IntentError("Domain was not supported")
            fields[key] = value
        elif key == "family":
            if value is not None and not isinstance(value, str):
                raise IntentError("Text family required")
            if (
                value is not None
                and value not in matching_families(change.evidence)
                and value.replace("_", " ") not in quote
            ):
                raise IntentError("Category was not supported")
            fields[key] = value
        elif key == "brand":
            if value is not None and (not isinstance(value, str) or normalized(value) not in quote):
                raise IntentError("Brand was not stated")
            if value and negated(value, message):
                raise IntentError("Excluded brand cannot become a positive requirement")
            fields[key] = value
            if value is None:
                fields["exclude_brands"] = []
        else:
            name = key.split(".", 1)[1]
            if value is None:
                fields["attributes"].pop(name, None)
            else:
                alias = {"mens": ["men", "men's", "mens"], "womens": ["women", "women's", "womens"]}
                if name == "fragrance_free":
                    if type(value) is not bool or (
                        value and not re.search(r"fragrance[ -]free", quote)
                    ):
                        raise IntentError("Fragrance-free cannot be inferred from unscented")
                    if value and negated("fragrance free", message):
                        raise IntentError(
                            "Negated fragrance-free claim cannot become a positive requirement"
                        )
                    if not value and not re.search(
                        r"not fragrance[ -]free|contains fragrance|with fragrance", quote
                    ):
                        raise IntentError("Negative fragrance claim was not stated")
                elif not isinstance(value, str) or not any(
                    evidence_ok(item, quote) for item in alias.get(normalized(value), [value])
                ):
                    raise IntentError("Attribute value was not stated")
                elif negated(value, message):
                    raise IntentError("Excluded attribute cannot become a positive requirement")
                fields["attributes"][name] = value
        task.sources[key] = (
            {
                "value": task.need,
                "source": "combined_user_concerns",
                "items": [item.model_dump() for item in task.needs],
            }
            if key == "need"
            else {"value": value, "evidence": change.evidence, "source": "user"}
        )
        effective.append(change)
    plan.changes = effective
    # Deterministic numeric bounds override any LLM interpretation of the comparator.
    for key, value in budget_overrides(message).items():
        if plan.recommendation_requested is False and not any(
            change.field == key for change in plan.changes
        ):
            continue
        if re.search(FOREIGN_CURRENCY, message, re.I) or any(
            change.field == key and change.value is None for change in plan.changes
        ):
            continue
        fields[key] = value
        task.sources[key] = {
            "value": value,
            "evidence": message,
            "source": "user_numeric_comparator",
        }
    if (
        plan.recommendation_requested is not False
        and re.search(r"fragrance[ -]free", normalized(message))
        and not any(
            change.field == "attribute.fragrance_free" and change.value is None
            for change in plan.changes
        )
        and not re.search(
            r"not fragrance[ -]free|don.t (?:need|require|want)|not required|remove|without (?:that|the) (?:requirement|filter)",
            normalized(message),
        )
    ):
        fields["attributes"]["fragrance_free"] = True
        task.sources["attribute.fragrance_free"] = {
            "value": True,
            "evidence": message,
            "source": "user_explicit_claim",
        }
    if (
        fields.get("family") in {"footwear", "apparel"}
        and plan.recommendation_requested is not False
    ):
        recipient = re.search(r"\b(?:adult\s+)?(?:women|men|boys|girls|children)\b", message, re.I)
        if recipient and not negated(recipient.group(), message):
            literal = recipient.group()
            task.recipient = literal
            task.sources["recipient"] = {"value": literal, "evidence": literal, "source": "user"}
            department = {"women": "womens", "men": "mens", "boys": "boys", "girls": "girls"}.get(
                literal.lower().split()[-1]
            )
            if department and not any(
                c.field == "attribute.department" and c.value is None for c in plan.changes
            ):
                fields["attributes"]["department"] = department
                task.sources["attribute.department"] = {
                    "value": department,
                    "evidence": literal,
                    "source": "explicit_recipient_mapping",
                }
    if fields.get("min_price") is not None and fields.get("max_price") is not None:
        low, high = Decimal(fields["min_price"]), Decimal(fields["max_price"])
        if low.is_finite() and high.is_finite() and low >= 0 and high >= 0 and low > high:
            raise PreferenceConflict(
                "Your new budget conflicts with the saved price range. Which minimum or maximum should I change or remove?"
            )
    task.filters = ProductFilters.model_validate(fields)


def concern_covered(value, existing):
    value, existing = normalized(value), normalized(existing)
    if value == existing or value in existing:
        return True
    fillers = {
        "i",
        "a",
        "an",
        "the",
        "need",
        "want",
        "product",
        "prduct",
        "something",
        "that",
        "is",
        "for",
        "please",
    }
    return existing in value and not (set(value.split()) - set(existing.split()) - fillers)


def vague_preference(message):
    """Detect an unclear preference, never translate it into a catalog claim."""
    words = re.findall(r"[a-z]+", normalized(message))
    chemical = any(
        SequenceMatcher(None, word, "chemical").ratio() >= 0.8 for word in words if len(word) >= 6
    )
    return chemical or bool(
        re.search(
            r"\b(?:gentler|less harsh|not too strong|more natural|clean beauty)\b",
            normalized(message),
        )
    )


def preserve_literal_preferences(plan, message):
    for change in plan.changes:
        if (
            evidence_ok(change.evidence, message)
            and vague_preference(change.evidence)
            and (change.field.startswith("attribute.") or change.field == "query")
            and change.value is not None
            and (
                not isinstance(change.value, str) or not evidence_ok(change.value, change.evidence)
            )
        ):
            change.field, change.value = "need", change.evidence
        if (
            change.field == "need"
            and isinstance(change.value, str)
            and evidence_ok(change.evidence, message)
            and not evidence_ok(change.value, change.evidence)
            and not matching_families(change.evidence)
        ):
            # Store what was actually said. A rewritten model value is never evidence.
            change.value = change.evidence
    if (
        len(message) <= 500
        and vague_preference(message)
        and not any(change.field == "need" and change.value is None for change in plan.changes)
    ):
        if not any(
            change.field == "need"
            and isinstance(change.value, str)
            and vague_preference(change.value)
            for change in plan.changes
        ):
            plan.changes.append(Change(field="need", value=message, evidence=message))


def preserve_unextracted_concerns(state, plan, message):
    """Retain a short literal concern statement when extraction omitted a new issue."""
    if len(message) > 500 or len(message.encode("utf-8")) > 1000 or matching_families(message):
        return
    if re.search(
        r"\b(?:not|no|without|remove|forget|ignore|drop|clear|reset|compare|details)\b|don.t|do not",
        message,
        re.I,
    ):
        return
    if not re.search(
        r"\b(?:need|want|reduce|lighten\w*|brighten\w*|help|have|having|also)\b", message, re.I
    ):
        return
    active = state.active()
    covered = active.need if active else ""
    covered += " " + " ".join(
        change.value
        for change in plan.changes
        if change.field == "need"
        and isinstance(change.value, str)
        and evidence_ok(change.evidence, message)
        and evidence_ok(change.value, change.evidence)
    )
    mentions = list(re.finditer(SKIN_CONCERN, message, re.I))
    if mentions and any(normalized(match.group()) not in normalized(covered) for match in mentions):
        plan.changes.append(Change(field="need", value=message, evidence=message))
        plan.intent = "refine" if active else "clarify"


SKIN_CONCERN = (
    r"\b(?:face|facial|skin|complexion|pim(?:ple|ble)s?|acne|blemish(?:es)?|dark spots?)\b"
)


SKIN_PROFILE = r"\b(?:oily|dry|combination|sensitive|normal)\s+skin\b|\bskin\s+(?:is\s+)?(?:oily|dry|combination|sensitive|normal)\b"
