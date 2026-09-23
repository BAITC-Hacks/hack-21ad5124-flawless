"""EKT catalog loading and normalization. Unknown stock is never treated as available."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from urllib.parse import urlparse

import requests

LOG = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
API_URL = "https://ekt.kz/api/products"
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
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _stock(value):
    number = _number(value)
    return max(0, int(number)) if number is not None else None


def normalize_product(raw: dict) -> dict:
    """Map both live API detail records and local demo records to one shape."""
    url = str(raw.get("url") or "")
    parts = urlparse(url).path.strip("/").split("/")
    category_slug = parts[1] if len(parts) > 1 and parts[0] == "catalog" else ""
    category = raw.get("category") or CATEGORY_NAMES.get(category_slug, category_slug.replace("_", " "))
    properties = raw.get("properties") or raw.get("characteristics") or {}
    certificates = raw.get("certificates") or []
    if not certificates and isinstance(properties, dict):
        certificates = [value for key, value in properties.items() if "CERT" in key.upper() and value]
    if not isinstance(certificates, list):
        certificates = [certificates]
    stock = _stock(raw.get("stock", raw.get("quantity")))
    stores = raw.get("stores") or []
    if stock is None and stores:
        stock = sum(_stock(store.get("quantity")) or 0 for store in stores if isinstance(store, dict))
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
        "url": url,
    }


class Catalog:
    def __init__(self, demo_mode: bool | None = None):
        self.demo_mode = (os.getenv("DEMO_MODE", "0") == "1") if demo_mode is None else demo_mode
        self.source = ""
        self.products: list[dict] = []
        self.by_article: dict[str, dict] = {}
        self.lock = RLock()

    def load(self):
        if not self.demo_mode:
            try:
                products = self._load_live()
                if products:
                    self._set_products(products, "live")
                    return
                LOG.warning("EKT API returned no products; loading demo catalog")
            except (requests.RequestException, ValueError, KeyError) as exc:
                LOG.warning("EKT API unavailable; loading demo catalog: %s", exc)
        path = BASE_DIR / "catalog_demo.json"
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        items = raw if isinstance(raw, list) else raw.get("items", raw.get("products", []))
        self._set_products([normalize_product(item) for item in items], "demo")

    def _set_products(self, products: list[dict], source: str):
        with self.lock:
            self.products = [p for p in products if p["article"] and p["name"]]
            self.by_article = {p["article"].casefold(): p for p in self.products}
            self.source = source
        LOG.info("Loaded %s products from %s", len(self.products), source)

    def _session(self):
        username = os.getenv("EKT_API_USER", "apiuser")
        password = os.getenv("EKT_API_PASSWORD")
        if not password:
            raise ValueError("EKT_API_PASSWORD is not configured")
        session = requests.Session()
        session.auth = (username, password)
        session.headers["Accept"] = "application/json"
        return session

    def _load_live(self):
        session = self._session()
        pages = max(1, min(10, int(os.getenv("CATALOG_PAGES", "3"))))
        raw_products: list[dict] = []
        try:
            for page in range(1, pages + 1):
                response = session.get(API_URL, params={"page": page}, timeout=8)
                response.raise_for_status()
                payload = response.json()
                items = payload.get("items", []) if isinstance(payload, dict) else payload
                if not isinstance(items, list):
                    raise ValueError("Unexpected EKT catalog response")
                if not items:
                    break
                raw_products.extend(items)
                page_size = int(payload.get("per_page", len(items))) if isinstance(payload, dict) else len(items)
                if len(items) < page_size:
                    break
        finally:
            session.close()

        def enrich(item):
            if not item.get("id"):
                return normalize_product(item)
            try:
                with self._session() as detail_session:
                    response = detail_session.get(f"{API_URL}/detail", params={"id": item["id"]}, timeout=8)
                    response.raise_for_status()
                    detail = response.json()
                return normalize_product({**item, **detail})
            except (requests.RequestException, ValueError) as exc:
                LOG.warning("Could not load product detail %s: %s", item.get("id"), exc)
                return normalize_product(item)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(enrich, item) for item in raw_products]
            return [future.result() for future in futures]

    def search(self, query: str, limit: int = 8) -> list[dict]:
        words = query.casefold().split()
        if not words:
            return []
        with self.lock:
            matches = [p for p in self.products if all(word in f"{p['article']} {p['name']} {p['category']}".casefold() for word in words)]
        matches.sort(key=lambda p: (p["availability"] != "in_stock", p["article"].casefold()))
        return matches[:limit]

    def get(self, article: str) -> dict | None:
        with self.lock:
            return self.by_article.get(article.casefold().strip())

    def analogs(self, article: str, limit: int = 5) -> list[dict]:
        original = self.get(article)
        if not original:
            return []
        with self.lock:
            return [p for p in self.products if p["article"] != original["article"] and p["category"] == original["category"] and p["availability"] == "in_stock"][:limit]
