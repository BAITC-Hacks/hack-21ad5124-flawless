import os
import time
import unittest
from threading import Event, Lock
from unittest.mock import patch

import requests

from catalog import API_URL, CATALOG_PAGE_WORKERS, DETAIL_API_URL, Catalog, normalize_product


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.auth = None
        self.headers = {}
        self.calls = []
        self.closed = False

    def get(self, url, *, params, timeout):
        self.calls.append((url, params, timeout))
        return self.handler(url, params)

    def close(self):
        self.closed = True


class SessionFactory:
    def __init__(self, handler):
        self.handler = handler
        self.sessions = []
        self.lock = Lock()

    def __call__(self):
        session = FakeSession(self.handler)
        with self.lock:
            self.sessions.append(session)
        return session

    @property
    def calls(self):
        return [call for session in self.sessions for call in session.calls]


def summary(product_id, article=None, name=None):
    return {
        "id": product_id,
        "article": article or f"A-{product_id}",
        "name": name or f"Product {product_id}",
        "price": "100.50",
        "image": f"https://img/{product_id}.jpg",
        "url": f"https://ekt.kz/catalog/kabel_provod/{product_id}",
        "url_api_detail": f"{DETAIL_API_URL}?id={product_id}",
        "offers": [],
    }


class LiveCatalogTests(unittest.TestCase):
    def live_env(self, **overrides):
        values = {
            "DEMO_MODE": "0",
            "EKT_API_USER": "catalog-user",
            "EKT_API_PASSWORD": "catalog-password",
            "CATALOG_PAGES": "0",
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=True)

    def test_default_per_page_paginates_until_short_page_without_detail_n_plus_one(self):
        first = [summary(index) for index in range(1, 1001)]
        second = [summary(1001)]

        def handler(url, params):
            self.assertEqual(url, API_URL)
            items = first if params["page"] == 1 else second
            return FakeResponse(
                {
                    "page": params["page"],
                    "per_page": 1000,
                    "count": 1001,
                    "items": items,
                }
            )

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()

        self.assertEqual(catalog.source, "live")
        self.assertEqual(len(catalog.products), 1001)
        self.assertEqual(
            [call[1] for call in factory.calls],
            [{"page": 1, "per_page": 1000}, {"page": 2, "per_page": 1000}],
        )
        self.assertEqual(len(factory.sessions), 1, "startup must not request product details")
        self.assertEqual(factory.sessions[0].auth, ("catalog-user", "catalog-password"))
        self.assertEqual(factory.sessions[0].headers["Accept"], "application/json")
        self.assertTrue(factory.sessions[0].closed)

    def test_positive_page_limit_and_duplicate_page_guard(self):
        requested_pages = []

        def limited_handler(url, params):
            requested_pages.append(params["page"])
            item = summary(params["page"])
            return FakeResponse({"page": params["page"], "per_page": 1, "items": [item]})

        factory = SessionFactory(limited_handler)
        with self.live_env(CATALOG_PER_PAGE="1", CATALOG_PAGES="2"), patch(
            "catalog.requests.Session", side_effect=factory
        ):
            catalog = Catalog()
            catalog.load()
        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(len(catalog.products), 2)

        duplicate_requests = []
        repeated = [summary(10), summary(11)]

        def duplicate_handler(url, params):
            duplicate_requests.append(params["page"])
            return FakeResponse({"page": 1, "per_page": 2, "items": repeated})

        duplicate_factory = SessionFactory(duplicate_handler)
        with self.live_env(CATALOG_PER_PAGE="2"), patch(
            "catalog.requests.Session", side_effect=duplicate_factory
        ):
            duplicate_catalog = Catalog()
            duplicate_catalog.load()
        self.assertEqual(duplicate_requests, [1, 2])
        self.assertEqual(len(duplicate_catalog.products), 2)

    def test_server_page_cap_without_per_page_uses_total_count_to_continue(self):
        requested_pages = []
        pages = {
            1: [summary(21), summary(22)],
            2: [summary(23), summary(24)],
        }

        def handler(url, params):
            self.assertEqual(params["per_page"], 1000)
            requested_pages.append(params["page"])
            return FakeResponse(
                {
                    "page": params["page"],
                    "count": 4,
                    "items": pages[params["page"]],
                }
            )

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()

        self.assertEqual(requested_pages, [1, 2])
        self.assertEqual(len(catalog.products), 4)
        self.assertEqual(catalog.source, "live")

    def test_known_pages_load_concurrently_but_products_keep_page_order(self):
        active = 0
        max_active = 0
        activity_lock = Lock()
        overlap = Event()

        def handler(url, params):
            nonlocal active, max_active
            page = params["page"]
            if page == 1:
                return FakeResponse({"page": 1, "per_page": 1, "count": 7, "items": [summary(1)]})
            with activity_lock:
                active += 1
                max_active = max(max_active, active)
                if active >= 2:
                    overlap.set()
            self.assertTrue(overlap.wait(1), "remaining pages were not fetched concurrently")
            with activity_lock:
                active -= 1
            return FakeResponse({"page": page, "per_page": 1, "count": 7, "items": [summary(page)]})

        factory = SessionFactory(handler)
        with self.live_env(CATALOG_PER_PAGE="1"), patch(
            "catalog.requests.Session", side_effect=factory
        ):
            catalog = Catalog()
            catalog.load()

        self.assertEqual(catalog.source, "live")
        self.assertEqual([product["article"] for product in catalog.products], [f"A-{page}" for page in range(1, 8)])
        self.assertEqual(sorted(call[1]["page"] for call in factory.calls), list(range(1, 8)))
        self.assertGreaterEqual(max_active, 2)
        self.assertLessEqual(max_active, CATALOG_PAGE_WORKERS)
        self.assertTrue(all(session.closed for session in factory.sessions))

    def test_parallel_page_failure_discards_partial_catalog_and_retries(self):
        def handler(url, params):
            page = params["page"]
            if page == 3:
                raise requests.Timeout("offline test timeout")
            return FakeResponse({"page": page, "per_page": 1, "count": 4, "items": [summary(page)]})

        factory = SessionFactory(handler)
        with self.live_env(CATALOG_PER_PAGE="1"), patch(
            "catalog.requests.Session", side_effect=factory
        ):
            catalog = Catalog()
            catalog.load()

        requested_pages = [call[1]["page"] for call in factory.calls]
        self.assertEqual(requested_pages.count(3), 2, "failed pages must keep the bounded retry policy")
        self.assertEqual(catalog.source, "unavailable")
        self.assertEqual(catalog.products, [])
        self.assertTrue(all(session.closed for session in factory.sessions))

    def test_live_mode_requires_both_organizer_credentials(self):
        with patch.dict(os.environ, {"DEMO_MODE": "0", "EKT_API_PASSWORD": "only-password"}, clear=True), patch(
            "catalog.requests.Session"
        ) as session_factory:
            catalog = Catalog()
            catalog.load()

        session_factory.assert_not_called()
        self.assertEqual(catalog.source, "unavailable")
        self.assertEqual(catalog.products, [])

    def test_search_and_get_load_details_lazily_cache_them_and_never_exceed_six_workers(self):
        summaries = [summary(index, article=f"A{index}", name=f"Cable product {index}") for index in range(9)]
        detail_calls = []
        activity_lock = Lock()
        active = 0
        max_active = 0

        def handler(url, params):
            nonlocal active, max_active
            if url == API_URL:
                return FakeResponse({"page": 1, "per_page": 1000, "count": 9, "items": summaries})
            self.assertEqual(url, DETAIL_API_URL)
            with activity_lock:
                active += 1
                max_active = max(max_active, active)
                detail_calls.append(params["id"])
            time.sleep(0.02)
            with activity_lock:
                active -= 1
            product_id = params["id"]
            return FakeResponse(
                {
                    **summaries[product_id],
                    "description": f"Detail {product_id}",
                    "quantity": product_id + 1,
                    "stores": [{"name": "Almaty", "quantity": product_id + 1}],
                    "properties": {"cores": "3", "section": "2.5"},
                }
            )

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()
            self.assertEqual(detail_calls, [])

            found = catalog.search("cable", limit=7)
            self.assertEqual(len(found), 7)
            self.assertEqual(len(detail_calls), 7)
            self.assertLessEqual(max_active, 6)
            self.assertTrue(all(product["availability"] == "in_stock" for product in found))

            catalog.search("cable", limit=7)
            catalog.get("A0")
            self.assertEqual(len(detail_calls), 7, "search and get must reuse cached details")

            product = catalog.get("A8")
            self.assertEqual(product["stock"], 9)
            self.assertEqual(product["characteristics"]["section"], "2.5")
            catalog.get("A8")
            self.assertEqual(len(detail_calls), 8)

        self.assertTrue(all(session.auth == ("catalog-user", "catalog-password") for session in factory.sessions))
        self.assertTrue(all(session.closed for session in factory.sessions))

    def test_search_expands_bounded_detail_window_past_unavailable_summaries(self):
        summaries = [
            summary(index, article=f"A{index}", name=f"Breaker product {index}")
            for index in range(10)
        ]
        detail_calls = []

        def handler(url, params):
            if url == API_URL:
                return FakeResponse({"page": 1, "count": 10, "items": summaries})
            product_id = params["id"]
            detail_calls.append(product_id)
            return FakeResponse(
                {
                    **summaries[product_id],
                    "quantity": 4 if product_id == 6 else 0,
                    "properties": {"current": "16 A"},
                }
            )

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()
            found = catalog.search("breaker", limit=1)
            first_call_count = len(detail_calls)
            cached = catalog.search("breaker", limit=1)

        self.assertEqual(found[0]["article"], "A6")
        self.assertEqual(cached[0]["article"], "A6")
        self.assertEqual(set(detail_calls), set(range(7)))
        self.assertEqual(first_call_count, 7)
        self.assertEqual(len(detail_calls), first_call_count)

    def test_live_analogs_lazy_load_candidate_details_and_cache_them(self):
        summaries = [
            summary(0, article="ORIG", name="Cable 3x2.5 Brand A"),
            summary(1, article="OUT", name="Cable 3x2.5 Brand B"),
            summary(2, article="BEST", name="Cable 3x2.5 Brand C"),
            summary(3, article="OTHER", name="Cable 2x1.5 Brand D"),
        ]
        details = {
            0: {"quantity": 1, "properties": {"cores": "3", "section": "2.5"}},
            1: {"quantity": 0, "properties": {"cores": "3", "section": "2.5"}},
            2: {"quantity": 5, "properties": {"cores": "3", "section": "2.5"}},
            3: {"quantity": 3, "properties": {"cores": "2", "section": "1.5"}},
        }
        detail_calls = []

        def handler(url, params):
            if url == API_URL:
                return FakeResponse({"page": 1, "per_page": 1000, "count": 4, "items": summaries})
            product_id = params["id"]
            detail_calls.append(product_id)
            return FakeResponse({**summaries[product_id], **details[product_id]})

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()
            self.assertEqual(detail_calls, [])
            found = catalog.analogs("ORIG", limit=2)
            first_call_count = len(detail_calls)
            cached = catalog.analogs("ORIG", limit=2)

        self.assertEqual([product["article"] for product in found], ["BEST", "OTHER"])
        self.assertEqual([product["article"] for product in cached], ["BEST", "OTHER"])
        self.assertEqual(set(detail_calls), {0, 1, 2, 3})
        self.assertEqual(first_call_count, 4)
        self.assertEqual(len(detail_calls), first_call_count)

    def test_malformed_detail_is_fail_safe_and_cached(self):
        detail_calls = []

        def handler(url, params):
            if url == API_URL:
                return FakeResponse({"page": 1, "per_page": 1000, "items": [summary(7, article="SAFE-7")]})
            detail_calls.append(params["id"])
            return FakeResponse(["not", "an", "object"])

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()
            first = catalog.get("SAFE-7")
            second = catalog.get("SAFE-7")

        self.assertIs(first, second)
        self.assertEqual(first["availability"], "unknown")
        self.assertEqual(first["name"], "Product 7")
        self.assertEqual(detail_calls, [7])

    def test_search_normalizes_cable_dimensions_yo_and_punctuation(self):
        catalog = Catalog(demo_mode=True)
        catalog._set_products(
            [
                normalize_product(
                    {
                        "id": 1,
                        "article": "VVG-3X25",
                        "name": "\u041a\u0430\u0431\u0435\u043b\u044c \u0401\u043b\u043e\u0447\u043a\u0430 \u0412\u0412\u0413\u043d\u0433(\u0410)-LS 3\u04452,5",
                        "properties": {"voltage": "0,66 \u043a\u0412"},
                        "stock": 2,
                    }
                )
            ],
            "demo",
        )

        for query in ("3\u04452.5", "3\u04452,5", "3x2.5"):
            self.assertEqual(catalog.search(query)[0]["article"], "VVG-3X25")
        self.assertEqual(catalog.search("\u043a\u0430\u0431\u0435\u043b\u044c \u0435\u043b\u043e\u0447\u043a\u0430")[0]["article"], "VVG-3X25")
        self.assertEqual(catalog.search("\u0412\u0412\u0413\u043d\u0433-\u0410/LS!")[0]["article"], "VVG-3X25")
        self.assertEqual(catalog.search("0,66\u043a\u0412")[0]["article"], "VVG-3X25")

    def test_live_catalog_with_zero_valid_products_is_unavailable(self):
        malformed = [
            None,
            {},
            {"article": "", "name": "No article"},
            {"article": "NO-NAME"},
            {"article": "NO-ID", "name": "Missing id"},
        ]

        def handler(url, params):
            return FakeResponse({"page": 1, "per_page": 1000, "count": 5, "items": malformed})

        factory = SessionFactory(handler)
        with self.live_env(), patch("catalog.requests.Session", side_effect=factory):
            catalog = Catalog()
            catalog.load()

        self.assertEqual(catalog.source, "unavailable")
        self.assertEqual(catalog.products, [])
        self.assertEqual(catalog.by_article, {})


if __name__ == "__main__":
    unittest.main()
