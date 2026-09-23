"""Deterministic checks for model text and localized safe responses."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation


PRODUCT_FACT_TOOLS = {"search_products", "get_product", "find_analogs"}


def detect_language(text: str) -> str:
    lowered = text.casefold()
    kazakh_markers = ("бар ма", "қанша", "канша", "тұрады", "турады", "қалай", "калай", "керек", "рахмет", "иә", "иа", "жоқ", "жок", "дана", "себет")
    if any(marker in lowered for marker in kazakh_markers) or re.search(r"[әғқңөұүһі]", lowered):
        return "kk"
    latin = len(re.findall(r"[a-z]", lowered))
    cyrillic = len(re.findall(r"[а-яё]", lowered))
    return "en" if latin > max(5, cyrillic * 2) else "ru"


def safe_reply(user_text: str, reason: str = "facts") -> str:
    language = detect_language(user_text)
    messages = {
        "facts": {
            "ru": "Не могу подтвердить эти данные по каталогу. Уточните название или ключевую характеристику — я проверю ещё раз.",
            "kk": "Бұл деректерді каталогтан растай алмадым. Атауын немесе негізгі сипаттамасын нақтылаңыз — қайта тексеремін.",
            "en": "I could not verify that information in the catalog. Please specify the product name or a key characteristic so I can check again.",
        },
        "instructions": {
            "ru": "Я не раскрываю внутренние инструкции. Могу помочь только с товарами и условиями покупки EKT.",
            "kk": "Ішкі нұсқауларды ашпаймын. EKT тауарлары мен сатып алу шарттары бойынша көмектесе аламын.",
            "en": "I cannot reveal internal instructions. I can help with EKT products and purchase terms.",
        },
        "discount": {
            "ru": "Я не могу обещать неподтверждённую скидку. Условия для крупного заказа уточняет менеджер EKT.",
            "kk": "Расталмаған жеңілдікке уәде бере алмаймын. Ірі тапсырыс шарттарын EKT менеджері нақтылайды.",
            "en": "I cannot promise an unverified discount. An EKT manager confirms terms for large orders.",
        },
        "tools": {
            "ru": "Не удалось безопасно завершить запрос. Уточните название или ключевую характеристику товара.",
            "kk": "Сұрауды қауіпсіз аяқтау мүмкін болмады. Тауардың атауын немесе негізгі сипаттамасын нақтылаңыз.",
            "en": "The request could not be completed safely. Please specify the product name or a key characteristic.",
        },
    }
    return messages.get(reason, messages["facts"])[language]


def cart_success_reply(user_text: str, result: dict) -> str:
    language = detect_language(user_text)
    product = str(result.get("name") or result.get("article") or "товар")
    qty = result.get("added_qty", result.get("qty", 1))
    if result.get("replayed"):
        if language == "kk":
            return f"Бұл сұрау бұрын өңделген, тауар қайта қосылмады. Себеттегі {product}: {result.get('qty', 0)} дана."
        if language == "en":
            return f"This request was already processed; the item was not added twice. {product} in cart: {result.get('qty', 0)}."
        return f"Этот запрос уже обработан — товар повторно не добавлен. {product} в корзине: {result.get('qty', 0)} шт."
    if language == "kk":
        return f"Себетке қосылды: {product}, {qty} дана."
    if language == "en":
        return f"Added to the cart: {product}, quantity {qty}."
    return f"Добавлено в корзину: {product}, {qty} шт."


def _decimal_amount(raw: str) -> Decimal | None:
    cleaned = re.sub(r"[\s\u00a0\u202f]", "", raw)
    if "," in cleaned and "." in cleaned:
        decimal_separator = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        cleaned = cleaned.replace(grouping_separator, "").replace(decimal_separator, ".")
    elif cleaned.count(",") > 1:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _currency_amounts(text: str) -> set[Decimal]:
    amounts = set()
    for raw in re.findall(r"(?<!\w)(\d[\d\s\u00a0\u202f.,]*)\s*(?:₸|тенге|тг)", text.casefold()):
        amount = _decimal_amount(raw)
        if amount is not None:
            amounts.add(amount)
    return amounts


def _trace_text(traces: list[dict]) -> str:
    return json.dumps(traces, ensure_ascii=False, default=str)


def _result_trace_text(traces: list[dict]) -> str:
    return json.dumps([trace.get("result") for trace in traces], ensure_ascii=False, default=str)


def _grounded_amounts(traces: list[dict]) -> set[Decimal]:
    amounts = _currency_amounts(_result_trace_text(traces))

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                if key == "price" and isinstance(nested, (int, float)) and not isinstance(nested, bool):
                    amounts.add(Decimal(str(nested)))
                walk(nested)
            price = value.get("price")
            quantity = value.get("qty")
            if (
                isinstance(price, (int, float))
                and not isinstance(price, bool)
                and isinstance(quantity, int)
                and not isinstance(quantity, bool)
                and quantity > 0
            ):
                amounts.add(Decimal(str(price)) * quantity)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for trace in traces:
        walk(trace.get("result"))
    return amounts


def requests_internal_instructions(text: str) -> bool:
    lowered = " ".join(str(text or "").casefold().split())
    return bool(re.search(
        r"(?:системн\w*\s+(?:промпт|инструкц\w*)|скрыт\w*\s+(?:промпт|инструкц\w*)|"
        r"internal instructions?|system prompt|hidden instructions?|"
        r"игнорир\w*.{0,30}(?:инструкц|правил)|ignore.{0,30}(?:instructions?|rules?)|"
        r"ты теперь (?:не|обычн|друг)|you are now|pretend (?:you are|to be)|jailbreak)",
        lowered,
    ))


def _contains_prompt_excerpt(reply: str, prompt: str, min_words: int = 12) -> bool:
    reply_words = re.findall(r"[\w-]+", reply, re.UNICODE)
    normalized_prompt = " ".join(re.findall(r"[\w-]+", prompt, re.UNICODE))
    if len(reply_words) < min_words:
        return False
    return any(
        " ".join(reply_words[index:index + min_words]) in normalized_prompt
        for index in range(len(reply_words) - min_words + 1)
    )


def _requires_product_lookup(user_text: str) -> bool:
    lowered = str(user_text or "").casefold()
    condition_intent = bool(re.search(r"(?:достав|оплат|услов|самовывоз|возврат|purchase|delivery|payment)", lowered))
    concrete_product = bool(re.search(
        r"(?:артикул|товар|кабел|провод|автомат|выключател|светильник|ламп|розет|щит|sku|product)",
        lowered,
    ))
    if condition_intent and not concrete_product:
        return False
    return bool(re.search(
        r"(?:артикул|товар|кабел|провод|автомат|выключател|светильник|ламп|розет|щит|"
        r"цена|сто(?:ит|имость)|налич|остат|аналог|характерист|мощност|вольт|ампер|"
        r"sku|product|price|stock|in stock|"
        r"(?=[a-zа-яёәіңғүұқөһ0-9-]*[a-zа-яёәіңғүұқөһ])(?=[a-zа-яёәіңғүұқөһ0-9-]*\d)"
        r"[a-zа-яёәіңғүұқөһ0-9]+(?:-[a-zа-яёәіңғүұқөһ0-9]+)+)",
        lowered,
    ))


_ARTICLE_TOKEN_RE = re.compile(
    r"(?<![\w-])(?=[a-zа-яёәіңғүұқөһ0-9-]*[a-zа-яёәіңғүұқөһ])"
    r"(?=[a-zа-яёәіңғүұқөһ0-9-]*\d)[a-zа-яёәіңғүұқөһ0-9]+"
    r"(?:-[a-zа-яёәіңғүұқөһ0-9]+)+(?![\w-])",
    re.IGNORECASE,
)


def _grounded_articles(traces: list[dict]) -> set[str]:
    articles: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            article = value.get("article")
            if isinstance(article, str) and article.strip():
                articles.add(article.casefold().strip())
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for trace in traces:
        walk(trace.get("result"))
    return articles


def _grounded_products(traces: list[dict]) -> dict[str, dict]:
    products: dict[str, dict] = {}

    def walk(value):
        if isinstance(value, dict):
            article = value.get("article")
            if isinstance(article, str) and article.strip() and any(
                key in value for key in ("name", "price", "stock", "availability", "characteristics")
            ):
                products[article.casefold().strip()] = value
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for trace in traces:
        walk(trace.get("result"))
    return products


def _references_grounded_product(reply: str, products: dict[str, dict]) -> bool:
    lowered = reply.casefold()
    for article, product in products.items():
        if re.search(rf"(?<![\w-]){re.escape(article)}(?![\w-])", lowered):
            return True
        name = " ".join(str(product.get("name") or "").casefold().split())
        if name and name in " ".join(lowered.split()):
            return True
    return False


def _article_bound_claims_are_grounded(reply: str, products: dict[str, dict]) -> bool:
    for sentence in re.split(r"[.!?;\n]+", reply.casefold()):
        mentioned = [
            (article, product)
            for article, product in products.items()
            if re.search(rf"(?<![\w-]){re.escape(article)}(?![\w-])", sentence)
        ]
        if len(mentioned) != 1:
            continue
        _article, product = mentioned[0]
        claimed_amounts = _currency_amounts(sentence)
        allowed_amounts: set[Decimal] = set()
        price = product.get("price")
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            allowed_amounts.add(Decimal(str(price)))
            quantity = product.get("qty")
            if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0:
                allowed_amounts.add(Decimal(str(price)) * quantity)
        if claimed_amounts and not claimed_amounts.issubset(allowed_amounts):
            return False

        claimed_stocks = {
            int(value)
            for value in re.findall(r"(?:остат(?:ок|ка)|на складе|қоймада|stock)\s*[:—-]?\s*(\d+)", sentence)
        }
        stock = product.get("stock")
        if claimed_stocks and (not isinstance(stock, int) or claimed_stocks != {stock}):
            return False
        says_out = bool(re.search(r"\b(?:нет в наличии|отсутствует|қоймада жоқ|out of stock)\b", sentence))
        says_in = not says_out and bool(re.search(r"\b(?:в наличии|на складе|қоймада|in stock)\b", sentence))
        if says_in and (not isinstance(stock, int) or stock <= 0):
            return False
        if says_out and stock != 0:
            return False
    return True


def _grounded_stocks(traces: list[dict]) -> set[int]:
    stocks: set[int] = set()

    def walk(value):
        if isinstance(value, dict):
            stock = value.get("stock")
            if isinstance(stock, int) and not isinstance(stock, bool):
                stocks.add(stock)
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    for trace in traces:
        walk(trace.get("result"))
    return stocks


def guard_model_reply(reply: str, user_text: str, traces: list[dict], system_prompt: str) -> str:
    reply = str(reply or "").strip()
    if not reply:
        return safe_reply(user_text, "facts")

    normalized_reply = " ".join(reply.casefold().split())
    normalized_prompt = " ".join(system_prompt.casefold().split())
    leak_phrases = (
        "вот мой системный промпт",
        "мой системный промпт:",
        "system prompt:",
        "internal instructions:",
        "безопасность инструкций",
        "главное правило — факты только из инструментов",
    )
    prompt_overlap = _contains_prompt_excerpt(normalized_reply, normalized_prompt)
    if prompt_overlap or any(phrase in normalized_reply for phrase in leak_phrases):
        return safe_reply(user_text, "instructions")

    trace_text = _result_trace_text(traces).casefold()
    claimed_percentages = {re.sub(r"\s+", "", value) for value in re.findall(r"\b\d{1,3}\s*%", normalized_reply)}
    grounded_percentages = {re.sub(r"\s+", "", value) for value in re.findall(r"\b\d{1,3}\s*%", trace_text)}
    percentage_words = re.findall(
        r"\b[^\W\d_]+(?:\s+[^\W\d_]+)?\s+процент\w*\b",
        normalized_reply,
        re.UNICODE,
    )
    if not claimed_percentages.issubset(grounded_percentages) or any(value not in trace_text for value in percentage_words):
        return safe_reply(user_text, "discount")

    cart_lookup_intent = bool(re.search(r"(?:корзин|себет|\bcart\b)", user_text.casefold()))
    conditions_trace = any(
        trace.get("name") == "get_purchase_conditions"
        and isinstance(trace.get("result"), str)
        and bool(trace["result"].strip())
        for trace in traces
    )
    successful_product_trace = any(
        (
            trace.get("name") in PRODUCT_FACT_TOOLS
            and trace.get("result") not in (None, [], {})
            and not (isinstance(trace.get("result"), dict) and trace["result"].get("error"))
        )
        or (
            trace.get("name") == "get_cart"
            and cart_lookup_intent
            and isinstance(trace.get("result"), dict)
            and bool(trace["result"].get("cart"))
        )
        for trace in traces
    )
    if _requires_product_lookup(user_text) and not successful_product_trace:
        return safe_reply(user_text, "facts")
    grounded_products = _grounded_products(traces)
    if successful_product_trace and _requires_product_lookup(user_text):
        if not _references_grounded_product(reply, grounded_products):
            return safe_reply(user_text, "facts")
        if not _article_bound_claims_are_grounded(reply, grounded_products):
            return safe_reply(user_text, "facts")
    claimed_articles = {value.casefold() for value in _ARTICLE_TOKEN_RE.findall(reply)}
    if claimed_articles and not claimed_articles.issubset(_grounded_articles(traces)):
        return safe_reply(user_text, "facts")
    factual_claim = bool(re.search(r"(?:₸|тенге|\bтг\b|\bв наличии\b|\bостат(?:ок|ка)\b|\bна складе\b|\bқоймада\b|\bstock\b|\bin stock\b)", normalized_reply))
    if factual_claim and not successful_product_trace and not conditions_trace:
        return safe_reply(user_text, "facts")

    claimed_amounts = _currency_amounts(reply)
    if claimed_amounts and not claimed_amounts.issubset(_grounded_amounts(traces)):
        return safe_reply(user_text, "facts")

    grounded_stocks = _grounded_stocks(traces)
    claimed_stocks = {
        int(value)
        for value in re.findall(r"(?:остат(?:ок|ка)|на складе|қоймада|stock)\s*[:—-]?\s*(\d+)", normalized_reply)
    }
    if claimed_stocks and not claimed_stocks.issubset(grounded_stocks):
        return safe_reply(user_text, "facts")
    says_out_of_stock = bool(re.search(r"\b(?:нет в наличии|отсутствует|қоймада жоқ|out of stock)\b", normalized_reply))
    says_in_stock = not says_out_of_stock and bool(re.search(r"\b(?:в наличии|на складе|қоймада|in stock)\b", normalized_reply))
    if says_in_stock and grounded_stocks and not any(stock > 0 for stock in grounded_stocks):
        return safe_reply(user_text, "facts")
    if says_out_of_stock and grounded_stocks and not any(stock == 0 for stock in grounded_stocks):
        return safe_reply(user_text, "facts")

    negated_cart_mutation = bool(re.search(
        r"\b(?:не|не был(?:о|а|и)?|не удалось)\s+(?:добав\w*|полож\w*)\b",
        normalized_reply,
    ))
    claims_cart_mutation = not negated_cart_mutation and bool(re.search(
        r"\b(?:добавил(?:а|и)?|добавлен(?:о|а|ы)?|положил(?:а|и)?|себетке қосылды|added to (?:the )?cart)\b",
        normalized_reply,
    ))
    successful_add = any(trace.get("name") == "add_to_cart" and isinstance(trace.get("result"), dict) and trace["result"].get("ok") for trace in traces)
    if claims_cart_mutation and not successful_add:
        return safe_reply(user_text, "facts")

    return reply
