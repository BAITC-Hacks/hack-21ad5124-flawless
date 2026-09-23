"""Tool dispatch, cart rules and a deterministic offline demo assistant."""

from __future__ import annotations

import hashlib
import json
import re
from threading import RLock

from catalog import BASE_DIR, Catalog, asset_path

CART_LINK = "https://ekt.kz/cart"


def contains_payment_data(text: str) -> bool:
    return bool(re.search(r"(?:\d[ -]?){13,19}", text) or re.search(r"\b(?:cvv|cvc|код\s+из\s+смс|sms\s*код)\b", text, re.IGNORECASE))


def product_brief(product: dict) -> dict:
    return {key: product.get(key) for key in ("article", "name", "category", "price", "stock", "availability", "url")}


def purchase_confirmed(messages: list[dict]) -> bool:
    """Require an explicit latest user instruction or yes to an assistant's cart offer."""
    if not messages or messages[-1].get("role") != "user":
        return False
    text = str(messages[-1].get("content") or "").casefold().strip()
    if not text or re.search(r"\b(?:не|нет|отмена|отменить|без|жоқ|жок)\b.{0,40}(?:добав|клад|полож|корзин|қос|кос|себет)", text):
        return False
    if re.search(r"\b(?:қоспа\w*|коспа\w*)\b", text):
        return False
    explicit_add = re.search(r"\b(?:добав(?:ь|ьте|ить|ляем)|полож(?:и|ите|ить)|клади|беру|возьму|қос\w*|кос\w*)\b", text)
    explicit_add = explicit_add or re.search(r"\bсебетке\b.{0,20}\bсал\w*\b", text)
    if explicit_add:
        return not text.endswith("?")
    if re.fullmatch(r"(?:да|ага|ок|окей|подтверждаю|согласен|согласна|иә|иа)[.!\s]*", text):
        previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
        return bool(previous and re.search(r"корзин|добав|себет|қос|кос", str(previous.get("content") or "").casefold()))
    return False


def cart_intent_matches(messages: list[dict], product: dict, qty: int, catalog: Catalog) -> bool:
    """Bind the model's article and quantity to the client's latest instruction."""
    def mentioned_articles(text: str) -> list[str]:
        return [p["article"] for p in catalog.products if re.search(rf"(?<![\w-]){re.escape(p['article'].casefold())}(?![\w-])", text)]

    latest = str(messages[-1]["content"]).casefold()
    mentioned = mentioned_articles(latest)
    if mentioned:
        if mentioned != [product["article"]]:
            return False
    elif product["name"].casefold() not in latest:
        replies = [str(m.get("content") or "").casefold() for m in reversed(messages[:-1]) if m.get("role") == "assistant"]
        direct_offer = bool(
            replies
            and re.search(r"корзин|добав|себет|қос|кос", replies[0])
            and (mentioned_articles(replies[0]) == [product["article"]] or product["name"].casefold() in replies[0])
        )
        generic_words = {"автоматический", "выключатель", "светодиодная", "светодиодный", "лампа", "товар"}
        distinguishing = [word for word in re.findall(r"[^\W\d_]{4,}", product["name"].casefold()) if word not in generic_words and word in latest]
        contextual_name = False
        for reply in replies:
            offered = mentioned_articles(reply)
            named = [article for article in offered if any(word in catalog.get(article)["name"].casefold() for word in distinguishing)]
            if named == [product["article"]]:
                contextual_name = True
                break
        if not direct_offer and not contextual_name:
            return False
    without_article = latest.replace(product["article"].casefold(), " ")
    quantities = re.findall(r"\b(\d+)\s*(?:штук(?:и|а)?|шт\.?|единиц(?:ы|а)?|дана)(?=\W|$)", without_article)
    if not quantities:
        quantities = re.findall(r"\b(?:добав\w*|полож\w*|беру|возьму|қос\w*|кос\w*)\s+(\d+)\b", without_article)
    if not quantities and re.fullmatch(r"(?:да|ага|ок|окей|подтверждаю|согласен|согласна|иә|иа)[.!\s]*", latest):
        previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
        if previous:
            quantities = re.findall(r"\b(\d+)\s*(?:штук(?:и|а)?|шт\.?|единиц(?:ы|а)?|дана)(?=\W|$)", str(previous.get("content") or "").casefold())
    if len(set(quantities)) > 1 or qty != (int(quantities[0]) if quantities else 1):
        return False
    return True


class ShopTools:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.carts: dict[str, dict[str, int]] = {}
        self.processed_additions: set[tuple[str, str, str]] = set()
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
        path = asset_path("purchase_conditions.txt") if self.catalog.demo_mode else BASE_DIR / "purchase_conditions.txt"
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
        if not cart_intent_matches(messages, product, qty, self.catalog):
            return {"error": "Подтвердите конкретный товар и количество перед добавлением", "article": article}
        turn = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        addition_key = (session_id, product["article"], turn)
        with self.lock:
            existing = self.carts.get(session_id, {}).get(product["article"], 0)
            if addition_key in self.processed_additions:
                return {"ok": True, "article": product["article"], "qty": existing, "cart": self.get_cart(session_id), "cart_link": CART_LINK}
            stock = product["stock"]
            if stock is None:
                return {"error": "Остаток товара неизвестен; добавление недоступно", "article": article}
            if existing + qty > stock:
                return {"error": "Недостаточно товара на складе", "article": article, "available": max(0, stock - existing)}
            self.carts.setdefault(session_id, {})[product["article"]] = existing + qty
            self.processed_additions.add(addition_key)
        return {"ok": True, "article": product["article"], "qty": existing + qty, "cart": self.get_cart(session_id), "cart_link": CART_LINK}

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
                if "user_confirmation" in args:
                    phrase = args["user_confirmation"]
                    if not isinstance(phrase, str) or not phrase.strip() or phrase.casefold() not in str(messages[-1].get("content") or "").casefold():
                        return {"error": "Подтверждение должно дословно присутствовать в последнем сообщении клиента"}
                return self.add_to_cart(session_id, str(args["article"]), args["qty"], messages)
            if name == "get_cart":
                return {"cart": self.get_cart(session_id), "cart_link": CART_LINK}
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

    system_prompt = asset_path("system_prompt.txt").read_text(encoding="utf-8")
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
    if contains_payment_data(text):
        return "Не отправляйте платёжные данные в чат. Оплата проходит только на сайте при оформлении заказа."
    lower = text.casefold()
    articles = [p["article"] for p in tools.catalog.products if p["article"].casefold() in lower]
    if purchase_confirmed(messages):
        if not articles:
            for previous in reversed(messages[:-1]):
                if previous.get("role") != "assistant":
                    continue
                previous_text = str(previous.get("content") or "").casefold()
                found = [p["article"] for p in tools.catalog.products if p["article"].casefold() in previous_text]
                named = [article for article in found if any(word in lower for word in re.findall(r"[^\W\d_]{5,}", tools.catalog.get(article)["name"].casefold()) if word not in {"автоматический", "выключатель", "светодиодная", "светодиодный"})]
                if len(named) == 1:
                    articles = named
                    break
                if len(found) == 1:
                    articles = found
                    break
        if len(articles) != 1:
            return "Укажите один артикул товара и подтвердите добавление, например: «Добавь 2 шт DEMO-AV-16»."
        quantity_match = re.search(r"\b(\d+)\s*(?:штук(?:и|а)?|шт\.?|единиц(?:ы|а)?|дана)(?=\W|$)", lower)
        if not quantity_match and re.fullmatch(r"(?:да|ага|ок|окей|подтверждаю|согласен|согласна|иә|иа)[.!\s]*", lower):
            previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), None)
            if previous:
                quantity_match = re.search(r"\b(\d+)\s*(?:штук(?:и|а)?|шт\.?|единиц(?:ы|а)?|дана)(?=\W|$)", str(previous.get("content") or "").casefold())
        qty = int(quantity_match.group(1)) if quantity_match else 1
        result = tools.dispatch("add_to_cart", {"article": articles[0], "qty": qty}, session_id, messages)
        if "error" in result:
            return f"Не удалось добавить товар: {result['error']}. Доступно: {result.get('available', 'неизвестно')}."
        return f"Добавлено в корзину: {articles[0]}, {qty} шт. Локальная корзина не синхронизируется с сайтом EKT. Ссылка: {CART_LINK}"
    if "корзин" in lower or "себет" in lower:
        cart = tools.dispatch("get_cart", {}, session_id, messages)["cart"]
        return ("В корзине: " + "; ".join(f"{p['name']} — {p['qty']} шт." for p in cart) if cart else "Корзина пуста.") + f" Ссылка: {CART_LINK} (локальная корзина с сайтом не синхронизируется)."
    if any(word in lower for word in ("достав", "оплат", "услов", "покуп")):
        return tools.dispatch("get_purchase_conditions", {}, session_id, messages)
    if "аналог" in lower:
        if not articles:
            for previous in reversed(messages[:-1]):
                if previous.get("role") == "assistant":
                    found = [p["article"] for p in tools.catalog.products if p["article"].casefold() in str(previous.get("content") or "").casefold()]
                    if len(found) == 1:
                        articles = found
                        break
        if articles:
            analogs = tools.dispatch("find_analogs", {"article": articles[0]}, session_id, messages)
            if analogs:
                options = "; ".join(f"{p['name']} ({p['article']}), {p['price']} ₸" for p in analogs[:3])
                return f"Подходящие аналоги в наличии: {options}. Они из той же категории; проверьте характеристики перед выбором. Какой добавить в корзину?"
            return "Аналогов в наличии не найдено."
    if articles:
        p = tools.dispatch("get_product", {"article": articles[0]}, session_id, messages)
        if "error" not in p:
            stock = p["stock"] if p["stock"] is not None else "неизвестен"
            return f"{p['name']} ({p['article']}): {p['price']} ₸, остаток: {stock}. " + ("Могу подобрать аналог." if p["stock"] == 0 else "Добавление недоступно до уточнения остатка." if p["stock"] is None else "Добавить в корзину?")
    query = next((word for word in ("лампа", "светодиод", "автомат", "выключатель") if word in lower), "")
    if query:
        brand = next((brand for brand in ("abb", "schneider", "iek", "siemens") if brand in lower), "")
        rating = re.search(r"\b\d+\s*[аa]\b", lower)
        query = " ".join(part for part in (query, brand, rating.group() if rating else "") if part)
    results = tools.dispatch("search_products", {"query": query}, session_id, messages) if query else []
    if results:
        if len(results) == 1:
            p = results[0]
            stock = p["stock"] if p["stock"] is not None else "неизвестен"
            return f"{p['name']} ({p['article']}): {p['price']} ₸, остаток: {stock}. " + ("Могу подобрать аналог." if p["stock"] == 0 else "Добавление недоступно до уточнения остатка." if p["stock"] is None else "Добавить в корзину?")
        return "Нашёл товары: " + "; ".join(f"{p['name']} ({p['article']}), {p['price']} ₸, остаток {p['stock']}" for p in results) + ". Назовите артикул для подробностей."
    return "Могу найти лампу или автоматический выключатель, показать аналоги, условия покупки и корзину. Назовите товар или артикул."
