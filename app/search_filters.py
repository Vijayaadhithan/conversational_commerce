"""Search filters."""

import re


def filter_sql(filters):
    clauses, values = [], []
    for field in ("domain", "family", "brand"):
        value = getattr(filters, field)
        if value is not None:
            clauses.append("lower(p.brand)=lower(%s)" if field == "brand" else f"p.{field}=%s")
            values.append(value)
    for field, op in (("min_price", ">="), ("max_price", "<=")):
        value = getattr(filters, field)
        if value is not None:
            clauses.append(f"p.historical_price {op} %s")
            values.append(value)
    for key, value in sorted(filters.attributes.items()):
        if key == "material":
            clauses.append("NOT coalesce((p.document #> '{quality,warnings}') ? %s, false)")
            values.append("conflicting_material_composition")
        if isinstance(value, bool):
            clauses.append("p.attributes #> ARRAY[%s,'value'] = %s::jsonb")
            values.extend((key, "true" if value else "false"))
        elif key == "department" and re.sub(r"[^a-z]", "", str(value).lower()) in {
            "men",
            "mens",
            "women",
            "womens",
            "boys",
            "girls",
        }:
            canonical = re.sub(r"[^a-z]", "", str(value).lower()).removesuffix("s")
            clauses.append(
                "regexp_replace(lower(p.attributes #>> ARRAY[%s,'value']), '[^a-z]', '', 'g') = ANY(%s)"
            )
            values.extend((key, [canonical, canonical + "s"]))
        else:
            clauses.append("lower(p.attributes #>> ARRAY[%s,'value'])=lower(%s)")
            values.extend((key, value))
    if filters.exclude_product_ids:
        clauses.append("NOT (p.product_id=ANY(%s))")
        values.append(filters.exclude_product_ids)
    if filters.exclude_brands:
        clauses.append("p.brand IS NOT NULL AND NOT (lower(p.brand)=ANY(%s))")
        values.append([brand.lower() for brand in filters.exclude_brands])
    return (" AND " + " AND ".join(clauses)) if clauses else "", values


def product_goal_sql(goal):
    """Conservative literal product identity gates; unknown types retain normal retrieval.

    Title/type wording is required, with description/features allowed for facial
    moisturizer aliases. This is textual listing support, not suitability inference.
    """
    rules = [
        (r"\brazor blades?\b", r"\m(blades?|cartridges?|refills?)\M"),
        (r"\blipsticks?\b", r"\mlipsticks?\M"),
        (r"\b(?:handbag|bag|crossbody|purse)s?\b", r"\m(handbags?|bags?|crossbody|purses?)\M"),
        (r"\bshampoo\b", r"\mshampoos?\M"),
        (r"\bconditioner\b", r"\mconditioners?\M"),
        (r"\bmoisturi\w*|\bface cream\b", r"\m(moisturi[sz][a-z]*|emulsions?|creams?)\M"),
        (r"\bcleanser\b", r"\mcleansers?\M"),
        (r"\bshirts?\b", r"\mshirts?\M"),
        (r"\b(?:shoes?|sneakers?)\b", r"\m(shoes?|sneakers?)\M"),
        (r"\bnecklaces?\b", r"\mnecklaces?\M"),
        (r"\bserum\b", r"\mserums?\M"),
        (r"\bsunscreen\b", r"\msunscreens?\M"),
    ]
    identity = "concat_ws(' ', p.document->>'title', p.attributes #>> '{product_type,value}')"
    clauses, values = [], []
    for trigger, pattern in rules:
        if re.search(trigger, goal, re.I):
            clauses.append(identity + " ~* %s")
            values.append(pattern)
            break
    full_text = "concat_ws(' ', p.document->>'title', p.document #>> '{content,features}', p.document #>> '{content,description}')"
    # Product names can describe a container/accessory or a different use entirely.
    if re.search(r"\blipsticks?\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(empty|containers?|holders?|organizers?|brooch|lapel|keychain|keyring)\M")
    if re.search(r"\brazor blades?\b", goal, re.I):
        clauses.append(full_text + " !~* %s")
        values.append(r"\m(scrapers?|scraping|paint|decals?|utility knife|box cutter)\M")
        clauses.append(full_text + " ~* %s")
        values.append(r"\m(shav[a-z]*|beards?|barbers?|safety razors?)\M")
        clauses.append("((p.document->>'title') !~* %s OR (p.document->>'title') ~* %s)")
        values.extend([r"\m(disposable|shavers?)\M", r"\m(refills?|replacement|cartridges?)\M"])
    if re.search(r"\bmoisturi\w*|\bface cream\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(masks?|peels?|cleansers?)\M")
    if re.search(r"\b(?:shoes?|sneakers?)\b", goal, re.I):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(
            r"\m(charms?|keychain|key chain|zipper pull|cleaning|cleaner|stain remover|insoles?|laces?|socks?|shorts?|pants?|leggings?)\M"
        )
        if re.search(r"\brunning\b", goal, re.I):
            clauses.append("(p.document->>'title') ~* %s")
            values.append(r"\mrunning\M")
    if re.search(r"\b(?:handbag|purse|crossbody)s?\b", goal, re.I) and not re.search(
        r"\b(?:cosmetic|makeup|toiletry)\b", goal, re.I
    ):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(
            r"\m(organizers?|inserts?|liners?|cosmetic|makeup|toiletry|keychain|charms?)\M"
        )
    if re.search(r"\bcrossbody\b", goal, re.I):
        clauses.append("(p.document->>'title') ~* %s")
        values.append(r"\mcross[ -]?body\M")
    if re.search(r"\bnecklaces?\b", goal, re.I) and not re.search(
        r"\b(?:holder|organizer|display|extender|clasp)\b", goal, re.I
    ):
        clauses.append("(p.document->>'title') !~* %s")
        values.append(r"\m(holders?|organizers?|display|extenders?|clasps?|jewelry box)\M")
    # A selected shade or title must support an explicitly named color. Generic
    # marketing about other shades elsewhere in a listing is not selected-variant evidence.
    colors = r"red|pink|blue|black|white|green|purple|orange|yellow|brown|silver|gold"
    requested_colors = set(re.findall(r"\b(?:" + colors + r")\b", goal.lower()))
    if clauses and len(requested_colors) == 1:
        color = requested_colors.pop()
        clauses.append(
            "concat_ws(' ', p.document->>'title', p.attributes #>> '{color,value}', p.attributes #>> '{material,value}') ~* %s"
        )
        values.append(r"\m" + color + r"\M")
        clauses.append(
            "(coalesce(p.attributes #>> '{color,value}', '') !~* %s OR (p.attributes #>> '{color,value}') ~* %s)"
        )
        values.extend([r"\m(" + colors + r")\M", r"\m" + color + r"\M"])
    if re.search(r"\bcotton\b", goal, re.I):
        clauses.append(full_text + " ~* %s AND " + full_text + " !~* %s")
        values.extend([r"\mcotton\M", r"\m(no|without|not) cotton\M"])
    if re.search(r"\bpockets?\b", goal, re.I):
        clauses.append(full_text + " ~* %s AND " + full_text + " !~* %s")
        values.extend([r"\mpockets?\M", r"\m(no|without|fake|faux) (chest |side )?pockets?\M"])
    if clauses and re.search(r"\bface|\bfacial", goal, re.I):
        clauses.append(
            "concat_ws(' ', p.document->>'title', p.document #>> '{content,features}', p.document #>> '{content,description}') ~* %s"
        )
        values.append(r"\m(face|facial)\M")
    return (" AND " + " AND ".join(clauses)) if clauses else "", values
