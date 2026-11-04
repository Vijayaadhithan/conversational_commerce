"""Small, authenticated catalog overview for the local shopping frontend."""

import copy
import threading
import time
from collections import OrderedDict

import psycopg

from app.product_search import pin_search_build
from app.storefronts import FAMILY_STOREFRONT, STOREFRONTS

_cache = OrderedDict()
_cache_lock = threading.Lock()


def catalog_summary(database_url):
    with psycopg.connect(
        database_url, connect_timeout=3, options="-c statement_timeout=5000"
    ) as db:
        # Pin the generation and counts in one consistent, read-only transaction.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        build = pin_search_build(db)
        key = (database_url, build["build_id"])
        with _cache_lock:
            cached = _cache.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                _cache.move_to_end(key)
                return copy.deepcopy(cached[1])
        rows = db.execute(
            """SELECT domain,family,count(*) FROM commerce.catalog_products
            WHERE import_id=%s GROUP BY domain,family ORDER BY domain,family""",
            (build["import_id"],),
        ).fetchall()
        result = {
            "product_count": sum(row[2] for row in rows),
            "storefronts": [
                dict(
                    id=key,
                    label=section["label"],
                    families=section["families"],
                    product_count=sum(
                        count
                        for domain, family, count in rows
                        if family in section["families"] and domain == section["domain"]
                    ),
                )
                for key, section in STOREFRONTS.items()
            ],
            "unmapped_product_count": sum(
                count
                for domain, family, count in rows
                if family not in FAMILY_STOREFRONT
                or domain != STOREFRONTS[FAMILY_STOREFRONT[family]]["domain"]
            ),
            "indexed_product_count": build["catalog_products"],
            "categories": [
                {"domain": domain, "family": family, "count": count}
                for domain, family, count in rows
            ],
            "price_context": "historical USD",
            "catalog_id": build["import_id"],
            "build_id": build["build_id"],
        }

        with _cache_lock:
            _cache[key] = (time.monotonic(), copy.deepcopy(result))
            _cache.move_to_end(key)
            while len(_cache) > 8:
                _cache.popitem(last=False)
        return result
