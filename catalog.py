"""EKT catalog loading and normalization. Unknown stock is never treated as available."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Event, RLock
from urllib.parse import urlparse

import requests

LOG = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
API_URL = "https://ekt.kz/api/products"
DETAIL_API_URL = f"{API_URL}/detail"
DEFAULT_CATALOG_PER_PAGE = 1000
MAX_CATALOG_PER_PAGE = 1000
MAX_CATALOG_PAGES = 100
CATALOG_PAGE_WORKERS = 6
DETAIL_WORKERS = 6
MAX_SEARCH_DETAIL_CANDIDATES = 24
MAX_ANALOG_DETAIL_CANDIDATES = 24
CATALOG_REQUEST_TIMEOUT = 15
DETAIL_REQUEST_TIMEOUT = 5
REQUEST_ATTEMPTS = 2


def asset_path(name: str) -> Path:
    """Use the logic team's assets once that branch is merged."""
    logic_file = BASE_DIR / "logic" / name
    return logic_file if logic_file.is_file() else BASE_DIR / name


CATEGORY_NAMES = {
    "kabel_provod": "Кабель / Провод",
    "svetilniki_lampy": "Светильники / Лампы",
    "nizkovoltnaya_apparatura": "Низковольтная аппаратура",
    "kabelenesushchie_sistemy": "Кабеленесущие системы",
    "izdeliya_dlya_montazha_i_instrument": "Изделия для монтажа и инструмент",
    "rozetki_vyklyuchateli_korobki": "Розетки / Выключатели / Коробки",
    "avtomatizatsiya": "Автоматизация",
}


def _number(value):
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(" ", "").replace(",", "."))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _stock(value):
    number = _number(value)
    return max(0, int(number)) if number is not None else None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        LOG.warning("Ignoring invalid %s value", name)
        return default


def _normalize_search_text(value) -> str:
    """Make cable dimensions, Russian letters and punctuation searchable alike."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("ё", "е").replace("х", "x")
    text = re.sub(r"(?<=\d)\s*[,\.]\s*(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "x", text)
    text = re.sub(r"[^\w.]+", " ", text, flags=re.UNICODE).replace("_", " ")
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    # Keep useful unit equivalence ("16 А"/"16А") without joining arbitrary words.
    text = re.sub(
        r"(?<=\d)\s+(?=(?:ка|ka|ма|ma|а|a|квт|kw|вт|w|кв|kv|в|v|мм|mm|см|cm|м|m)\b)",
        "",
        text,
        flags=re.UNICODE,
    )
    return " ".join(text.split())


def _search_values(value) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, nested in value.items():
            result.extend((str(key), *_search_values(nested)))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for nested in value:
            result.extend(_search_values(nested))
        return result
    return [str(value)] if value is not None else []


def _availability_rank(product: dict) -> int:
    return {"in_stock": 0, "unknown": 1, "out_of_stock": 2}.get(
        str(product.get("availability") or "unknown"),
        1,
    )


def normalize_product(raw: dict) -> dict:
    """Map both live API detail records and local demo records to one shape."""
    if not isinstance(raw, dict):
        raw = {}
    url = str(raw.get("url") or "")
    parts = urlparse(url).path.strip("/").split("/")
    category_slug = parts[1] if len(parts) > 1 and parts[0] == "catalog" else ""
    category = raw.get("category") or CATEGORY_NAMES.get(category_slug, category_slug.replace("_", " "))
    properties = raw.get("properties") or raw.get("characteristics") or raw.get("specs") or {}
    if not isinstance(properties, dict):
        properties = {}
    certificates = raw.get("certificates") or []
    if not certificates and isinstance(properties, dict):
        certificates = [value for key, value in properties.items() if "CERT" in key.upper() and value]
    if not isinstance(certificates, list):
        certificates = [certificates]
    raw_stock = raw.get("stock")
    if raw_stock is None:
        raw_stock = raw.get("quantity")
    stock = _stock(raw_stock)
    stores = raw.get("stores") or []
    if isinstance(raw_stock, dict):
        stores = [{"name": name, "quantity": quantity} for name, quantity in raw_stock.items()]
    if stock is None and stores:
        quantities = [_stock(store.get("quantity")) for store in stores if isinstance(store, dict)]
        if quantities and all(quantity is not None for quantity in quantities):
            stock = sum(quantities)
    return {
        "id": raw.get("id"),
        "article": str(raw.get("article") or raw.get("sku") or "").strip(),
        "name": str(raw.get("name") or raw.get("title") or "").strip(),
        "category": str(category or "Прочее оборудование"),
        "characteristics": properties,
        "certificates": certificates,
        "description": raw.get("description") or "",
        "price": _number(raw.get("price")),
        "stock": stock,
        "stores": stores,
        "availability": "in_stock" if stock is not None and stock > 0 else "out_of_stock" if stock == 0 else "unknown",
        "image": raw.get("image") or "",
        "offers": raw.get("offers") or [],
        "url": url,
        "url_api_detail": raw.get("url_api_detail") or "",
    }


class Catalog:
    def __init__(self, demo_mode: bool | None = None):
        self.demo_mode = (os.getenv("DEMO_MODE", "0") == "1") if demo_mode is None else demo_mode
        self.source = ""
        self.products: list[dict] = []
        self.by_article: dict[str, dict] = {}
        self.lock = RLock()
        self._detail_attempted: set[str] = set()
        self._detail_inflight: dict[str, Event] = {}
        self._detail_slots = BoundedSemaphore(DETAIL_WORKERS)

    def load(self):
        if not self.demo_mode:
            try:
                products = self._load_live()
                if products:
                    self._set_products(products, "live")
                    return
                LOG.warning("EKT API returned no products")
            except (requests.RequestException, ValueError, KeyError) as exc:
                LOG.warning("EKT API unavailable: %s", exc)
            self._set_products([], "unavailable")
            return
        path = BASE_DIR / "catalog_demo.json"
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        items = raw if isinstance(raw, list) else raw.get("items", raw.get("products", []))
        self._set_products([normalize_product(item) for item in items], "demo")

    def _set_products(self, products: list[dict], source: str):
        valid: list[dict] = []
        seen_articles: set[str] = set()
        for product in products:
            if not isinstance(product, dict):
                continue
            article = str(product.get("article") or "").strip()
            name = str(product.get("name") or "").strip()
            key = article.casefold()
            product_id = product.get("id")
            missing_live_id = source == "live" and (product_id is None or not str(product_id).strip())
            if not article or not name or missing_live_id or key in seen_articles:
                continue
            seen_articles.add(key)
            valid.append(product)
        effective_source = "unavailable" if source == "live" and not valid else source
        with self.lock:
            for event in self._detail_inflight.values():
                event.set()
            self._detail_attempted.clear()
            self._detail_inflight.clear()
            self.products = valid
            self.by_article = {p["article"].casefold(): p for p in self.products}
            self.source = effective_source
        LOG.info("Loaded %s products from %s", len(self.products), effective_source)

    def _session(self):
        username = (os.getenv("EKT_API_USER") or "").strip()
        password = os.getenv("EKT_API_PASSWORD")
        if not username or not password:
            raise ValueError("EKT_API_USER and EKT_API_PASSWORD must be configured")
        session = requests.Session()
        session.auth = (username, password)
        session.headers["Accept"] = "application/json"
        return session

    @staticmethod
    def _request_json(session, url: str, *, params: dict, timeout: int):
        last_error = None
        for attempt in range(REQUEST_ATTEMPTS):
            try:
                response = session.get(url, params=params, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < REQUEST_ATTEMPTS:
                    LOG.warning("Retrying EKT API request after %s", type(exc).__name__)
        raise last_error

    @staticmethod
    def _catalog_items(payload) -> list:
        if not isinstance(payload, dict):
            raise ValueError("Unexpected EKT catalog response")
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise ValueError("Unexpected EKT catalog response")
        return items

    @staticmethod
    def _known_catalog_pages(payload: dict, item_count: int, per_page: int, page_limit: int) -> int | None:
        """Return a bounded page count when the first response exposes pagination."""
        for key in ("total_pages", "pages", "last_page"):
            try:
                reported_pages = int(payload.get(key))
            except (TypeError, ValueError):
                continue
            if reported_pages > 0:
                return min(reported_pages, page_limit)

        try:
            total_count = int(payload.get("count"))
        except (TypeError, ValueError):
            total_count = None
        if total_count is not None and total_count >= 0:
            try:
                response_page_size = int(payload.get("per_page"))
            except (TypeError, ValueError):
                # The endpoint may silently cap a requested page size. In that
                # case the first non-empty page is the only safe divisor.
                response_page_size = item_count or per_page
            if response_page_size <= 0:
                response_page_size = item_count or per_page
            elif 0 < item_count < response_page_size and total_count > item_count:
                response_page_size = item_count
            return min(max(1, math.ceil(total_count / response_page_size)), page_limit)

        try:
            response_page_size = int(payload.get("per_page", per_page))
        except (TypeError, ValueError):
            response_page_size = per_page
        if response_page_size > 0 and item_count < response_page_size:
            return 1
        return None

    def _load_catalog_pages_parallel(self, page_numbers: list[int], per_page: int) -> list[tuple[int, dict]]:
        """Fetch known catalog pages concurrently, with one Session per worker."""
        if not page_numbers:
            return []
        worker_count = min(CATALOG_PAGE_WORKERS, len(page_numbers))
        chunks = [page_numbers[index::worker_count] for index in range(worker_count)]

        def load_chunk(chunk: list[int]) -> list[tuple[int, dict]]:
            session = self._session()
            try:
                return [
                    (
                        page,
                        self._request_json(
                            session,
                            API_URL,
                            params={"page": page, "per_page": per_page},
                            timeout=CATALOG_REQUEST_TIMEOUT,
                        ),
                    )
                    for page in chunk
                ]
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            chunk_results = list(pool.map(load_chunk, chunks))
        return sorted(
            (result for chunk_result in chunk_results for result in chunk_result),
            key=lambda result: result[0],
        )

    def _load_live(self):
        session = self._session()
        per_page = _env_int("CATALOG_PER_PAGE", DEFAULT_CATALOG_PER_PAGE)
        if per_page <= 0:
            per_page = DEFAULT_CATALOG_PER_PAGE
        per_page = min(per_page, MAX_CATALOG_PER_PAGE)
        configured_pages = _env_int("CATALOG_PAGES", 0)
        pages = min(configured_pages, MAX_CATALOG_PAGES) if configured_pages > 0 else MAX_CATALOG_PAGES
        products: list[dict] = []
        seen_fingerprints: set[tuple[tuple[str, str, str], ...]] = set()
        seen_reported_pages: set[int] = set()
        received_items = 0

        def accept_page(payload: dict) -> bool:
            nonlocal received_items
            items = self._catalog_items(payload)
            if not items:
                return False

            reported_page = payload.get("page")
            if isinstance(reported_page, int):
                if reported_page in seen_reported_pages:
                    LOG.warning("Stopping EKT pagination after repeated page %s", reported_page)
                    return False
                seen_reported_pages.add(reported_page)
            fingerprint = tuple(
                (
                    str(item.get("id") or ""),
                    str(item.get("article") or ""),
                    str(item.get("name") or ""),
                )
                if isinstance(item, dict)
                else ("", "", repr(item))
                for item in items
            )
            if fingerprint in seen_fingerprints:
                LOG.warning("Stopping EKT pagination after duplicate page content")
                return False
            seen_fingerprints.add(fingerprint)

            for item in items:
                normalized = normalize_product(item)
                if (
                    normalized["id"] is not None
                    and str(normalized["id"]).strip()
                    and normalized["article"]
                    and normalized["name"]
                ):
                    products.append(normalized)
            received_items += len(items)
            try:
                response_page_size = int(payload.get("per_page", per_page))
            except (TypeError, ValueError):
                response_page_size = per_page
            if response_page_size <= 0:
                response_page_size = per_page
            if len(items) < response_page_size:
                try:
                    total_count = int(payload.get("count"))
                except (TypeError, ValueError):
                    total_count = None
                if total_count is None or received_items >= total_count:
                    return False
            return True

        try:
            first_payload = self._request_json(
                session,
                API_URL,
                params={"page": 1, "per_page": per_page},
                timeout=CATALOG_REQUEST_TIMEOUT,
            )
            first_items = self._catalog_items(first_payload)
            known_pages = self._known_catalog_pages(first_payload, len(first_items), per_page, pages)
            if not accept_page(first_payload) or pages == 1:
                return products

            if known_pages is not None:
                remaining_pages = list(range(2, known_pages + 1))
                if len(remaining_pages) == 1:
                    page = remaining_pages[0]
                    page_payloads = [
                        (
                            page,
                            self._request_json(
                                session,
                                API_URL,
                                params={"page": page, "per_page": per_page},
                                timeout=CATALOG_REQUEST_TIMEOUT,
                            ),
                        )
                    ]
                elif remaining_pages:
                    page_payloads = self._load_catalog_pages_parallel(remaining_pages, per_page)
                else:
                    page_payloads = []
                for _page, payload in page_payloads:
                    if not accept_page(payload):
                        break
                return products

            # Compatibility fallback for responses without a usable total.
            for page in range(2, pages + 1):
                payload = self._request_json(
                    session,
                    API_URL,
                    params={"page": page, "per_page": per_page},
                    timeout=CATALOG_REQUEST_TIMEOUT,
                )
                if not accept_page(payload):
                    break
        finally:
            session.close()

        return products

    @staticmethod
    def _detail_key(product: dict) -> str | None:
        product_id = product.get("id")
        if product_id is None or str(product_id).strip() == "":
            return None
        return str(product_id)

    def _fetch_detail(self, product: dict) -> dict | None:
        product_id = product.get("id")
        session = None
        try:
            with self._detail_slots:
                session = self._session()
                detail = self._request_json(
                    session,
                    DETAIL_API_URL,
                    params={"id": product_id},
                    timeout=DETAIL_REQUEST_TIMEOUT,
                )
            if not isinstance(detail, dict) or not detail:
                raise ValueError("Unexpected EKT product detail response")
            detail_id = detail.get("id")
            if detail_id is not None and str(detail_id) != str(product_id):
                raise ValueError("EKT product detail id does not match")
            return detail
        except (requests.RequestException, TypeError, ValueError) as exc:
            LOG.warning("Could not load product detail %s: %s", product_id, exc)
            return None
        finally:
            if session is not None:
                session.close()

    def _ensure_detail(self, product: dict) -> dict:
        if self.source != "live":
            return product
        key = self._detail_key(product)
        if key is None:
            return product

        with self.lock:
            if key in self._detail_attempted:
                return product
            event = self._detail_inflight.get(key)
            owner = event is None
            if owner:
                event = Event()
                self._detail_inflight[key] = event
        if not owner:
            event.wait(DETAIL_REQUEST_TIMEOUT * REQUEST_ATTEMPTS + 2)
            return product

        try:
            detail = self._fetch_detail(product)
            if detail is not None:
                merged = dict(product)
                merged.update(detail)
                # A detail quantity is authoritative even when it explicitly becomes unknown.
                if "quantity" in detail and "stock" not in detail:
                    merged.pop("stock", None)
                if not str(merged.get("article") or "").strip():
                    merged["article"] = product.get("article")
                if not str(merged.get("name") or "").strip():
                    merged["name"] = product.get("name")
                enriched = normalize_product(merged)
                if enriched["article"] and enriched["name"]:
                    with self.lock:
                        old_key = product["article"].casefold()
                        product.clear()
                        product.update(enriched)
                        new_key = product["article"].casefold()
                        if old_key != new_key and self.by_article.get(old_key) is product:
                            del self.by_article[old_key]
                        self.by_article[new_key] = product
        finally:
            with self.lock:
                self._detail_attempted.add(key)
                finished = self._detail_inflight.pop(key, None)
                if finished is not None:
                    finished.set()
        return product

    def _enrich_details(self, products: list[dict]) -> list[dict]:
        if self.source != "live" or not products:
            return products
        with ThreadPoolExecutor(max_workers=min(DETAIL_WORKERS, len(products))) as pool:
            return list(pool.map(self._ensure_detail, products))

    def search(self, query: str, limit: int = 8) -> list[dict]:
        words = _normalize_search_text(query).split()
        if not words or limit <= 0:
            return []
        with self.lock:
            matches = []
            for product in self.products:
                values = [
                    product.get("article"),
                    product.get("name"),
                    product.get("category"),
                    product.get("description"),
                    *_search_values(product.get("characteristics")),
                ]
                searchable = _normalize_search_text(" ".join(str(value or "") for value in values))
                if all(word in searchable for word in words):
                    matches.append(product)
        matches.sort(key=lambda p: (_availability_rank(p), p["article"].casefold()))

        if self.source == "live" and matches:
            target = min(limit, len(matches))
            budget = min(len(matches), MAX_SEARCH_DETAIL_CANDIDATES)
            enriched = min(target, budget)
            self._enrich_details(matches[:enriched])
            while (
                enriched < budget
                and sum(product["availability"] == "in_stock" for product in matches[:enriched]) < target
            ):
                batch_end = min(budget, enriched + DETAIL_WORKERS)
                self._enrich_details(matches[enriched:batch_end])
                enriched = batch_end

        matches.sort(key=lambda p: (_availability_rank(p), p["article"].casefold()))
        return matches[:limit]

    def get(self, article: str) -> dict | None:
        with self.lock:
            product = self.by_article.get(article.casefold().strip())
        return self._ensure_detail(product) if product is not None else None

    def analogs(self, article: str, limit: int = 5) -> list[dict]:
        if limit <= 0:
            return []
        original = self.get(article)
        if not original:
            return []
        with self.lock:
            candidates = [
                product
                for product in self.products
                if product["article"] != original["article"]
                and product["category"] == original["category"]
            ]

        if self.source == "live" and candidates:
            original_words = set(_normalize_search_text(original.get("name")).split())

            def summary_rank(product):
                product_words = set(_normalize_search_text(product.get("name")).split())
                return (
                    _availability_rank(product),
                    -len(original_words & product_words),
                    product["article"].casefold(),
                )

            candidates.sort(key=summary_rank)
            candidate_budget = min(
                len(candidates),
                MAX_ANALOG_DETAIL_CANDIDATES,
                max(DETAIL_WORKERS, limit * 3),
            )
            self._enrich_details(candidates[:candidate_budget])

        matches = [
            product
            for product in candidates
            if product["category"] == original["category"]
            and product["availability"] == "in_stock"
        ]

        def similarity(product):
            original_specs = {str(key).casefold(): str(value).casefold() for key, value in original["characteristics"].items()}
            product_specs = {str(key).casefold(): str(value).casefold() for key, value in product["characteristics"].items()}
            return sum(product_specs.get(key) == value for key, value in original_specs.items())
        return sorted(matches, key=lambda p: (-similarity(p), p["article"].casefold()))[:limit]
