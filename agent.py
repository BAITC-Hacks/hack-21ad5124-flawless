"""Tool dispatch, cart rules and a deterministic offline demo assistant."""

from __future__ import annotations

import json
import re
from threading import RLock

from catalog import BASE_DIR, Catalog

CART_LINK = "https://ekt.kz/cart"


def product_brief(product: dict) -> dict:
    return {key: product.get(key) for key in ("article", "name", "category", "price", "stock", "availability", "url")}


def purchase_confirmed(messages: list[dict]) -> bool:
    """Require an explicit latest user instruction or yes to an assistant's cart offer."""
    if not messages or messages[-1].get("role") != "user":
        return False
    text = str(messages[-1].get("content") or "").casefold().strip()
    if not text or re.search(r"\b(не|нет|отмена|отменить|пока не|без)\s+(?:\w+\s+){0,2}(?:добав|клад|полож|корзин)", text):
        return False
    if re.search(r"\b(?:добав(?:ь|ьте|ить|ляем)|полож(?:и|ите|ить)|клади|оформ(?:и|ите|ить))\b", text):
        return not text.endswith("?")
    if re.fullmatch(r"(?:да|ага|ок|окей|подтверждаю|согласен|согласна)[.!\s]*", text):
        previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
        return bool(previous and re.search(r"корзин|добав", str(previous.get("content") or "").casefold()))
    return False


class ShopTools:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.carts: dict[str, dict[str, int]] = {}
        self.lock = RLock()

    def search_products(self, query: str):
        return [product_brief(p) for p in self.catalog.search(query)]

    def get_product(self, article: str):
        product = self.catalog.get(article)
        if not product:
            return {"error": "Товар не найден", "article": article}
        return {**product, "description": str(product.get("description") or "")[:2500]}

    def find_analogs(self, article: str):
        if not self.catalog.get(article):
            return {"error": "Товар не найден", "article": article}
        return [product_brief(p) for p in self.catalog.analogs(article)]

    def get_purchase_conditions(self):
        path = BASE_DIR / "purchase_conditions.txt"
        return path.read_text(encoding="utf-8") if path.exists() else "Условия покупки уточняйте у менеджера EKT."

    def get_cart(self, session_id: str):
        with self.lock:
            entries = list(self.carts.get(session_id, {}).items())
        result = []
        for article, qty in entries:
            product = self.catalog.get(article)
            if product:
                result.append({"article": product["article"], "name": product["name"], "qty": qty, "price": product["price"]})
        return result

    def add_to_cart(self, session_id: str, article: str, qty: int, messages: list[dict]):
        if not purchase_confirmed(messages):
            return {"error": "Нужно явное подтверждение клиента на добавление товара в корзину"}
        if type(qty) is not int or qty < 1:
            return {"error": "Количество должно быть положительным целым числом"}
        product = self.catalog.get(article)
        if not product:
            return {"error": "Товар не найден", "article": article}
        with self.lock:
            existing = self.carts.get(session_id, {}).get(product["article"], 0)
            stock = product["stock"]
            if stock is None:
                return {"error": "Остаток товара неизвестен; добавление недоступно", "article": article}
            if existing + qty > stock:
                return {"error": "Недостаточно товара на складе", "article": article, "available": max(0, stock - existing)}
            self.carts.setdefault(session_id, {})[product["article"]] = existing + qty
        return {"ok": True, "article": product["article"], "qty": existing + qty, "cart": self.get_cart(session_id)}

    def dispatch(self, name: str, args: dict, session_id: str, messages: list[dict]):
        try:
            if name == "search_products":
                return self.search_products(str(args["query"]))
            if name == "get_product":
                return self.get_product(str(args["article"]))
            if name == "find_analogs":
                return self.find_analogs(str(args["article"]))
            if name == "get_purchase_conditions":
                return self.get_purchase_conditions()
            if name == "add_to_cart":
                # Always bind to the request session. The model cannot choose another cart.
                return self.add_to_cart(session_id, str(args["article"]), args["qty"], messages)
            if name == "get_cart":
                return self.get_cart(session_id)
        except (KeyError, TypeError, ValueError):
            return {"error": "Некорректные аргументы инструмента"}
        return {"error": "Неизвестный инструмент"}


TOOLS = [
    {"type": "function", "function": {"name": "search_products", "description": "Искать товары по названию, артикулу или категории.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "get_product", "description": "Получить характеристики, сертификаты, цену и остатки товара по артикулу.", "parameters": {"type": "object", "properties": {"article": {"type": "string"}}, "required": ["article"]}}},
    {"type": "function", "function": {"name": "find_analogs", "description": "Найти имеющиеся в наличии товары той же категории.", "parameters": {"type": "object", "properties": {"article": {"type": "string"}}, "required": ["article"]}}},
    {"type": "function", "function": {"name": "get_purchase_conditions", "description": "Получить условия покупки и доставки.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "add_to_cart", "description": "Добавить товар в локальную корзину только после явного подтверждения клиента. session_id берётся с сервера.", "parameters": {"type": "object", "properties": {"article": {"type": "string"}, "qty": {"type": "integer", "minimum": 1}}, "required": ["article", "qty"]}}},
    {"type": "function", "function": {"name": "get_cart", "description": "Показать локальную корзину текущей сессии.", "parameters": {"type": "object", "properties": {}}}},
]


def run_openai_agent(messages: list[dict], session_id: str, tools: ShopTools, model: str, api_key: str) -> str:
    from openai import OpenAI

    system_prompt = (BASE_DIR / "system_prompt.txt").read_text(encoding="utf-8")
    conversation = [{"role": "system", "content": system_prompt}, *messages]
    client = OpenAI(api_key=api_key, timeout=15.0, max_retries=0)
    added_articles: set[str] = set()
    for _ in range(8):
        response = client.chat.completions.create(model=model, messages=conversation, tools=TOOLS)
        message = response.choices[0].message
        calls = message.tool_calls or []
        if not calls:
            return message.content or "Не удалось подготовить ответ. Попробуйте переформулировать вопрос."
        conversation.append(message.model_dump(exclude_none=True))
        for call in calls:
            try:
                args = json.loads(call.function.arguments)
                if call.function.name == "add_to_cart" and str(args.get("article", "")).casefold() in added_articles:
                    result = {"error": "Этот товар уже добавлен в текущем запросе"}
                else:
                    result = tools.dispatch(call.function.name, args, session_id, messages)
                    if call.function.name == "add_to_cart" and isinstance(result, dict) and result.get("ok"):
                        added_articles.add(str(args["article"]).casefold())
            except (json.JSONDecodeError, AttributeError, TypeError):
                result = {"error": "Некорректные аргументы инструмента"}
            conversation.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False, default=str)})
    return "Не удалось завершить поиск. Попробуйте уточнить запрос."


def run_demo_agent(messages: list[dict], session_id: str, tools: ShopTools) -> str:
    """Offline tool-using fallback for common demo questions."""
    text = str(messages[-1]["content"]).strip()
    lower = text.casefold()
    articles = [p["article"] for p in tools.catalog.products if p["article"].casefold() in lower]
    if purchase_confirmed(messages):
        if not articles:
            previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
            if previous:
                previous_text = str(previous.get("content") or "").casefold()
                articles = [p["article"] for p in tools.catalog.products if p["article"].casefold() in previous_text]
        if len(articles) != 1:
            return "Укажите один артикул товара и подтвердите добавление, например: «Добавь 2 шт DEMO-LED-12»."
        quantity_match = re.search(r"\b(\d+)\s*(?:шт|штук|единиц)", lower)
        qty = int(quantity_match.group(1)) if quantity_match else 1
        result = tools.dispatch("add_to_cart", {"article": articles[0], "qty": qty}, session_id, messages)
        if "error" in result:
            return f"Не удалось добавить товар: {result['error']}. Доступно: {result.get('available', 'неизвестно')}."
        return f"Добавлено в корзину: {articles[0]}, {qty} шт. Локальная корзина не синхронизируется с сайтом EKT."
    if "корзин" in lower:
        cart = tools.dispatch("get_cart", {}, session_id, messages)
        return "В корзине: " + "; ".join(f"{p['name']} — {p['qty']} шт." for p in cart) if cart else "Корзина пуста."
    if any(word in lower for word in ("достав", "оплат", "услов", "покуп")):
        return tools.dispatch("get_purchase_conditions", {}, session_id, messages)
    if "аналог" in lower and articles:
        analogs = tools.dispatch("find_analogs", {"article": articles[0]}, session_id, messages)
        return "Аналоги в наличии: " + "; ".join(f"{p['name']} ({p['article']})" for p in analogs) if analogs else "Аналогов в наличии не найдено."
    if articles:
        p = tools.dispatch("get_product", {"article": articles[0]}, session_id, messages)
        if "error" not in p:
            stock = p["stock"] if p["stock"] is not None else "неизвестен"
            return f"{p['name']} ({p['article']}): {p['price']} ₸, остаток: {stock}. Добавить в корзину?"
    query = next((word for word in ("лампа", "светодиод", "автомат", "выключатель") if word in lower), "")
    results = tools.dispatch("search_products", {"query": query}, session_id, messages) if query else []
    if results:
        return "Нашёл товары: " + "; ".join(f"{p['name']} ({p['article']}), {p['price']} ₸, остаток {p['stock']}" for p in results) + ". Назовите артикул для подробностей."
    return "Могу найти лампу или автоматический выключатель, показать аналоги, условия покупки и корзину. Назовите товар или артикул."
