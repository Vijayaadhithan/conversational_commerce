"""Validated intent transitions and catalog-grounded conversational replies."""

from copy import deepcopy
from decimal import Decimal
import re
import unicodedata

from app.commerce_models import Change, Concern, FAMILIES, IntentPlan, ShoppingState, ShoppingTask
from app.product_search import ProductFilters, ProductSearchRequest


class IntentError(ValueError):
    pass


class PreferenceConflict(ValueError):
    pass


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def evidence_ok(quote, message):
    return bool(quote.strip()) and normalized(quote) in normalized(message)


def negated(value, message):
    text, value = normalized(message).replace("-", " "), re.escape(normalized(value).replace("-", " "))
    return bool(re.search(r"\b(?:not|no|without|avoid|exclude|except|rather than|don.t want|do not want|don.t need|do not need)\s+(?:any\s+|a\s+|an\s+)?" + value + r"\b", text)
                or re.search(value + r"(?: is)? (?:not required|not wanted|not necessary)\b", text))


ONES = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))


def number_text(text):
    text = normalized(text)
    words = {**ONES, **TENS, "one hundred": 100, "a hundred": 100}
    words.update({f"{ten} {one}": base + amount for ten, base in TENS.items() for one, amount in ONES.items() if 0 < amount < 10})
    pattern = r"\b(?:" + "|".join(re.escape(key) for key in sorted(words, key=len, reverse=True)) + r")\b"
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    return re.sub(pattern, lambda match: str(words[match.group()]), text)


def amounts(text):
    return {Decimal(value) for value in re.findall(r"(?<!\w)[+-]?\d+(?:\.\d{1,2})?(?![\w.])", number_text(text))}


FOREIGN_CURRENCY = r"(?:₹|€|£|\b(?:INR|EUR|GBP|CAD|AUD|JPY|CNY|SGD|rupees)\b|\bRs\.?\s*\d)"


def matching_families(text):
    return [name for name, (domain, terms) in FAMILIES.items()
            if any(re.search(r"\b" + re.escape(term), normalized(text)) for term in terms)]


def fill_explicit_goal(plan, message):
    """Recover a literal product type if the model omitted its query change."""
    if plan.intent not in {"search", "new_task", "switch_task"} or any(change.field == "query" and change.value for change in plan.changes):
        return
    if re.search(r"\bback\b|\breturn\b", normalized(message)): return
    families = matching_families(message)
    if len(families) != 1 or re.search(r"\b(?:not|no|without)\b|don.t (?:need|want)", normalized(message)):
        return
    terms = sorted(FAMILIES[families[0]][1], key=len, reverse=True)
    match = re.search(r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\w*", message, re.I)
    if match:
        plan.changes = [change for change in plan.changes if change.field != "query"]
        plan.changes.append(Change(field="query", value=match.group(), evidence=match.group()))


def budget_overrides(message):
    text = number_text(message)
    number = r"\$?\s*(\d+(?:\.\d{1,2})?)(?![\d.])"
    result = {}
    for pattern, key, step in [
        (r"(?:under|below|less than)\s*" + number, "max_price", Decimal("-0.01")),
        (r"(?:up to|at most|maximum(?: budget)?(?: is)?|max(?: budget)?(?: is)?|budget(?: is| of)?)\s*" + number, "max_price", Decimal(0)),
        (r"(?:at least|minimum(?: is)?)\s*" + number, "min_price", Decimal(0)),
        (r"(?:over|more than)\s*" + number, "min_price", Decimal("0.01")),
    ]:
        matches = list(re.finditer(r"\b" + pattern, text))
        if matches: result[key] = str(Decimal(matches[-1].group(1)) + step)
    match = re.search(r"between\s*" + number + r"\s*and\s*" + number, text)
    if match: result.update(min_price=match.group(1), max_price=match.group(2))
    return result


def can_clear(field, quote):
    quote = normalized(quote).replace("-", " ")
    if re.search(r"don.t (?:remove|ignore|drop|forget|clear)|do not (?:remove|ignore|drop|forget|clear)|never (?:remove|ignore|drop|forget|clear)", quote):
        return False
    aliases = {"query": r"search|product|start over", "need": r"need|skin|hair|concern",
        "recipient": r"recipient|who|person", "domain": r"category|domain", "family": r"category|type|family",
        "brand": r"brand", "min_price": r"budget|price|minimum", "max_price": r"budget|price|maximum",
        "unsupported": r"requirement|constraint|that|stock|delivery|allergy|safe|currency"}
    target = aliases.get(field, re.escape(field.removeprefix("attribute.").replace("_", " ")))
    return bool(re.search(target, quote) and re.search(r"any|remove|ignore|drop|forget|no longer|not required|not necessary|no preference|doesn.t matter|don.t (?:care|need|require)|without|no budget", quote))


def direct_plan(message):
    text = normalized(message).rstrip(".!?")
    action = None
    if re.fullmatch(r"(?:please )?(?:start over|reset|forget my preferences|new search)", text): action = "reset"
    elif re.fullmatch(r"(?:please )?(?:show (?:me )?)?(?:more|more options|other options|show alternatives)", text): action = "more"
    elif re.fullmatch(r"(?:please )?(?:show (?:me )?)?(?:cheaper|cheaper options|less expensive options)", text): action = "cheaper"
    elif re.fullmatch(r"(?:please )?(?:show (?:me )?)?(?:another brand|a different brand|different brands)", text): action = "different_brand"
    elif text in {"hi", "hello", "hey", "thanks", "thank you"}: action = "smalltalk"
    elif text in {"what are my preferences", "what are my requirements", "show my preferences"}: action = "preferences"
    elif re.fullmatch(r"compare (?:the )?(?:first two|first and second|1 and 2)(?: products| options| ones)?", text): action = "compare"
    elif re.fullmatch(r"(?:show |tell me |give me )?(?:more )?(?:details|information)(?: about| on| for)? (?:the )?(?:first|second|third|fourth|fifth|[1-5])(?: one| product| option)?", text): action = "details"
    return IntentPlan(intent=action, evidence=message) if action else None


def reference_positions(message, count):
    text = normalized(message)
    values = []
    for word, end in (("two", 2), ("three", 3), ("four", 4), ("five", 5)):
        if "first " + word in text: values.extend(range(1, end + 1))
    for word, position in [("first", 1), ("second", 2), ("third", 3), ("fourth", 4), ("fifth", 5)]:
        if re.search(r"\b" + word + r"\b", text): values.append(position)
    values.extend(int(value) for value in re.findall(r"\b([1-5])\b", text))
    if "last one" in text and count: values.append(count)
    return list(dict.fromkeys(values))


def apply_changes(task, plan, message):
    fields = task.filters.model_dump(mode="json")
    changed = set()
    effective = []
    # Older sessions stored one concern string. Preserve it and its original evidence.
    if task.need and not task.needs:
        task.needs = [Concern(value=task.need, evidence=task.sources.get("need", {}).get("evidence", task.need))]
    for change in plan.changes:
        key, value = change.field, change.value
        if key == "need" and not evidence_ok(change.evidence, message) and any(
                evidence_ok(change.evidence, item.evidence) or evidence_ok(change.evidence, item.value) for item in task.needs):
            # Old quoted concerns are already retained; they are not fresh changes.
            continue
        if (key in {"domain", "family"} and isinstance(value, str) and not task.query
                and not matching_families(message) and re.search(SKIN_CONCERN, task.need + " " + message, re.I)
                and normalized(value.replace("_", " ")) not in normalized(message)):
            # Taxonomy inferred from a concern is owned by guided discovery below.
            continue
        current = (getattr(task, key) if key in {"query", "need", "recipient"}
                   else fields["attributes"].get(key.split(".", 1)[1]) if key.startswith("attribute.")
                   else fields.get(key))
        # Restating an existing fact is not a new preference or new evidence.
        # Discard these no-ops rather than attributing old facts to the latest message.
        denied = (isinstance(value, str) and negated(value, message)) or (key == "attribute.fragrance_free" and value is True and negated("fragrance free", message))
        if key == "attribute.size" and isinstance(value, str):
            system = re.search(r"\b(US|UK|EU)\b", message)
            if system and system.group().casefold() not in normalized(value).split():
                raise IntentError("Required sizing system was omitted")
        same_concern = key == "need" and isinstance(value, str) and any(normalized(value) == normalized(item.value) for item in task.needs)
        if value is not None and (current == value or same_concern) and not denied:
            continue
        if (key in changed and key not in {"need", "unsupported"}) or not evidence_ok(change.evidence, message):
            raise IntentError("Preference change lacks unique current-message evidence")
        changed.add(key)
        if denied: raise IntentError("Negated preference cannot become a positive requirement")
        # A model may explicitly encode an unknown initial product type as null.
        # This is a no-op, never permission to clear an existing product goal.
        if key == "query" and value is None and not task.query:
            continue
        if value is None:
            if not can_clear(key, change.evidence): raise IntentError("Constraint removal was not explicit")
        elif isinstance(value, str) and len(value.encode()) > 1000:
            raise IntentError("Extracted preference exceeds supported size")
        quote = normalized(change.evidence)
        if key == "query" and isinstance(value, str) and normalized(value) in {"product", "products", "item", "items", "something", "stuff", "thing", "things", "prduct", "prdct"}:
            # These words do not select a product type. Keep the concern and clarify.
            continue
        if key in {"query", "need", "recipient"}:
            if value is not None and not isinstance(value, str): raise IntentError("Text preference required")
            if value and not evidence_ok(value, change.evidence): raise IntentError("Text preference was not stated")
            if value and negated(value, message): raise IntentError("Negated preference cannot become a positive requirement")
            if key == "query" and value:
                proposed, supported = matching_families(value), matching_families(change.evidence)
                if not proposed or not set(proposed) & set(supported):
                    raise IntentError("Product goal was not stated by the user")
                if not task.query and len(supported) == 1:
                    family = supported[0]
                    fields.update(domain=FAMILIES[family][0], family=family)
                    for category, mapped in (("domain", FAMILIES[family][0]), ("family", family)):
                        task.sources[category] = {"value": mapped, "evidence": change.evidence, "source": "product_type_mapping"}
            if key == "need":
                if value is None:
                    if len(task.needs) > 1 and re.search(r"\b(?:except|keep|but)\b", quote):
                        raise PreferenceConflict("Please name the exact concern to remove so I can keep the others unchanged.")
                    all_concerns = re.search(r"\ball\b|forget everything|start over", quote)
                    targets = [item for item in task.needs if normalized(item.value) in quote]
                    if len(task.needs) > 1 and not all_concerns and not targets:
                        raise PreferenceConflict("Which saved concern should I remove? Quote that concern, or explicitly say 'clear all concerns'.")
                    task.needs = [item for item in task.needs if item not in targets] if targets and not all_concerns else []
                else:
                    if len(task.needs) >= 20: raise PreferenceConflict("There are 20 saved concerns. Which concerns should I remove before adding another?")
                    task.needs.append(Concern(value=value, evidence=change.evidence))
                task.need = "; ".join(item.value for item in task.needs)
            else:
                setattr(task, key, value or "")
        elif key == "unsupported":
            if value is None: task.unsupported = []
            elif isinstance(value, str) and evidence_ok(value, message):
                if value not in task.unsupported:
                    if len(task.unsupported) >= 10: raise PreferenceConflict("There are ten unsupported requirements. Which should I remove before adding another?")
                    task.unsupported.append(value)
            else: raise IntentError("Unsupported requirement lacks evidence")
        elif key in {"min_price", "max_price"}:
            if re.search(FOREIGN_CURRENCY, message, re.I): continue
            if value is not None and (not isinstance(value, str) or Decimal(value) not in amounts(change.evidence)):
                raise IntentError("Budget amount was not stated")
            fields[key] = value
        elif key == "domain":
            if value is not None and not isinstance(value, str): raise IntentError("Text domain required")
            if value is not None and value not in {FAMILIES[name][0] for name in matching_families(change.evidence)} and value not in quote:
                raise IntentError("Domain was not supported")
            fields[key] = value
        elif key == "family":
            if value is not None and not isinstance(value, str): raise IntentError("Text family required")
            if value is not None and value not in matching_families(change.evidence) and value.replace("_", " ") not in quote:
                raise IntentError("Category was not supported")
            fields[key] = value
        elif key == "brand":
            if value is not None and (not isinstance(value, str) or normalized(value) not in quote): raise IntentError("Brand was not stated")
            if value and negated(value, message): raise IntentError("Excluded brand cannot become a positive requirement")
            fields[key] = value
            if value is None: fields["exclude_brands"] = []
        else:
            name = key.split(".", 1)[1]
            if value is None: fields["attributes"].pop(name, None)
            else:
                alias = {"mens": ["men", "men's", "mens"], "womens": ["women", "women's", "womens"]}
                if name == "fragrance_free":
                    if type(value) is not bool or (value and not re.search(r"fragrance[ -]free", quote)):
                        raise IntentError("Fragrance-free cannot be inferred from unscented")
                    if value and negated("fragrance free", message):
                        raise IntentError("Negated fragrance-free claim cannot become a positive requirement")
                    if not value and not re.search(r"not fragrance[ -]free|contains fragrance|with fragrance", quote):
                        raise IntentError("Negative fragrance claim was not stated")
                elif not isinstance(value, str) or not any(evidence_ok(item, quote) for item in alias.get(normalized(value), [value])):
                    raise IntentError("Attribute value was not stated")
                elif negated(value, message):
                    raise IntentError("Excluded attribute cannot become a positive requirement")
                fields["attributes"][name] = value
        task.sources[key] = ({"value": task.need, "source": "combined_user_concerns", "items": [item.model_dump() for item in task.needs]}
                             if key == "need" else {"value": value, "evidence": change.evidence, "source": "user"})
        effective.append(change)
    plan.changes = effective
    # Deterministic numeric bounds override any LLM interpretation of the comparator.
    for key, value in budget_overrides(message).items():
        if re.search(FOREIGN_CURRENCY, message, re.I) or any(change.field == key and change.value is None for change in plan.changes): continue
        fields[key] = value
        task.sources[key] = {"value": value, "evidence": message, "source": "user_numeric_comparator"}
    if re.search(r"fragrance[ -]free", normalized(message)) and not any(change.field == "attribute.fragrance_free" and change.value is None for change in plan.changes) and not re.search(r"not fragrance[ -]free|don.t (?:need|require|want)|not required|remove|without (?:that|the) (?:requirement|filter)", normalized(message)):
        fields["attributes"]["fragrance_free"] = True
        task.sources["attribute.fragrance_free"] = {"value": True, "evidence": message, "source": "user_explicit_claim"}
    if fields.get("min_price") is not None and fields.get("max_price") is not None:
        low, high = Decimal(fields["min_price"]), Decimal(fields["max_price"])
        if low.is_finite() and high.is_finite() and low >= 0 and high >= 0 and low > high:
            raise PreferenceConflict("Your new budget conflicts with the saved price range. Which minimum or maximum should I change or remove?")
    task.filters = ProductFilters.model_validate(fields)


def question(state, text, kind="general"):
    state.pending_question, state.pending_kind = text, kind
    return {"action": "clarify", "reply": text, "products": [], "search": None}


SKIN_CONCERN = r"\b(?:face|facial|skin|complexion|pim(?:ple|ble)s?|acne|blemish(?:es)?|dark spots?)\b"
SKIN_PROFILE = r"\b(?:oily|dry|combination|sensitive|normal)\s+skin\b|\bskin\s+(?:is\s+)?(?:oily|dry|combination|sensitive|normal)\b"


def skin_discovery(state, task, message, old_pending):
    """A concern can guide broad discovery without inventing a specific product type."""
    if task.query or not re.search(SKIN_CONCERN, task.need, re.I): return None
    requested = bool(re.search(r"\b(?:show|suggest|recommend|find)\b|\b(?:not sure|don.t know|no idea)\b", message, re.I)) and not re.search(r"(?:don.t|do not|never)\s+(?:show|suggest|recommend|find)", message, re.I)
    answered = bool(old_pending == "skin_profile" and (re.search(SKIN_PROFILE, task.need, re.I) or re.search(r"\b(?:oily|dry|combination|sensitive|normal)\b", message, re.I)))
    if old_pending == "skin_profile" and (requested or answered):
        task.query = "skin care"
        task.filters.domain, task.filters.family = "beauty", "skin_care"
        task.sources["query"] = {"value": task.query, "source": "concern_guided_discovery", "evidence": message,
                                 "basis": task.need, "specific_product_type_selected": False}
        for name, value in (("domain", "beauty"), ("family", "skin_care")):
            task.sources[name] = {"value": value, "source": "concern_guided_discovery", "basis": task.need}
        return None
    missing = "What is your budget in USD?" if re.search(SKIN_PROFILE, task.need, re.I) else "What is your skin type (oily, dry, combination, sensitive, or unsure), and your budget in USD?"
    return question(state, f"I've saved your concerns: {task.need}. {missing} You can also say 'show options' to browse skin-care listings for these concerns.", "skin_profile")


def preserve_unextracted_concerns(state, plan, message):
    """Retain a short literal concern statement when extraction omitted a new issue."""
    if len(message) > 500 or len(message.encode("utf-8")) > 1000 or matching_families(message): return
    if re.search(r"\b(?:not|no|without|remove|forget|ignore|drop|clear|reset|compare|details)\b|don.t|do not", message, re.I): return
    if not re.search(r"\b(?:need|want|reduce|lighten\w*|brighten\w*|help|have|having|also)\b", message, re.I): return
    active = state.active()
    covered = active.need if active else ""
    covered += " " + " ".join(change.value for change in plan.changes if change.field == "need"
        and isinstance(change.value, str) and evidence_ok(change.evidence, message) and evidence_ok(change.value, change.evidence))
    mentions = list(re.finditer(SKIN_CONCERN, message, re.I))
    if mentions and any(normalized(match.group()) not in normalized(covered) for match in mentions):
        plan.changes.append(Change(field="need", value=message, evidence=message))
        plan.intent = "refine" if active else "clarify"


def card_reply(products, compare=False):
    lines = []
    for index, row in enumerate(products, 1):
        lines.append(f"{row.get('display_position', index)}. {row['title']} — ${row['price']['amount']} USD (historical).")
        for reason in row.get("why_recommended", []): lines.append("   " + reason)
    if compare and len(products) > 1:
        cheapest = min(products, key=lambda row: Decimal(row["price"]["amount"]))
        highest = max(Decimal(row["price"]["amount"]) for row in products)
        difference = highest - Decimal(cheapest["price"]["amount"])
        lines.append(f"Lowest historical price: {cheapest['title']}; difference from the highest: ${difference:.2f} USD.")
        lines.append("Missing attributes remain unknown; the listings do not establish which product will work better for you.")
    return "\n".join(lines)


def explain_products(rows, task):
    products = deepcopy(rows)
    for position, row in enumerate(products, 1):
        reasons = []
        if task.filters.max_price is not None:
            reasons.append(f"Its listed historical price is within your ${task.filters.max_price:.2f} maximum.")
        for name in task.filters.attributes:
            claim = row["attributes"].get(name)
            if claim:
                reasons.append(f"The listing explicitly states {name.replace('_', ' ')}: {claim['value']} ({claim['field']}).")
        snippets = row.get("listing_evidence", [])
        concern_snippet = next((item for item in snippets if task.need and normalized(task.need) in normalized(item["text"])), None)
        snippet = concern_snippet or (snippets[0] if snippets else None)
        if snippet:
            reasons.append(f"Related listing text: “{snippet['text']}” ({snippet['field']}).")
        else: reasons.append("Retrieved for similarity to your product request; the listing does not provide an exact matching excerpt.")
        if task.need and not concern_snippet:
            reasons.append(f"The returned excerpts do not explicitly confirm suitability for your stated concern: {task.need}.")
        row.update(display_position=position, why_recommended=reasons)
    return products


def handle_turn(saved, plan, message, search):
    state = ShoppingState.model_validate(saved or {}).model_copy(deep=True)
    if not evidence_ok(plan.evidence, message): raise IntentError("Intent lacks current-message evidence")
    # A facial concern can be mislabelled as a product goal by the model.
    # Keep that literal concern without pretending a concrete product type was chosen.
    for change in plan.changes:
        if (change.field == "query" and isinstance(change.value, str) and not matching_families(change.evidence)
                and re.search(SKIN_CONCERN, change.evidence, re.I) and evidence_ok(change.evidence, message)):
            literal = change.value if evidence_ok(change.value, change.evidence) and not matching_families(change.value) else change.evidence
            change.field, change.value = "need", literal
    preserve_unextracted_concerns(state, plan, message)
    if plan.intent == "reset" and not re.search(r"start over|reset|forget|new search", normalized(message)):
        existing = state.active()
        repeated = existing and any(evidence_ok(item.value, message) for item in existing.needs)
        if existing and not existing.needs and existing.need:
            repeated = evidence_ok(existing.need, message)
        current_concerns = any(change.field == "need" and isinstance(change.value, str)
                               and evidence_ok(change.value, message) for change in plan.changes)
        if repeated or current_concerns:
            plan.intent = "refine" if existing else "clarify"
    fill_explicit_goal(plan, message)
    active = state.active()
    goals = [change for change in plan.changes if change.field == "query" and isinstance(change.value, str)]
    returning = bool(re.search(r"\bback\b|\breturn\b", normalized(message)))
    if returning:
        targets = [task for task in state.tasks if task.query and normalized(task.query) in normalized(message)]
        if len(targets) == 1:
            plan.intent, plan.task_id = "switch_task", targets[0].id
    if goals and not returning and plan.intent not in {"reset", "smalltalk", "preferences", "details", "compare", "similar"}:
        families = matching_families(goals[0].value)
        if len(families) > 1:
            state.turns += 1
            return state, question(state, "Which product should we start with? We can keep separate shopping tasks for the others.", "multiple")
        if not active or not active.query:
            plan.intent = "search"
        elif len(families) == 1 and active.filters.family and families[0] != active.filters.family:
            plan.intent = "new_task"
        elif plan.intent == "switch_task":
            plan.intent = "refine"
    old_pending = state.pending_kind
    state.pending_question, state.pending_kind = "", ""
    state.turns += 1
    if plan.intent in {"details", "compare"} and plan.changes:
        return state, question(state, "Would you like to inspect the previously shown products, or search with changed requirements first?", "reference")
    if plan.intent == "reset":
        reset = direct_plan(message)
        explicit = re.search(r"start over|reset all|forget everything|forget (?:all )?my preferences|new search", normalized(message))
        if (not (reset and reset.intent == "reset") and not explicit) or re.search(r"don.t|do not|never", normalized(message)):
            raise IntentError("A full reset was not requested")
        state = ShoppingState(turns=state.turns)
        result = question(state, "What beauty or fashion product are you looking for, and what should it help with?", "product_type")
        return state, result
    if plan.intent == "smalltalk":
        return state, {"action": "smalltalk", "reply": "I'm here to help with beauty and fashion products. Tell me what you need, or what you'd like to change.", "products": [], "search": None}
    if plan.intent == "switch_task":
        target = next((task for task in state.tasks if task.id == plan.task_id), None)
        terms = set(re.findall(r"\w+", normalized(target.query))) - {"a", "an", "the", "for", "me"} if target else set()
        if not target or not (terms & set(re.findall(r"\w+", normalized(message))) or re.search(r"\btask\s*" + str(plan.task_id) + r"\b", normalized(message))):
            return state, question(state, "Which saved shopping task would you like to return to?", "reference")
        state.active_task_id = plan.task_id
    if plan.intent == "new_task" and state.active():
        goals = [change for change in plan.changes if change.field == "query" and isinstance(change.value, str)]
        if not state.active().query and not goals:
            plan.intent = "refine"
        elif goals and matching_families(goals[0].value) == matching_families(state.active().query) and not re.search(r"new|another|also|now|instead|different|switch", normalized(message)):
            plan.intent = "refine"
    if not state.active() or plan.intent == "new_task":
        if len(state.tasks) >= 5:
            return state, question(state, "There are five saved shopping tasks. Return to one of them, or explicitly start over.")
        if plan.intent == "new_task" and state.tasks and not any(change.field == "query" and change.value for change in plan.changes):
            return state, question(state, "Which new product type would you like to look for?", "product_type")
        task = ShoppingTask(id=len(state.tasks) + 1)
        state.tasks.append(task)
        state.active_task_id = task.id
    task = state.active()
    personal_skin_context = old_pending == "skin_profile" or bool(re.search(SKIN_PROFILE, message, re.I))
    if personal_skin_context and not re.search(r"\b(?:only|must|require|required)\b", message, re.I):
        for change in plan.changes:
            if change.field == "attribute.skin_type" and isinstance(change.value, str) and evidence_ok(change.value, change.evidence):
                change.field, change.value = "need", change.evidence
        profile = re.search(r"\b(?:oily|dry|combination|sensitive|normal)(?:\s+skin)?\b", message, re.I) if old_pending == "skin_profile" else re.search(SKIN_PROFILE, message, re.I)
        if profile and not negated(profile.group(), message) and not any(change.field == "need" and isinstance(change.value, str) and profile.group().casefold() in change.value.casefold() for change in plan.changes):
            plan.changes.append(Change(field="need", value=profile.group(), evidence=profile.group()))
    proposed = task.model_copy(deep=True)
    try:
        apply_changes(proposed, plan, message)
    except PreferenceConflict as exc:
        return state, question(state, str(exc), "budget_conflict")
    state.tasks[state.tasks.index(task)] = proposed
    task = proposed
    # Requirements not represented by this historical catalog never become implicit matches.
    unsupported_patterns = [FOREIGN_CURRENCY,
        r"\bin stock\b|deliver(?:ed|y)? by|arrive by|current price|live price",
        r"\ballerg(?:y|ic)|pregnancy.safe|safe during pregnancy|treat (?:eczema|psoriasis)"]
    for pattern in unsupported_patterns:
        match = re.search(pattern, message, re.I)
        if match and match.group() not in task.unsupported: task.unsupported.append(match.group())
    if old_pending == "unsupported" and re.fullmatch(r"yes(?: please)?|okay|ok|proceed without (?:that|those)(?: requirements)?", normalized(message).rstrip(".!")):
        task.sources["unsupported"] = {"value": [], "evidence": message, "source": "explicit_answer_to_removal_question"}
        task.unsupported = []
    if task.unsupported:
        quoted = ", ".join(task.unsupported)
        return state, question(state, f"This catalog cannot verify the requirement(s): {quoted}. It contains historical USD listing claims. Would you like to continue without those unverified requirements?", "unsupported")
    if plan.question_kind == "multiple" and not matching_families(message):
        plan.question_kind = "skin_profile" if re.search(SKIN_CONCERN, task.need, re.I) else "product_type"
    if plan.intent == "clarify" and plan.question_kind == "multiple":
        return state, question(state, "Which product should we start with? We can keep separate shopping tasks for the others.", "multiple")
    if plan.intent == "preferences":
        return state, {"action": "preferences", "reply": f"Current goal: {task.query or 'not specified'}. Concern: {task.need or 'not specified'}. Saved requirements: {task.filters.model_dump_json(exclude_none=True)}", "products": [], "search": None}
    if plan.intent in {"details", "compare", "similar"}:
        positions = reference_positions(message, len(task.shown))
        if not positions and len(task.shown) == 1: positions = [1]
        literal_ids = re.findall(r"amazon:[A-Z0-9]{10}", message)
        if literal_ids:
            positions = [index + 1 for index, row in enumerate(task.shown) if row["product_id"] in literal_ids]
        if not positions or any(not 1 <= position <= len(task.shown) for position in positions) or (plan.intent == "compare" and len(positions) < 2):
            return state, question(state, "Which product number(s) from the latest displayed list do you mean?", "reference")
        selected = [task.shown[position - 1] for position in positions]
        if plan.intent != "similar":
            return state, {"action": plan.intent, "reply": card_reply(selected, compare=plan.intent == "compare"),
                           "products": selected, "search": task.last_search}
        if len(selected) != 1: return state, question(state, "Which single product should I use for similar options?", "reference")
        query = selected[0]["title"]
    else:
        query = " ".join(part for part in (task.query, task.need) if part)
    if not task.query:
        guided = skin_discovery(state, task, message, old_pending)
        if guided: return state, guided
        query = " ".join(part for part in (task.query, task.need) if part)
    if not task.query:
        return state, question(state, "What type of product are you looking for—for example a moisturizer, cleanser, shirt, or bag?", "product_type")
    if task.filters.family in {"footwear", "apparel"} and not any(name in task.filters.attributes for name in ("department", "age_range")):
        return state, question(state, "Who are these for (for example adult men, adult women, or children)? Please mention any required size and sizing system too; only explicit listing claims can be matched.", "recipient")
    filters = task.filters.model_copy(deep=True)
    if plan.intent == "cheaper":
        if not task.shown: return state, question(state, "Which displayed product or price should the cheaper options be compared with?", "reference")
        if not re.search(r"cheap|less expensive|lower (?:price|cost)", normalized(message)): raise IntentError("Relative budget change lacks evidence")
        reference = min(task.shown, key=lambda row: Decimal(row["price"]["amount"]))
        maximum = Decimal(reference["price"]["amount"]) - Decimal("0.01")
        if filters.max_price is not None: maximum = min(maximum, filters.max_price)
        if maximum < 0 or (filters.min_price is not None and maximum < filters.min_price):
            return state, question(state, "A cheaper maximum would conflict with your saved minimum price. Which price requirement should change?")
        task.filters.max_price = maximum
        filters.max_price = maximum
        task.sources["max_price"] = {"value": str(maximum), "source": "computed_from_displayed_price", "product_id": reference["product_id"], "evidence": message}
    if plan.intent == "different_brand":
        if not re.search(r"another brand|different brand|other brand", normalized(message)):
            raise IntentError("Brand exclusion was not requested")
        brands = {row["brand"]["value"] for row in task.shown if row.get("brand")}
        if not brands: return state, question(state, "The displayed listings have no explicit brand metadata. Which brand should I avoid?", "reference")
        filters.brand = None
        task.filters.brand = None
        exclusions = sorted(set(filters.exclude_brands) | brands)
        if len(exclusions) > 10:
            return state, question(state, "There are already too many excluded brands. Explicitly clear brand preferences before excluding more.")
        filters.exclude_brands = exclusions
        task.filters.exclude_brands = filters.exclude_brands
        task.sources["exclude_brands"] = {"value": filters.exclude_brands, "source": "explicit_other_brand_request", "evidence": message}
    if plan.intent == "more":
        if len(task.seen_ids) >= 100:
            return state, question(state, "You have seen 100 products in this task. Start a new task or explicitly reset it to allow a fresh result set.")
        filters.exclude_product_ids = list(dict.fromkeys([*filters.exclude_product_ids, *task.seen_ids]))
    if plan.intent == "similar": filters.exclude_product_ids = list(dict.fromkeys([*filters.exclude_product_ids, selected[0]["product_id"]]))
    if len(query.encode("utf-8")) > 1000:
        return state, question(state, "Your saved request is too long to search in one query. Which concerns should I remove or replace to focus this search?", "query_size")
    request = ProductSearchRequest(query=query, filters=ProductFilters.model_validate(filters.model_dump()), limit=min(5, 100 - len(task.seen_ids)) if plan.intent == "more" else 5)
    response = search(request)
    products = explain_products(response["results"], task)
    new_ids = [row["product_id"] for row in products if row["product_id"] not in task.seen_ids]
    if len(task.seen_ids) + len(new_ids) > 100:
        return state, question(state, "This task's 100-product history is full. Start a new task or explicitly reset it before showing more products.")
    task.seen_ids.extend(new_ids)
    search_metadata = {key: value for key, value in response.items() if key != "results"}
    if products:
        task.shown = products
        task.last_search = search_metadata
    if not products:
        reply = "I found no products matching all your saved requirements. I kept those requirements unchanged. You can explicitly change a budget, brand, or attribute to try again."
    else:
        reply = f"For your request: {task.query}" + (f"; concern: {task.need}" if task.need else "") + ".\n" + card_reply(products)
        reply += "\nThese are historical listing claims, not verified outcomes or current stock. Tell me what to change, ask for more options, or refer to a product number."
    return state, {"action": "recommend", "reply": reply, "products": products, "search": search_metadata}
