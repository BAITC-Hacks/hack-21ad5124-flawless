"""Validate model UI intentions and build safe, localized chat actions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, get_args

from pydantic import BaseModel

from catalog import Catalog


ActionType = Literal[
    "add_to_cart", "show_analogs", "show_details", "show_availability",
    "show_certificates", "compare", "delivery", "payment", "cheaper_options",
    "other_brand", "clarify", "contact_manager", "continue_search",
    "change_quantity", "remove_from_cart", "clear_cart",
]
ACTION_TYPES = get_args(ActionType)
ARTICLE_ACTIONS = {
    "add_to_cart", "show_analogs", "show_details", "show_availability",
    "show_certificates", "cheaper_options", "other_brand", "change_quantity",
    "remove_from_cart",
}
MAX_ACTIONS = 6


class ChatAction(BaseModel):
    type: ActionType
    label: str
    message: str
    article: str | None = None
    qty: int | None = None
    max_qty: int | None = None


COPY = {
    "ru": {
        "add_to_cart": ("Добавить", "Добавь {qty} шт {article} в корзину"),
        "show_analogs": ("Похожие варианты", "Покажи аналоги для {article}"),
        "show_details": ("Характеристики", "Покажи характеристики товара {article}"),
        "show_availability": ("Наличие по городам", "Покажи наличие товара {article} по складам"),
        "show_certificates": ("Сертификаты", "Покажи сертификаты товара {article}"),
        "compare": ("Сравнить", "Сравни найденные товары по характеристикам"),
        "delivery": ("Доставка", "Какие условия доставки?"),
        "payment": ("Оплата", "Какие способы оплаты доступны?"),
        "cheaper_options": ("Подобрать дешевле", "Подбери более дешёвый вариант для {article}"),
        "other_brand": ("Другой производитель", "Покажи похожий товар другого производителя вместо {article}"),
        "clarify": ("Уточнить параметры", "Помоги уточнить параметры товара"),
        "contact_manager": ("Спросить менеджера", "Как связаться с менеджером EKT?"),
        "continue_search": ("Продолжить поиск", "Помоги найти другой товар"),
        "change_quantity": ("Изменить количество", "Измени количество {article} в корзине на {qty} шт"),
        "remove_from_cart": ("Удалить товар", "Удали {article} из корзины"),
        "clear_cart": ("Очистить корзину", "Очисти корзину"),
    },
    "kk": {
        "add_to_cart": ("Себетке қосу", "{article} тауарынан {qty} дана себетке қос"),
        "show_analogs": ("Ұқсас нұсқалар", "{article} тауарына ұқсас нұсқаларды көрсет"),
        "show_details": ("Сипаттамалар", "{article} тауарының сипаттамаларын көрсет"),
        "show_availability": ("Қалалардағы қор", "{article} тауарының қоймалардағы қорын көрсет"),
        "show_certificates": ("Сертификаттар", "{article} тауарының сертификаттарын көрсет"),
        "compare": ("Салыстыру", "Табылған тауарлардың сипаттамаларын салыстыр"),
        "delivery": ("Жеткізу", "Жеткізу шарттары қандай?"),
        "payment": ("Төлем", "Қандай төлем тәсілдері бар?"),
        "cheaper_options": ("Арзанырақ нұсқа", "{article} тауарына арзанырақ балама тап"),
        "other_brand": ("Басқа өндіруші", "{article} орнына басқа өндірушінің ұқсас тауарын көрсет"),
        "clarify": ("Параметрлерді нақтылау", "Тауар параметрлерін нақтылауға көмектес"),
        "contact_manager": ("Менеджерден сұрау", "EKT менеджерімен қалай байланысуға болады?"),
        "continue_search": ("Іздеуді жалғастыру", "Басқа тауар табуға көмектес"),
        "change_quantity": ("Санын өзгерту", "Себеттегі {article} тауар санын {qty} данаға өзгерт"),
        "remove_from_cart": ("Тауарды жою", "{article} тауарын себеттен алып таста"),
        "clear_cart": ("Себетті тазалау", "Себетті тазала"),
    },
}


@dataclass
class ActionState:
    """Facts the model actually received from tools during this answer."""

    proposed: list[dict] = field(default_factory=list)
    called: bool = False
    observed_articles: set[str] = field(default_factory=set)

    def observe(self, name: str, args: dict, result: object) -> None:
        if name in {"search_products", "find_analogs"} and isinstance(result, list):
            self.observed_articles.update(item["article"] for item in result if isinstance(item, dict) and isinstance(item.get("article"), str))
            if name == "find_analogs" and isinstance(args.get("article"), str):
                self.observed_articles.add(args["article"])
        elif name == "get_product" and isinstance(result, dict) and "error" not in result:
            self.observed_articles.add(result["article"])
        elif name in {"add_to_cart", "change_quantity", "remove_from_cart"} and isinstance(result, dict) and result.get("ok"):
            if isinstance(result.get("article"), str):
                self.observed_articles.add(result["article"])
        elif name == "get_cart" and isinstance(result, dict):
            self.observed_articles.update(item["article"] for item in result.get("cart", []) if isinstance(item, dict) and isinstance(item.get("article"), str))


def answer_language(reply: str) -> Literal["ru", "kk"]:
    return "kk" if re.search(r"[әғқңөұүһі]|\b(?:қанша|қалай|керек|рахмет|иә|жоқ|дана|себет|тауар|бар)\b", reply.casefold()) else "ru"


def _mentioned_articles(text: str, catalog: Catalog) -> list[str]:
    lowered = text.casefold()
    return [product["article"] for product in catalog.products
            if re.search(rf"(?<![\w-]){re.escape(product['article'].casefold())}(?![\w-])", lowered)]


def demo_action_proposals(latest: str, reply: str, catalog: Catalog, cart_before: list[dict], cart_after: list[dict]) -> list[dict]:
    """Offer a small deterministic set when no model action tool was used."""
    before = {item["article"]: item["qty"] for item in cart_before}
    changed = [item["article"] for item in cart_after if item["qty"] > before.get(item["article"], 0)]
    if changed:
        return [{"type": "change_quantity", "article": changed[0]},
                {"type": "remove_from_cart", "article": changed[0]}, {"type": "continue_search"}]

    latest_lower = latest.casefold()
    if any(word in latest_lower for word in ("достав", "оплат", "услов", "жеткіз", "төлем")):
        return [{"type": "delivery"}, {"type": "payment"}, {"type": "contact_manager"}]

    mentioned = _mentioned_articles(latest, catalog) or _mentioned_articles(reply, catalog)
    if len(mentioned) == 1:
        article = mentioned[0]
        product = catalog.get(article)
        if product and product["stock"] is not None and product["stock"] > 0:
            return [{"type": kind, "article": article} for kind in
                    ("add_to_cart", "show_analogs", "show_details", "show_availability", "show_certificates")]
        return [{"type": kind, "article": article} for kind in
                ("show_analogs", "show_availability", "other_brand")] + [{"type": "continue_search"}]
    if len(mentioned) >= 2:
        return [{"type": "compare"}, {"type": "clarify"}, {"type": "continue_search"}]
    return [{"type": "clarify"}, {"type": "contact_manager"}, {"type": "continue_search"}]


def _brand(product: dict) -> str:
    specs = product.get("characteristics") or {}
    if isinstance(specs, dict):
        for key, value in specs.items():
            if any(word in str(key).casefold() for word in ("brand", "бренд", "марка", "manufacturer")) and isinstance(value, str):
                return value.casefold().strip()
    return ""


def build_actions(proposals: list[dict], reply: str, latest: str, catalog: Catalog,
                  cart_before: list[dict], cart_after: list[dict], observed_articles: set[str] | None = None) -> list[ChatAction]:
    """Allowlist actions and derive every user-facing field from trusted data."""
    language = answer_language(reply)
    reply_lower = reply.casefold()
    observed = {article.casefold() for article in observed_articles} if observed_articles is not None else None
    cart = {item["article"]: item["qty"] for item in cart_after}
    prior = {item["article"]: item["qty"] for item in cart_before}
    just_added = any(qty > prior.get(article, 0) for article, qty in cart.items())
    visible = _mentioned_articles(latest + " " + reply, catalog)
    if observed is not None:
        visible = [article for article in visible if article.casefold() in observed]
    candidates = set(visible) | ({article for article in observed_articles} if observed_articles is not None else set())
    result: list[ChatAction] = []
    seen_types: set[str] = set()

    for raw in proposals[:30]:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind not in ACTION_TYPES or kind in seen_types:
            continue
        if just_added and kind not in {"change_quantity", "remove_from_cart", "clear_cart", "continue_search"}:
            continue
        article = raw.get("article") if isinstance(raw.get("article"), str) else None
        if kind in ARTICLE_ACTIONS:
            if article is None and len(candidates) == 1:
                article = next(iter(candidates))
            product = catalog.get(article) if article else None
            if not product or not re.fullmatch(r"[\w./+\-]{1,100}", product["article"]):
                continue
            article = product["article"]
            if observed is not None and article.casefold() not in observed:
                continue
        else:
            product = None
            article = None

        max_qty = None
        qty = None
        if kind == "add_to_cart":
            stock = product["stock"]
            max_qty = stock - cart.get(article, 0) if type(stock) is int else 0
            if max_qty <= 0:
                continue
            qty = 1
        elif kind == "show_analogs":
            analogs = catalog.analogs(article)
            if not analogs or ("аналог" in reply_lower or "ұқсас" in reply_lower) and any(
                    analog["article"].casefold() in reply_lower for analog in analogs):
                continue
        elif kind == "show_details":
            specs = product.get("characteristics") or {}
            if isinstance(specs, dict) and any(str(key).casefold() in reply_lower for key in specs if key):
                continue
        elif kind == "show_availability":
            stores = product.get("stores") or []
            if product["stock"] is None or not stores or any(
                    str(store.get("name", "")).casefold() in reply_lower
                    for store in stores if isinstance(store, dict) and store.get("name")):
                continue
        elif kind == "show_certificates":
            if not product.get("certificates") or "сертификат" in reply_lower:
                continue
        elif kind == "compare":
            if len(candidates) < 2 or "сравн" in reply_lower or "салыстыр" in reply_lower:
                continue
        elif kind == "cheaper_options":
            price = product.get("price")
            if price is None or not any(p["category"] == product["category"] and p["stock"] and
                                        p["price"] is not None and p["price"] < price for p in catalog.products):
                continue
            if "дешевле" in reply_lower or "арзанырақ" in reply_lower:
                continue
        elif kind == "other_brand":
            brand = _brand(product)
            if not brand or not any(p["category"] == product["category"] and p["stock"] and
                                    _brand(p) and _brand(p) != brand for p in catalog.products):
                continue
        elif kind == "delivery" and ("достав" in reply_lower or "жеткіз" in reply_lower):
            continue
        elif kind == "payment" and ("оплат" in reply_lower or "төлем" in reply_lower):
            continue
        elif kind == "contact_manager" and re.search(r"\+\d[\d\s()\-]{7,}|@[\w.-]+", reply):
            continue
        elif kind == "change_quantity":
            if article not in cart or not product or type(product["stock"]) is not int or product["stock"] < 1:
                continue
            qty, max_qty = cart[article], product["stock"]
        elif kind == "remove_from_cart" and article not in cart:
            continue
        elif kind == "clear_cart" and not cart:
            continue

        label, message = COPY[language][kind]
        message = message.replace("{article}", article or "")
        result.append(ChatAction(type=kind, label=label, message=message, article=article, qty=qty, max_qty=max_qty))
        seen_types.add(kind)
        if len(result) == MAX_ACTIONS:
            break
    return result
