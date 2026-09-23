"""Tool dispatch, cart rules and a deterministic offline demo assistant."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import OrderedDict
from threading import RLock
from typing import NamedTuple

from catalog import BASE_DIR, Catalog, asset_path
from guardrails import cart_success_reply, detect_language, guard_model_reply, requests_internal_instructions, safe_reply
from ui_actions import ACTION_TYPES, MAX_UI_QUANTITY, ActionState

CART_LINK = "https://ekt.kz/cart"
MAX_CART_QUANTITY = MAX_UI_QUANTITY
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6
MAX_TOOL_RESULT_CHARS = 12_000
OPENAI_TIMEOUT_SECONDS = 6.0
MAX_PROCESSED_ADDITIONS = 10_000


class QuantityParse(NamedTuple):
    """A quantity found in text, or an explicit reason not to default it."""

    status: str
    value: int | None = None


_RU_QUANTITY_WORDS = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}
_KZ_QUANTITY_WORDS = {
    "бір": 1,
    "екі": 2,
    "үш": 3,
    "төрт": 4,
    "бес": 5,
    "алты": 6,
    "жеті": 7,
    "сегіз": 8,
    "тоғыз": 9,
    "он": 10,
}
_QUANTITY_WORDS = {**_RU_QUANTITY_WORDS, **_KZ_QUANTITY_WORDS}
_WORD_QUANTITY_PATTERN = "|".join(sorted(map(re.escape, _QUANTITY_WORDS), key=len, reverse=True))
_RU_WORD_QUANTITY_PATTERN = "|".join(sorted(map(re.escape, _RU_QUANTITY_WORDS), key=len, reverse=True))
_QUANTITY_UNIT_PATTERN = r"(?:штуку|штука|штуки|штук|шт\.?|единицу|единица|единицы|единиц|ед\.?|дана)"
_DIRECT_VERB_PATTERN = (
    r"(?:добав(?:ь|ьте|ляй|ляйте|ляем|ить)|полож(?:и|ите|им|ить)|клад(?:и|ите|ём|ем)|"
    r"оформ(?:и|ите|ляй|ляйте|ляем|ить)|(?:за)?кид(?:ай|айте|ывай|ывайте|нуть)|"
    r"беру|берём|берем|возьму|возьмём|возьмем|"
    r"қос(?:шы|ыңыз|ыңдар|айық|амын)?|кос(?:шы|ыңыз|ыңдар|айық|амын)?|"
    r"себетке\s+сал(?:шы|ыңыз|ыңдар)?)"
)
_DIRECT_ACTION_RE = re.compile(rf"(?<!\w){_DIRECT_VERB_PATTERN}(?!\w)", re.IGNORECASE)
_THROW_ACTION_RE = re.compile(r"(?<!\w)(?:за)?кид(?:ай|айте|ывай|ывайте|нуть)(?!\w)", re.IGNORECASE)
_CART_MARKER_RE = re.compile(r"(?<!\w)(?:корзин\w*|себет\w*)(?!\w)", re.IGNORECASE)
_QUANTITY_WITH_UNIT_RE = re.compile(
    rf"(?<![\w-])(?P<value>[+-]?\d+(?:[.,]\d+)?|{_WORD_QUANTITY_PATTERN})\s*"
    rf"(?P<unit>{_QUANTITY_UNIT_PATTERN})(?!\w)",
    re.IGNORECASE,
)
_UNIT_RE = re.compile(rf"(?<!\w)(?P<unit>{_QUANTITY_UNIT_PATTERN})(?!\w)", re.IGNORECASE)
_BARE_AFTER_VERB_RE = re.compile(
    rf"(?<!\w){_DIRECT_VERB_PATTERN}(?!\w)"
    rf"(?:\s+(?:в\s+(?:корзину|корзинку)|себетке))?\s*[,;:]?\s*"
    rf"(?:пожалуйста\s*[,]?\s*)?"
    rf"(?P<value>[+-]?\d+(?:[.,]\d+)?|{_RU_WORD_QUANTITY_PATTERN})(?!\w)",
    re.IGNORECASE,
)
_UNSUPPORTED_QUANTITY_WORDS = {
    "ноль", "нуль", "оба", "обе", "пара", "пару", "несколько", "много", "мало",
    "десяток", "дюжина", "сотня", "сто", "тысяча", "тысячи", "тысяч", "миллион",
    "миллиона", "миллионов", "миллиард", "полтора", "полторы", "одиннадцать",
    "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
    "семнадцать", "восемнадцать", "девятнадцать", "двадцать", "тридцать", "сорок",
    "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто", "двести",
    "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот",
    "миллиард", "миллиарда", "миллиардов", "триллион", "половина", "половину", "четверть",
    "минус", "плюс", "нөл", "жиырма", "отыз", "қырық", "елу", "алпыс", "жетпіс",
    "сексен", "тоқсан", "жүз", "мың", "бірнеше", "көп", "аз",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "hundred", "thousand", "million",
}
_UNSUPPORTED_QUANTITY_PATTERN = "|".join(
    sorted(map(re.escape, _UNSUPPORTED_QUANTITY_WORDS), key=len, reverse=True)
)
_UNSUPPORTED_AFTER_VERB_RE = re.compile(
    rf"(?<!\w){_DIRECT_VERB_PATTERN}(?!\w)"
    rf"(?:\s+(?:в\s+(?:корзину|корзинку)|себетке))?\s*[,;:]?\s*"
    rf"(?:пожалуйста\s*[,]?\s*)?(?:{_UNSUPPORTED_QUANTITY_PATTERN})(?!\w)",
    re.IGNORECASE,
)
_UNSUPPORTED_QUANTITY_RE = re.compile(
    rf"(?<!\w)(?:{_UNSUPPORTED_QUANTITY_PATTERN})(?!\w)", re.IGNORECASE
)
_UNSUPPORTED_KZ_COMPOSITE_RE = re.compile(
    rf"(?<!\w)он\s+(?:{_WORD_QUANTITY_PATTERN})(?!\w)", re.IGNORECASE
)
_AMBIGUOUS_QUANTITY_RE = re.compile(
    rf"(?<![\w-])(?:\d+|{_WORD_QUANTITY_PATTERN})\s*"
    rf"(?:[-–—/]|или|либо|до)\s*(?:\d+|{_WORD_QUANTITY_PATTERN})(?![\w-])",
    re.IGNORECASE,
)
_NUMBER_TOKEN_RE = re.compile(r"(?<![\w-])[+-]?\d+(?:[.,]\d+)?(?![\w-])")
_MEASUREMENT_AFTER_NUMBER_RE = re.compile(
    r"\s*(?:а|a|вт|w|к|k|v|вольт(?:а|ов)?|ампер(?:а|ов)?|мм|см|м|p|₸)(?!\w)",
    re.IGNORECASE,
)
_CANCELLATION_RE = re.compile(
    r"(?<!\w)(?:хотя\s+(?:нет|не)|нет|не\s+(?:надо|нужно|добав\w*|клад\w*|полож\w*|"
    r"оформ\w*|кида\w*|закидыва\w*)|передумал(?:а|и)?|отмена|отмени\w*|"
    r"жоқ|жок|керек\s+емес|қоспа\w*|коспа\w*|салма\w*)(?!\w)",
    re.IGNORECASE,
)
_QUESTION_REQUEST_RE = re.compile(
    r"(?<!\w)(?:можно|можешь|можете|сможешь|сможете|мог(?:ла|ли)?\s+бы|"
    r"могу|ли|добавишь|добавите|положишь|положите|оформишь|оформите|кинешь|кинете)(?!\w)",
    re.IGNORECASE,
)
_GENERIC_CONSENT_RE = re.compile(
    r"(?:да|ага|ок|окей|подтверждаю|согласен|согласна|конечно|ну\s+давай|да\s+конечно|"
    r"хорошо|ок\s+погнали|погнали|иә|ия|иа|әрине)[.!\s]*",
    re.IGNORECASE,
)
_UNSAFE_SELECTOR_RE = re.compile(
    r"(?<!\w)(?:перв(?:ый|ую|ого|ому)|втор(?:ой|ую|ого|ому)|трет(?:ий|ью|ьего|ьему)|"
    r"тот|та|ту|того|тому)(?!\w)",
    re.IGNORECASE,
)
_OFFER_REFERENCE_RE = re.compile(
    r"(?<!\w)(?:такой|такая|такую|таких|этот|эта|эту|этого|его|осындай)(?!\w)",
    re.IGNORECASE,
)
_ARTICLE_LIKE_RE = re.compile(r"(?<![\w-])[a-zа-яёәіңғүұқөһ0-9]+(?:-[a-zа-яёәіңғүұқөһ0-9]+)+(?![\w-])", re.IGNORECASE)
_EN_UI_ARTICLE_PATTERN = r"(?P<article>[\w./+\-]{1,100})"
_EN_ADD_BUTTON_RE = re.compile(
    rf"Add (?P<qty>\d{{1,5}}) × {_EN_UI_ARTICLE_PATTERN} to the cart",
    re.IGNORECASE,
)
_EN_CHANGE_BUTTON_RE = re.compile(
    rf"Change the quantity of {_EN_UI_ARTICLE_PATTERN} in the cart to (?P<qty>\d{{1,5}})",
    re.IGNORECASE,
)
_EN_REMOVE_BUTTON_RE = re.compile(
    rf"Remove {_EN_UI_ARTICLE_PATTERN} from the cart",
    re.IGNORECASE,
)
_EN_CLEAR_BUTTON_RE = re.compile(r"Clear the cart", re.IGNORECASE)
_DEMO_UI_QUESTION_REPLIES = {
    "Помоги уточнить параметры товара": "Что именно вы ищете? Укажите категорию товара, бренд и основные характеристики.",
    "Тауар параметрлерін нақтылауға көмектес": "Қандай тауар керек? Санатын, брендін және негізгі сипаттамаларын жазыңыз.",
    "Help me refine the product requirements": "What product do you need? Specify its category, brand, and key specifications.",
    "Помоги найти другой товар": "Какой другой товар найти? Укажите название, бренд или нужную характеристику.",
    "Басқа тауар табуға көмектес": "Қандай басқа тауарды табу керек? Атауын, брендін немесе сипаттамасын жазыңыз.",
    "Help me find another product": "What other product should I find? Specify a name, brand, or required specification.",
}


def _quantity_value(raw: str) -> int | None:
    raw = raw.casefold()
    if raw in _QUANTITY_WORDS:
        return _QUANTITY_WORDS[raw]
    if re.fullmatch(r"\d+", raw):
        if len(raw.lstrip("0")) > len(str(MAX_CART_QUANTITY)):
            return None
        value = int(raw)
        return value if 1 <= value <= MAX_CART_QUANTITY else None
    return None


def _english_button_values(pattern: re.Pattern, text: str) -> tuple[str, int | None] | None:
    """Parse only the exact English command emitted by a trusted UI button."""
    match = pattern.fullmatch(str(text or "").strip())
    if not match:
        return None
    quantity = match.groupdict().get("qty")
    return match.group("article"), _quantity_value(quantity) if quantity is not None else None


def parse_quantity(text: str) -> QuantityParse:
    """Parse a cart quantity without silently repairing unsafe or ambiguous input."""
    text = str(text or "").casefold()
    if not text:
        return QuantityParse("absent")
    if (
        _UNSUPPORTED_QUANTITY_RE.search(text)
        or _UNSUPPORTED_KZ_COMPOSITE_RE.search(text)
        or _AMBIGUOUS_QUANTITY_RE.search(text)
        or _UNSUPPORTED_AFTER_VERB_RE.search(text)
    ):
        return QuantityParse("invalid")
    standalone = re.fullmatch(rf"[+-]?\d+(?:[.,]\d+)?|{_WORD_QUANTITY_PATTERN}", text.strip())
    if standalone:
        value = _quantity_value(standalone.group())
        return QuantityParse("valid", value) if value is not None else QuantityParse("invalid")

    found: list[tuple[tuple[int, int], int]] = []
    unit_spans: list[tuple[int, int]] = []
    for match in _QUANTITY_WITH_UNIT_RE.finditer(text):
        value = _quantity_value(match.group("value"))
        if value is None:
            return QuantityParse("invalid")
        found.append((match.span(), value))
        unit_spans.append(match.span("unit"))

    for unit_match in _UNIT_RE.finditer(text):
        if any(start <= unit_match.start() and unit_match.end() <= end for start, end in unit_spans):
            continue
        unit = unit_match.group("unit").rstrip(".")
        if unit in {"штука", "штуку", "единица", "единицу"}:
            found.append((unit_match.span(), 1))
            continue
        # A plural/unit marker with an unknown word is an explicit but unparseable quantity.
        return QuantityParse("invalid")

    for match in _BARE_AFTER_VERB_RE.finditer(text):
        value_span = match.span("value")
        if any(start <= value_span[0] and value_span[1] <= end for (start, end), _ in found):
            continue
        value = _quantity_value(match.group("value"))
        if value is None:
            return QuantityParse("invalid")
        found.append((value_span, value))

    if _has_direct_action(text):
        for number_match in _NUMBER_TOKEN_RE.finditer(text):
            if any(start <= number_match.start() and number_match.end() <= end for (start, end), _ in found):
                continue
            if _MEASUREMENT_AFTER_NUMBER_RE.match(text, number_match.end()):
                continue
            # A number in an unsupported position must not silently turn into the default of one.
            return QuantityParse("invalid")

    if not found:
        return QuantityParse("absent")
    if len(found) != 1:
        return QuantityParse("invalid")
    return QuantityParse("valid", found[0][1])


def _has_cancellation(text: str) -> bool:
    return bool(_CANCELLATION_RE.search(text))


def _has_direct_action(text: str) -> bool:
    matches = list(_DIRECT_ACTION_RE.finditer(text))
    if not matches:
        return False
    if any(not _THROW_ACTION_RE.fullmatch(match.group()) for match in matches):
        return True
    return bool(_CART_MARKER_RE.search(text))


def _is_question_request(text: str) -> bool:
    return "?" in text or bool(_QUESTION_REQUEST_RE.search(text))


def _is_generic_consent(text: str) -> bool:
    return bool(_GENERIC_CONSENT_RE.fullmatch(text.strip()))


def _immediate_assistant_text(messages: list[dict]) -> str | None:
    if len(messages) < 2 or messages[-2].get("role") != "assistant":
        return None
    return str(messages[-2].get("content") or "").casefold()


def _mentioned_products(text: str, catalog: Catalog) -> list[dict]:
    mentioned: list[dict] = []
    for product in catalog.products:
        article = product["article"].casefold()
        name = product["name"].casefold().strip()
        if re.search(rf"(?<![\w-]){re.escape(article)}(?![\w-])", text) or (name and name in text):
            mentioned.append(product)
    return mentioned


def _has_positive_offer_language(text: str) -> bool:
    if _has_cancellation(text) or not _CART_MARKER_RE.search(text):
        return False
    if re.search(r"(?<!\w)(?:какой|какую|который|которую|қайсы)(?!\w)", text):
        return False
    return bool(re.search(
        r"(?<!\w)(?:добавить|добавим|могу\s+добавить|хотите\s+добавить|положить|"
        r"оформить|закинуть|қосайын|косайын|салайын)(?!\w)",
        text,
        re.IGNORECASE,
    ))


def _looks_like_concrete_offer_without_catalog(text: str) -> bool:
    articles = {match.group().casefold() for match in _ARTICLE_LIKE_RE.finditer(text)}
    if len(articles) == 1:
        return True
    if articles:
        return False
    stripped = re.sub(
        r"(?<!\w)(?:добавить|добавим|могу|хотите|положить|оформить|закинуть|"
        r"қосайын|косайын|салайын|корзин\w*|себет\w*|ба|ли)(?!\w)",
        " ",
        text,
    )
    stripped = re.sub(rf"(?<!\w)(?:{_WORD_QUANTITY_PATTERN}|{_QUANTITY_UNIT_PATTERN})(?!\w)", " ", stripped)
    words = [
        word for word in re.findall(r"[^\W_]+", stripped, re.UNICODE)
        if word not in {"этот", "эта", "эту", "товар", "товара", "его", "туда", "в", "на", "и", "а"}
    ]
    return len(words) >= 2 and (len(words) >= 3 or any(any(char.isdigit() for char in word) for word in words))


def _offer_target(messages: list[dict], catalog: Catalog | None = None) -> dict | bool | None:
    text = _immediate_assistant_text(messages)
    if not text or not _has_positive_offer_language(text):
        return None
    if catalog is None:
        return True if _looks_like_concrete_offer_without_catalog(text) else None
    mentioned = _mentioned_products(text, catalog)
    return mentioned[0] if len(mentioned) == 1 else None


_CONTEXT_STOP_WORDS = {
    "добавь", "добавьте", "добавляй", "добавляйте", "добавить", "положи", "положите",
    "клади", "оформи", "оформляй", "оформить", "кидай", "закидывай", "беру", "берём",
    "берем", "возьму", "корзину", "корзина", "корзине", "себетке", "қос", "кос", "сал",
    "штук", "штука", "штуки", "штуку", "единица", "единицы", "единиц", "дана", "да",
    "ага", "ок", "окей", "пожалуйста", "товар", "товара", "лампа", "автоматический",
    "выключатель", "светодиодная", "светодиодный", "таких", "такой", "этот", "его",
    *_QUANTITY_WORDS.keys(),
}


def _contextual_product(messages: list[dict], text: str, catalog: Catalog) -> dict | None:
    words = {
        word for word in re.findall(r"[^\W\d_]{3,}", text, re.UNICODE)
        if word not in _CONTEXT_STOP_WORDS
    }
    if not words:
        return None
    assistant_history = [
        str(message.get("content") or "").casefold()
        for message in messages[:-1]
        if message.get("role") == "assistant"
    ]
    previously_mentioned = {
        product["article"]
        for reply in assistant_history
        for product in _mentioned_products(reply, catalog)
    }
    candidates = []
    for product in catalog.products:
        searchable = f"{product['article']} {product['name']}".casefold()
        if not any(word in searchable for word in words):
            continue
        if product["article"] in previously_mentioned:
            candidates.append(product)
    return candidates[0] if len(candidates) == 1 else None


def _only_action_quantity_and_fillers(text: str) -> bool:
    cleaned = _DIRECT_ACTION_RE.sub(" ", text)
    cleaned = _CART_MARKER_RE.sub(" ", cleaned)
    cleaned = _QUANTITY_WITH_UNIT_RE.sub(" ", cleaned)
    cleaned = re.sub(rf"(?<!\w)(?:{_WORD_QUANTITY_PATTERN})(?!\w)", " ", cleaned)
    cleaned = re.sub(r"(?<!\w)\d+(?!\w)", " ", cleaned)
    cleaned = re.sub(
        r"(?<!\w)(?:да|ага|ок|окей|ну|пожалуйста|возьми|только|в|на|туда|мне|же)(?!\w)",
        " ",
        cleaned,
    )
    return not re.search(r"[^\W_]", cleaned, re.UNICODE)


def _resolve_requested_product(messages: list[dict], catalog: Catalog) -> tuple[dict | None, str]:
    text = str(messages[-1].get("content") or "").casefold()
    if _UNSAFE_SELECTOR_RE.search(text):
        return None, "ambiguous"
    mentioned = _mentioned_products(text, catalog)
    if len(mentioned) != 1:
        if mentioned:
            return None, "ambiguous"
    else:
        return mentioned[0], "explicit"
    contextual = _contextual_product(messages, text, catalog)
    if contextual:
        return contextual, "context"
    offered = _offer_target(messages, catalog)
    may_use_offer = (
        _is_generic_consent(text)
        or bool(_OFFER_REFERENCE_RE.search(text))
        or (_has_direct_action(text) and _only_action_quantity_and_fillers(text))
    )
    if isinstance(offered, dict) and may_use_offer:
        return offered, "offer"
    return None, "missing"


def _offer_quantity_text(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"[.!;\n]+", text) if part.strip()]
    clause = next((part for part in reversed(sentences) if _has_positive_offer_language(part)), text)
    russian_action = re.search(r"(?<!\w)(?:добавить|добавим|положить|оформить|закинуть)(?!\w)", clause)
    return clause[russian_action.start():] if russian_action else clause


def _without_product_identity(text: str, product: dict | None) -> str:
    text = str(text or "").casefold()
    if not product:
        return text
    name = str(product.get("name") or "").casefold().strip()
    if name:
        text = text.replace(name, " ")
    article = str(product.get("article") or "").casefold().strip()
    if article:
        text = re.sub(rf"(?<![\w-]){re.escape(article)}(?![\w-])", " ", text)
    return text


def _expected_quantity(messages: list[dict], source: str, product: dict | None = None) -> QuantityParse:
    parsed = parse_quantity(_without_product_identity(messages[-1].get("content"), product))
    if parsed.status != "absent":
        return parsed
    if source == "offer":
        offer_text = _immediate_assistant_text(messages)
        if offer_text:
            offered = parse_quantity(_without_product_identity(_offer_quantity_text(offer_text), product))
            if offered.status != "absent":
                return offered
    return QuantityParse("valid", 1)


def contains_payment_data(text: str) -> bool:
    canonical = unicodedata.normalize("NFKC", str(text or ""))
    canonical = "".join(
        character
        for character in canonical
        if not unicodedata.category(character).startswith("C")
    )
    pan_scan = "".join(
        " " if unicodedata.category(character).startswith("Z") or character in "-–—−" else character
        for character in canonical
    )
    return bool(
        re.search(r"(?<!\d)(?:\d[\s-]*){13,19}(?!\d)", pan_scan)
        or re.search(r"\b(?:cvv|cvc|код\s+из\s+смс|sms\s*код)\b", canonical, re.IGNORECASE)
        or re.search(
            r"\b(?:пароль|password|passwd|passcode)\b\s*(?::|=|—|-)\s*\S+",
            canonical,
            re.IGNORECASE,
        )
        or re.search(r"\bмой\s+пароль\s+\S+", canonical, re.IGNORECASE)
    )


def product_brief(product: dict) -> dict:
    return {key: product.get(key) for key in ("article", "name", "category", "price", "stock", "availability", "url")}


def purchase_confirmed(messages: list[dict], catalog: Catalog | None = None) -> bool:
    """Require an unequivocal latest-user command bound to a safe quantity and target."""
    if not messages or messages[-1].get("role") != "user":
        return False
    text = str(messages[-1].get("content") or "").casefold().strip()
    if not text or _has_cancellation(text) or _is_question_request(text) or _UNSAFE_SELECTOR_RE.search(text):
        return False
    english_button = _english_button_values(_EN_ADD_BUTTON_RE, text)
    if english_button:
        article, quantity = english_button
        if quantity is None:
            return False
        return catalog is None or catalog.get(article) is not None
    if catalog is None and parse_quantity(text).status == "invalid":
        return False
    offered = _offer_target(messages, catalog)
    if _is_generic_consent(text):
        if not offered:
            return False
        if catalog is not None:
            target, source = _resolve_requested_product(messages, catalog)
            return bool(target and _expected_quantity(messages, source, target).status == "valid")
        offer_text = _immediate_assistant_text(messages)
        return bool(offer_text and parse_quantity(_offer_quantity_text(offer_text)).status != "invalid")
    if not _has_direct_action(text):
        return False
    if _OFFER_REFERENCE_RE.search(text) and not offered:
        return False
    if catalog is not None:
        target, source = _resolve_requested_product(messages, catalog)
        if target is None:
            return False
        expected = _expected_quantity(messages, source, target)
        return expected.status == "valid"
    return True


def cart_intent_matches(messages: list[dict], product: dict, qty: int, catalog: Catalog) -> bool:
    """Bind the model's article and quantity to the client's latest instruction."""
    if not messages or messages[-1].get("role") != "user":
        return False
    english_button = _english_button_values(
        _EN_ADD_BUTTON_RE,
        str(messages[-1].get("content") or ""),
    )
    if english_button:
        article, quantity = english_button
        return article.casefold() == product["article"].casefold() and quantity == qty
    target, source = _resolve_requested_product(messages, catalog)
    if target is None or target["article"] != product["article"]:
        return False
    expected = _expected_quantity(messages, source, target)
    return expected.status == "valid" and expected.value == qty


class ShopTools:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.carts: dict[str, dict[str, int]] = {}
        self.processed_additions: OrderedDict[tuple[str, str, int, str], None] = OrderedDict()
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
        # The logic-owned policy is the single source of truth in both catalog modes.
        path = asset_path("purchase_conditions.txt")
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
        if not purchase_confirmed(messages, self.catalog):
            return {"error": "Нужно явное подтверждение клиента на добавление товара в корзину"}
        if type(qty) is not int or not 1 <= qty <= MAX_CART_QUANTITY:
            return {"error": f"Количество должно быть целым числом от 1 до {MAX_CART_QUANTITY}"}
        product = self.catalog.get(article)
        if not product:
            return {"error": "Товар не найден", "article": article}
        if not cart_intent_matches(messages, product, qty, self.catalog):
            return {"error": "Подтвердите конкретный товар и количество перед добавлением", "article": article}
        latest_user = " ".join(str(messages[-1].get("content") or "").casefold().split())
        turn = hashlib.sha256(latest_user.encode("utf-8")).hexdigest()
        addition_key = (session_id, product["article"], qty, turn)
        with self.lock:
            existing = self.carts.get(session_id, {}).get(product["article"], 0)
            if addition_key in self.processed_additions:
                self.processed_additions.move_to_end(addition_key)
                return {
                    "ok": True,
                    "replayed": True,
                    "article": product["article"],
                    "added_qty": 0,
                    "qty": existing,
                    "cart": self.get_cart(session_id),
                    "cart_link": CART_LINK,
                }
            stock = product["stock"]
            if stock is None:
                return {"error": "Остаток товара неизвестен; добавление недоступно", "article": article}
            if existing + qty > stock:
                return {"error": "Недостаточно товара на складе", "article": article, "available": max(0, stock - existing)}
            self.carts.setdefault(session_id, {})[product["article"]] = existing + qty
            self.processed_additions[addition_key] = None
            while len(self.processed_additions) > MAX_PROCESSED_ADDITIONS:
                self.processed_additions.popitem(last=False)
        return {
            "ok": True,
            "replayed": False,
            "article": product["article"],
            "added_qty": qty,
            "qty": existing + qty,
            "cart": self.get_cart(session_id),
            "cart_link": CART_LINK,
        }

    def change_quantity(self, session_id: str, article: str, qty: int, messages: list[dict]):
        """Set an existing cart line only for an exact, explicit UI/user command."""
        product = self.catalog.get(article)
        if not product:
            return {"error": "Товар не найден", "article": article}
        if type(qty) is not int or not 1 <= qty <= MAX_CART_QUANTITY:
            return {"error": f"Количество должно быть целым числом от 1 до {MAX_CART_QUANTITY}"}
        latest = str(messages[-1].get("content") or "").strip() if messages and messages[-1].get("role") == "user" else ""
        escaped = re.escape(product["article"])
        russian = rf"Измени\s+количество\s+{escaped}\s+в\s+корзине\s+на\s+{qty}\s+шт[.! ]*"
        kazakh = rf"Себеттегі\s+{escaped}\s+тауар\s+санын\s+{qty}\s+данаға\s+өзгерт[.! ]*"
        english = f"Change the quantity of {product['article']} in the cart to {qty}"
        if not (
            re.fullmatch(russian, latest, re.IGNORECASE)
            or re.fullmatch(kazakh, latest, re.IGNORECASE)
            or latest.casefold() == english.casefold()
        ):
            return {"error": "Подтвердите конкретный товар и новое количество"}
        stock = product["stock"]
        if type(stock) is not int or qty > stock:
            return {"error": "Недостаточно товара на складе", "available": stock}
        with self.lock:
            if product["article"] not in self.carts.get(session_id, {}):
                return {"error": "Товара нет в корзине", "article": product["article"]}
            self.carts[session_id][product["article"]] = qty
        return {
            "ok": True,
            "article": product["article"],
            "qty": qty,
            "cart": self.get_cart(session_id),
            "cart_link": CART_LINK,
        }

    def remove_from_cart(self, session_id: str, article: str, messages: list[dict]):
        """Remove one exact cart line only after an explicit command."""
        product = self.catalog.get(article)
        if not product:
            return {"error": "Товар не найден", "article": article}
        latest = str(messages[-1].get("content") or "").strip() if messages and messages[-1].get("role") == "user" else ""
        escaped = re.escape(product["article"])
        russian = rf"Удали\s+{escaped}\s+из\s+корзины[.! ]*"
        kazakh = rf"{escaped}\s+тауарын\s+себеттен\s+алып\s+таста[.! ]*"
        english = f"Remove {product['article']} from the cart"
        if not (
            re.fullmatch(russian, latest, re.IGNORECASE)
            or re.fullmatch(kazakh, latest, re.IGNORECASE)
            or latest.casefold() == english.casefold()
        ):
            return {"error": "Подтвердите удаление конкретного товара"}
        with self.lock:
            cart = self.carts.get(session_id, {})
            if product["article"] not in cart:
                return {"error": "Товара нет в корзине", "article": product["article"]}
            cart.pop(product["article"])
            for key in list(self.processed_additions):
                if key[0] == session_id and key[1] == product["article"]:
                    self.processed_additions.pop(key, None)
        return {
            "ok": True,
            "article": product["article"],
            "cart": self.get_cart(session_id),
            "cart_link": CART_LINK,
        }

    def clear_cart(self, session_id: str, messages: list[dict]):
        """Clear the current session cart only after an exact explicit command."""
        latest = str(messages[-1].get("content") or "").casefold().strip() if messages and messages[-1].get("role") == "user" else ""
        localized_command = latest.rstrip(".! ") in {"очисти корзину", "себетті тазала"}
        if not localized_command and not _EN_CLEAR_BUTTON_RE.fullmatch(latest):
            return {"error": "Подтвердите очистку корзины"}
        with self.lock:
            self.carts[session_id] = {}
            for key in list(self.processed_additions):
                if key[0] == session_id:
                    self.processed_additions.pop(key, None)
        return {"ok": True, "cart": [], "cart_link": CART_LINK}

    def dispatch(self, name: str, args: dict, session_id: str, messages: list[dict]):
        try:
            if not isinstance(args, dict):
                raise TypeError
            if name == "search_products":
                if set(args) != {"query"}:
                    raise ValueError
                query = args["query"]
                if not isinstance(query, str) or not query.strip() or len(query) > 200:
                    raise ValueError
                return self.search_products(query.strip())
            if name == "get_product":
                if set(args) != {"article"}:
                    raise ValueError
                article = args["article"]
                if not isinstance(article, str) or not article.strip() or len(article) > 128:
                    raise ValueError
                return self.get_product(article.strip())
            if name == "find_analogs":
                if set(args) != {"article"}:
                    raise ValueError
                article = args["article"]
                if not isinstance(article, str) or not article.strip() or len(article) > 128:
                    raise ValueError
                return self.find_analogs(article.strip())
            if name == "get_purchase_conditions":
                if args:
                    raise ValueError
                return self.get_purchase_conditions()
            if name == "add_to_cart":
                # Always bind to the request session. The model cannot choose another cart.
                if not {"article", "qty"}.issubset(args) or not set(args).issubset({"article", "qty", "user_confirmation"}):
                    raise ValueError
                if "user_confirmation" in args:
                    phrase = args["user_confirmation"]
                    if not isinstance(phrase, str) or not phrase.strip() or phrase.casefold() not in str(messages[-1].get("content") or "").casefold():
                        return {"error": "Подтверждение должно дословно присутствовать в последнем сообщении клиента"}
                article = args["article"]
                if not isinstance(article, str) or not article.strip() or len(article) > 128:
                    raise ValueError
                return self.add_to_cart(session_id, article.strip(), args["qty"], messages)
            if name == "change_quantity":
                if set(args) != {"article", "qty"}:
                    raise ValueError
                article = args["article"]
                if not isinstance(article, str) or not article.strip() or len(article) > 128:
                    raise ValueError
                return self.change_quantity(session_id, article.strip(), args["qty"], messages)
            if name == "remove_from_cart":
                if set(args) != {"article"}:
                    raise ValueError
                article = args["article"]
                if not isinstance(article, str) or not article.strip() or len(article) > 128:
                    raise ValueError
                return self.remove_from_cart(session_id, article.strip(), messages)
            if name == "clear_cart":
                if args:
                    raise ValueError
                return self.clear_cart(session_id, messages)
            if name == "get_cart":
                if args:
                    raise ValueError
                return {"cart": self.get_cart(session_id), "cart_link": CART_LINK}
        except (KeyError, TypeError, ValueError):
            return {"error": "Некорректные аргументы инструмента"}
        return {"error": "Неизвестный инструмент"}


def _function_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    _function_tool(
        "search_products",
        "Искать товары по названию, артикулу или категории.",
        {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
        ["query"],
    ),
    _function_tool(
        "get_product",
        "Получить характеристики, сертификаты, цену и остатки товара по артикулу.",
        {"article": {"type": "string", "minLength": 1, "maxLength": 128}},
        ["article"],
    ),
    _function_tool(
        "find_analogs",
        "Найти имеющиеся в наличии товары той же категории.",
        {"article": {"type": "string", "minLength": 1, "maxLength": 128}},
        ["article"],
    ),
    _function_tool("get_purchase_conditions", "Получить условия покупки и доставки.", {}, []),
    _function_tool(
        "add_to_cart",
        "Добавить товар в локальную корзину только после явного подтверждения клиента. session_id берётся с сервера.",
        {
            "article": {"type": "string", "minLength": 1, "maxLength": 128},
            "qty": {"type": "integer", "minimum": 1, "maximum": MAX_CART_QUANTITY},
        },
        ["article", "qty"],
    ),
    _function_tool(
        "change_quantity",
        "Установить новое количество товара в корзине только по явной команде клиента.",
        {
            "article": {"type": "string", "minLength": 1, "maxLength": 128},
            "qty": {"type": "integer", "minimum": 1, "maximum": MAX_CART_QUANTITY},
        },
        ["article", "qty"],
    ),
    _function_tool(
        "remove_from_cart",
        "Удалить конкретный товар из корзины только по явной команде клиента.",
        {"article": {"type": "string", "minLength": 1, "maxLength": 128}},
        ["article"],
    ),
    _function_tool(
        "clear_cart",
        "Очистить корзину только по явной команде клиента.",
        {},
        [],
    ),
    _function_tool("get_cart", "Показать локальную корзину текущей сессии.", {}, []),
    _function_tool(
        "set_ui_actions",
        "Записать до шести полезных следующих UI-действ. Не изменяет корзину.",
        {
            "actions": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(ACTION_TYPES)},
                        "article": {"type": ["string", "null"], "maxLength": 128},
                        "qty": {"type": ["integer", "null"], "minimum": 1, "maximum": MAX_CART_QUANTITY},
                    },
                    "required": ["type", "article", "qty"],
                    "additionalProperties": False,
                },
            },
        },
        ["actions"],
    ),
]


def _assistant_message_payload(message, calls: list) -> dict:
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": [
            {
                "id": getattr(call, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(getattr(call, "function", None), "name", ""),
                    "arguments": getattr(getattr(call, "function", None), "arguments", "{}"),
                },
            }
            for call in calls
        ],
    }


def _tool_message_content(result) -> str:
    raw = json.dumps({"untrusted_tool_data": result}, ensure_ascii=False, default=str)
    if len(raw) <= MAX_TOOL_RESULT_CHARS:
        return raw
    excerpt = json.dumps(result, ensure_ascii=False, default=str)[: MAX_TOOL_RESULT_CHARS // 3]
    return json.dumps(
        {"untrusted_tool_data_excerpt": excerpt, "truncated": True},
        ensure_ascii=False,
    )


def _record_ui_actions(state: ActionState, args: dict) -> dict:
    """Store only allowlisted model intentions; labels and commands stay server-owned."""
    if set(args) != {"actions"} or not isinstance(args.get("actions"), list):
        raise ValueError
    if len(args["actions"]) > 6:
        raise ValueError
    proposals: list[dict] = []
    for raw in args["actions"][:6]:
        if not isinstance(raw, dict) or raw.get("type") not in ACTION_TYPES:
            continue
        item = {"type": raw["type"]}
        article = raw.get("article")
        if isinstance(article, str) and article.strip() and len(article) <= 128:
            item["article"] = article.strip()
        qty = raw.get("qty")
        if type(qty) is int and 1 <= qty <= MAX_CART_QUANTITY:
            item["qty"] = qty
        proposals.append(item)
    state.called = True
    state.proposed = proposals
    return {"ok": True, "recorded": len(proposals)}


def _observe_ui_facts(state: ActionState, name: str, args: dict, result: object) -> None:
    """Retain the small trusted result set needed to validate UI actions without new I/O."""
    state.observe(name, args, result)
    products = getattr(state, "observed_products", None)
    if not isinstance(products, dict):
        products = {}
        state.observed_products = products

    values = result if isinstance(result, list) else [result]
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("article"), str) and "error" not in value:
            products[value["article"].casefold()] = value

    if name == "find_analogs" and isinstance(result, list) and result:
        sources = getattr(state, "analog_sources", None)
        if not isinstance(sources, set):
            sources = set()
            state.analog_sources = sources
        article = args.get("article")
        if isinstance(article, str):
            sources.add(article.casefold())


def _cart_mutation_reply(user_text: str, name: str, result: dict) -> str:
    language = detect_language(user_text)
    if name == "change_quantity":
        if language == "en":
            return f"Quantity of {result['article']} in the cart changed to {result['qty']}. Link: {CART_LINK}"
        return (
            f"Себеттегі {result['article']} тауарының саны: {result['qty']} дана. Сілтеме: {CART_LINK}"
            if language == "kk" else
            f"Количество {result['article']} в корзине изменено на {result['qty']} шт. Ссылка: {CART_LINK}"
        )
    if name == "remove_from_cart":
        if language == "en":
            return f"{result['article']} was removed from the cart. Link: {CART_LINK}"
        return (
            f"{result['article']} тауары себеттен алынды. Сілтеме: {CART_LINK}"
            if language == "kk" else
            f"{result['article']} удалён из корзины. Ссылка: {CART_LINK}"
        )
    if language == "en":
        return f"The cart was cleared. Link: {CART_LINK}"
    return f"Себет тазаланды. Сілтеме: {CART_LINK}" if language == "kk" else f"Корзина очищена. Ссылка: {CART_LINK}"


def run_openai_agent(
    messages: list[dict],
    session_id: str,
    tools: ShopTools,
    model: str,
    api_key: str,
    action_state: ActionState | None = None,
) -> str:
    from openai import OpenAI

    system_prompt = asset_path("system_prompt.txt").read_text(encoding="utf-8")
    user_text = str(messages[-1].get("content") or "") if messages else ""
    if requests_internal_instructions(user_text):
        return safe_reply(user_text, "instructions")
    conversation = [{"role": "system", "content": system_prompt}, *messages]
    client = OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT_SECONDS, max_retries=0)
    state = action_state if action_state is not None else ActionState()
    traces: list[dict] = []
    tool_call_count = 0

    def forced_final_completion() -> str:
        """Make one bounded, tool-free call after the operational budget is exhausted."""
        response = client.chat.completions.create(
            model=model,
            messages=conversation,
            temperature=0,
            max_completion_tokens=500,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            return safe_reply(user_text, "tools")
        message = choices[0].message
        if getattr(message, "tool_calls", None) or not str(getattr(message, "content", "") or "").strip():
            return safe_reply(user_text, "tools")
        return guard_model_reply(getattr(message, "content", ""), user_text, traces, system_prompt)

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=model,
            messages=conversation,
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=False,
            temperature=0,
            max_completion_tokens=500,
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            return safe_reply(user_text, "tools")
        message = choices[0].message
        calls = list(getattr(message, "tool_calls", None) or [])
        if not calls:
            return guard_model_reply(getattr(message, "content", ""), user_text, traces, system_prompt)
        operational_calls = [
            call for call in calls
            if str(getattr(getattr(call, "function", None), "name", "")) != "set_ui_actions"
        ]
        action_calls = len(calls) - len(operational_calls)
        if action_calls and operational_calls:
            return safe_reply(user_text, "tools")
        if action_calls > 1:
            return safe_reply(user_text, "tools")
        if len(operational_calls) > MAX_TOOL_CALLS - tool_call_count:
            return safe_reply(user_text, "tools")
        action_only = bool(calls) and not operational_calls
        conversation.append(_assistant_message_payload(message, calls))
        for call in calls:
            name = str(getattr(getattr(call, "function", None), "name", ""))
            args = {}
            try:
                args = json.loads(getattr(getattr(call, "function", None), "arguments", "{}"))
                if not isinstance(args, dict):
                    raise TypeError
                if name == "set_ui_actions":
                    result = _record_ui_actions(state, args)
                else:
                    result = tools.dispatch(name, args, session_id, messages)
                    _observe_ui_facts(state, name, args, result)
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                result = {"error": "Некорректные аргументы инструмента"}
            if name != "set_ui_actions":
                tool_call_count += 1
            traces.append({"name": name, "args": args, "result": result})
            if name == "add_to_cart" and isinstance(result, dict) and result.get("ok"):
                return cart_success_reply(user_text, result)
            if name in {"change_quantity", "remove_from_cart", "clear_cart"} and isinstance(result, dict) and result.get("ok"):
                return _cart_mutation_reply(user_text, name, result)
            conversation.append({
                "role": "tool",
                "tool_call_id": str(getattr(call, "id", "")),
                "content": _tool_message_content(result),
            })
        if action_only:
            return forced_final_completion()
    return forced_final_completion()


_DEMO_SEARCH_STOPWORDS = {
    "а", "в", "вы", "для", "есть", "и", "или", "какая", "какие", "какой", "ли", "мне", "на",
    "найди", "найдите", "нужен", "нужна", "нужно", "нужны", "по", "пожалуйста", "покажи", "покажите",
    "расскажи", "расскажите", "сколько", "стоит", "товар", "товары", "у", "хочу", "цена",
}
_DEMO_SEARCH_ALIASES = {
    "автомата": "автомат", "автоматы": "автомат", "автоматов": "автомат",
    "кабели": "кабель", "кабеля": "кабель", "кабелей": "кабель",
    "лампы": "лампа", "лампу": "лампа", "ламп": "лампа",
    "розетки": "розетка", "розетку": "розетка", "розеток": "розетка",
    "светильники": "светильник", "светильника": "светильник", "светильников": "светильник",
    "выключатели": "выключатель", "выключателя": "выключатель", "выключателей": "выключатель",
    "инструменты": "инструмент", "щиты": "щит", "щитов": "щит",
}


def _demo_search_query(text: str) -> str:
    """Remove conversational filler while retaining brands, ratings and dimensions."""
    tokens = re.findall(r"[0-9A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]+(?:[.,xх×][0-9A-Za-zА-Яа-яЁё]+)*", text)
    useful: list[str] = []
    for token in tokens:
        folded = token.casefold()
        if folded in _DEMO_SEARCH_STOPWORDS:
            continue
        useful.append(_DEMO_SEARCH_ALIASES.get(folded, token))
    return " ".join(useful[:12])


def run_demo_agent(messages: list[dict], session_id: str, tools: ShopTools) -> str:
    """Offline tool-using fallback for common demo questions."""
    text = str(messages[-1]["content"]).strip()
    if contains_payment_data(text):
        return "Не отправляйте платёжные данные, пароли или коды в чат. Оплата проходит только на сайте при оформлении заказа."
    if text in _DEMO_UI_QUESTION_REPLIES:
        return _DEMO_UI_QUESTION_REPLIES[text]
    lower = text.casefold()
    articles = [p["article"] for p in tools.catalog.products if p["article"].casefold() in lower]
    english_change = _english_button_values(_EN_CHANGE_BUTTON_RE, text)
    if english_change:
        article, qty = english_change
        if qty is None:
            return f"Specify a whole-number quantity from 1 to {MAX_CART_QUANTITY}."
        result = tools.dispatch(
            "change_quantity",
            {"article": article, "qty": qty},
            session_id,
            messages,
        )
        return (
            _cart_mutation_reply(text, "change_quantity", result)
            if result.get("ok") else
            "The quantity could not be changed. Check the product, quantity, and available stock."
        )
    english_remove = _english_button_values(_EN_REMOVE_BUTTON_RE, text)
    if english_remove:
        article, _quantity = english_remove
        result = tools.dispatch("remove_from_cart", {"article": article}, session_id, messages)
        return (
            _cart_mutation_reply(text, "remove_from_cart", result)
            if result.get("ok") else
            "The item could not be removed. Check that it is still in the cart."
        )
    if _EN_CLEAR_BUTTON_RE.fullmatch(text):
        result = tools.dispatch("clear_cart", {}, session_id, messages)
        return (
            _cart_mutation_reply(text, "clear_cart", result)
            if result.get("ok") else
            "The cart could not be cleared. Please try again."
        )
    if lower.startswith("измени количество ") or lower.startswith("себеттегі "):
        quantity = re.search(r"\b(\d+)\s*(?:шт|данаға)(?:\W|$)", lower)
        if len(articles) != 1 or not quantity:
            return "Укажите товар и новое количество."
        result = tools.dispatch(
            "change_quantity",
            {"article": articles[0], "qty": int(quantity.group(1))},
            session_id,
            messages,
        )
        return _cart_mutation_reply(text, "change_quantity", result) if result.get("ok") else f"Не удалось изменить количество: {result['error']}."
    if lower.startswith("удали ") or "себеттен алып таста" in lower:
        if len(articles) != 1:
            return "Укажите один товар для удаления."
        result = tools.dispatch("remove_from_cart", {"article": articles[0]}, session_id, messages)
        return _cart_mutation_reply(text, "remove_from_cart", result) if result.get("ok") else f"Не удалось удалить товар: {result['error']}."
    if lower.rstrip(".! ") in {"очисти корзину", "себетті тазала"}:
        result = tools.dispatch("clear_cart", {}, session_id, messages)
        return _cart_mutation_reply(text, "clear_cart", result) if result.get("ok") else f"Не удалось очистить корзину: {result['error']}."
    if purchase_confirmed(messages, tools.catalog):
        english_add = _english_button_values(_EN_ADD_BUTTON_RE, text)
        if english_add:
            article, value = english_add
            target = tools.catalog.get(article)
            source = "english_button"
            quantity = QuantityParse("valid", value) if value is not None else QuantityParse("invalid")
        else:
            target, source = _resolve_requested_product(messages, tools.catalog)
            quantity = _expected_quantity(messages, source, target) if target is not None else QuantityParse("absent")
        if target is None:
            return "Укажите один артикул товара и подтвердите добавление, например: «Добавь 2 шт DEMO-AV-16»."
        if quantity.status != "valid" or quantity.value is None:
            return (
                f"Specify a whole-number quantity from 1 to {MAX_CART_QUANTITY}."
                if english_add else
                f"Укажите количество целым числом от 1 до {MAX_CART_QUANTITY}."
            )
        qty = quantity.value
        result = tools.dispatch("add_to_cart", {"article": target["article"], "qty": qty}, session_id, messages)
        if "error" in result:
            if english_add:
                return "The item could not be added. Check the product, quantity, and available stock."
            return f"Не удалось добавить товар: {result['error']}. Доступно: {result.get('available', 'неизвестно')}."
        return cart_success_reply(text, result)
    if "корзин" in lower or "себет" in lower:
        cart = tools.dispatch("get_cart", {}, session_id, messages)["cart"]
        summary = "В корзине: " + "; ".join(f"{p['name']} — {p['qty']} шт." for p in cart) if cart else "Корзина пуста."
        return f"{summary} Ссылка: {CART_LINK} (локальная корзина с сайтом не синхронизируется)."
    if any(word in lower for word in ("достав", "оплат", "услов", "покуп", "жеткіз", "төлем")):
        return tools.dispatch("get_purchase_conditions", {}, session_id, messages)
    if "менеджер" in lower or "менеджері" in lower:
        return "Для сложного заказа свяжитесь с менеджером по телефону, указанному в шапке сайта."
    if articles and any(word in lower for word in ("характерист", "сипаттама")):
        product = tools.dispatch("get_product", {"article": articles[0]}, session_id, messages)
        specs = product.get("characteristics") or {}
        details = "; ".join(f"{key}: {value}" for key, value in specs.items()) if isinstance(specs, dict) else ""
        return f"{product['name']} ({product['article']}): {details or 'характеристики не указаны'}."
    if articles and any(word in lower for word in ("по складам", "наличие товара", "қоймалардағы")):
        product = tools.dispatch("get_product", {"article": articles[0]}, session_id, messages)
        stores = product.get("stores") or []
        locations = "; ".join(
            f"{store.get('name', 'Склад')}: {store.get('quantity', 'неизвестно')} шт."
            for store in stores if isinstance(store, dict)
        )
        stock = product["stock"] if product["stock"] is not None else "неизвестен"
        return f"{product['name']} ({product['article']}): остаток {stock}. {locations or 'Данных по складам нет.'}"
    if articles and ("сертификат" in lower or "сертификаттар" in lower):
        product = tools.dispatch("get_product", {"article": articles[0]}, session_id, messages)
        certificates = product.get("certificates") or []
        return f"Сертификаты {product['article']}: " + ("; ".join(map(str, certificates)) if certificates else "в каталоге не указаны.")
    if "сравн" in lower or "салыстыр" in lower:
        compared = articles[:]
        if len(compared) < 2:
            for previous in reversed(messages[:-1]):
                if previous.get("role") == "assistant":
                    compared = [
                        product["article"] for product in tools.catalog.products
                        if product["article"].casefold() in str(previous.get("content") or "").casefold()
                    ]
                    if len(compared) >= 2:
                        break
        if len(compared) >= 2:
            products = [tools.catalog.get(article) for article in compared[:3]]
            return "Сравнение: " + "; ".join(
                f"{product['name']} ({product['article']}): {product['price']} ₸, остаток {product['stock']}"
                for product in products if product
            )
        return "Назовите два артикула для сравнения."
    if "аналог" in lower or "ұқсас" in lower:
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
    query = _demo_search_query(text)
    results = tools.dispatch("search_products", {"query": query}, session_id, messages) if query else []
    if not isinstance(results, list):
        results = []
    if results:
        if len(results) == 1:
            p = results[0]
            stock = p["stock"] if p["stock"] is not None else "неизвестен"
            return f"{p['name']} ({p['article']}): {p['price']} ₸, остаток: {stock}. " + ("Могу подобрать аналог." if p["stock"] == 0 else "Добавление недоступно до уточнения остатка." if p["stock"] is None else "Добавить в корзину?")
        return "Нашёл товары: " + "; ".join(f"{p['name']} ({p['article']}), {p['price']} ₸, остаток {p['stock']}" for p in results) + ". Назовите артикул для подробностей."
    requested_article = re.search(r"\b[A-Z0-9]+(?:-[A-Z0-9]+)+\b", text, re.IGNORECASE)
    if requested_article:
        return f"Товар {requested_article.group(0)} в каталоге не найден. Проверьте артикул или уточните название."
    return "По запросу товары в каталоге не найдены. Уточните название, бренд или характеристику."
