"""FastAPI entry point for the EKT shopping assistant."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import unicodedata
from collections import OrderedDict
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from threading import RLock
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from attachments import (AttachmentInput, AttachmentResult, bulk_add_from_context,
                         describe_images, format_context, history_context,
                         process_attachments, summarize_document)
from agent import CART_LINK, ShopTools, contains_payment_data, run_demo_agent, run_openai_agent
from cart_state import CartStateCodec, InvalidCartToken
from catalog import Catalog
from ui_actions import ActionState, ChatAction, build_actions, demo_action_proposals

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
DEMO_QUESTIONS_FALLBACK = [
    "Есть автомат на 25 А?",
    "DEMO-AV-25 нет в наличии? Какой аналог посоветуете?",
    "Как у вас с оплатой и доставкой по Алматы? Есть минимальная партия?",
    "Да, добавь 2 шт DEMO-AV-16 в корзину",
    "Покажи корзину и дай ссылку",
]
LIVE_EXAMPLE_QUESTIONS = [
    "Покажите кабель ВВГ 3х2,5",
    "Есть автомат ABB на 40 А?",
    "Найдите светильник мощностью 36 Вт",
    "Какие товары Legrand сейчас есть в наличии?",
    "Каковы условия оплаты и доставки?",
]
MAX_CONTEXT_CHARS = 24_000
MAX_SESSION_CACHE = 1_000
MAX_MODEL_REPLY_CHARS = 8_000
SESSION_LOCK_STRIPES = 64


def load_demo_questions() -> list[str]:
    """Expose the logic-owned demo script without duplicating it in the UI."""
    path = BASE_DIR / "logic" / "demo_questions.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        questions = [item["user"].strip() for item in payload["script"] if isinstance(item, dict) and isinstance(item.get("user"), str) and item["user"].strip()]
        if questions:
            return questions
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        LOG.warning("Could not load logic/demo_questions.json", exc_info=True)
    return DEMO_QUESTIONS_FALLBACK.copy()


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=40_000)

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        value = unicodedata.normalize("NFKC", value).strip()
        value = "".join(
            character
            for character in value
            if not unicodedata.category(character).startswith("C") or character in "\n\r\t"
        ).strip()
        visible = any(not character.isspace() and not unicodedata.category(character).startswith("C") for character in value)
        if not visible:
            raise ValueError("Сообщение не может быть пустым")
        if len(value) > (40_000 if "[Вложение:" in value else 8_000):
            raise ValueError("Сообщение слишком длинное")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    cart_token: str | None = Field(default=None, max_length=8192)
    attachments: list[AttachmentInput] = Field(default_factory=list)

    @field_validator("session_id", mode="before")
    @classmethod
    def normalize_session_id(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        value = unicodedata.normalize("NFKC", value)
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("session_id содержит недопустимые символы")
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError("session_id имеет недопустимый формат")
        return value

    @model_validator(mode="after")
    def limit_context_size(self):
        limit = 48_000 if any("[Вложение:" in message.content for message in self.messages) else MAX_CONTEXT_CHARS
        if sum(len(message.content) for message in self.messages) > limit:
            raise ValueError("История диалога слишком длинная")
        return self


class CartItem(BaseModel):
    article: str
    name: str
    qty: int
    price: float | None


class ChatResponse(BaseModel):
    reply: str
    cart: list[CartItem]
    cart_link: str | None
    cart_token: str | None = None
    assistant_source: Literal["openai", "demo", "system", "unavailable"] = "system"
    actions: list[ChatAction] = Field(default_factory=list)
    attachments: list[AttachmentResult] = Field(default_factory=list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    catalog = Catalog()
    catalog.load()
    app.state.catalog = catalog
    app.state.tools = ShopTools(catalog)
    signing_key = os.getenv("CART_SIGNING_KEY")
    app.state.cart_codec = CartStateCodec(signing_key) if signing_key else None
    app.state.histories = {}
    app.state.last_responses = OrderedDict()
    app.state.history_lock = RLock()
    app.state.turn_locks = tuple(RLock() for _ in range(SESSION_LOCK_STRIPES))
    yield


app = FastAPI(title="EKT AI Assistant", lifespan=lifespan)
origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    catalog = app.state.catalog
    status = {
        "ok": catalog.source != "unavailable",
        "catalog_mode": "demo" if catalog.demo_mode else "live",
        "catalog_source": catalog.source,
        "products": len(catalog.products),
    }
    return JSONResponse(status, status_code=200 if status["ok"] else 503)


@app.get("/api/demo-questions")
def demo_questions():
    if app.state.catalog.demo_mode:
        return {"questions": load_demo_questions()}

    products = app.state.catalog.products
    available = next(
        (
            product
            for product in products
            if product["stock"] is not None
            and product["stock"] >= 2
            and product["price"] is not None
            and product["price"] > 0
        ),
        None,
    )
    unavailable = next((product for product in products if product["stock"] == 0), None)
    if not available:
        return {"questions": LIVE_EXAMPLE_QUESTIONS}

    article = available["article"]
    analog_question = (
        f"Подберите аналог для {unavailable['article']}"
        if unavailable
        else f"Какие похожие товары есть для {article}?"
    )
    return {
        "questions": [
            f"Какие характеристики и наличие у товара {article}?",
            analog_question,
            "Какие условия оплаты и доставки?",
            f"Да, добавь 2 шт {article} в корзину",
            "Покажи корзину",
        ]
    }


@app.get("/demo-cart", response_class=HTMLResponse)
def demo_cart(token: str):
    """Render the signed assistant cart without exposing its state in the URL body."""
    codec: CartStateCodec | None = app.state.cart_codec
    if not codec:
        raise HTTPException(status_code=404)
    tools = ShopTools(app.state.catalog)
    try:
        session_id = codec.restore(tools, token)
    except InvalidCartToken as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cart = tools.get_cart(session_id)
    rows = "".join(
        f"<li><span>{escape(item['name'])} × {item['qty']}</span>"
        f"<strong>{item['price'] * item['qty']:,.0f} ₸</strong></li>" if item["price"] is not None else
        f"<li><span>{escape(item['name'])} × {item['qty']}</span><strong>Цена неизвестна</strong></li>"
        for item in cart
    )
    total = sum((item["price"] or 0) * item["qty"] for item in cart)
    return HTMLResponse(
        "<!doctype html><html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Корзина ассистента EKT</title><style>body{font:16px/1.5 Arial,sans-serif;max-width:680px;margin:5vh auto;padding:24px;color:#173447}"
        "li{display:flex;justify-content:space-between;gap:20px;padding:14px 0;border-bottom:1px solid #ddd}ul{padding:0;list-style:none}"
        "a{color:#07547a}</style><h1>Корзина ассистента EKT</h1><p>Эта корзина не связана с корзиной сайта ekt.kz. Оформление заказа из неё пока недоступно.</p>"
        f"<ul>{rows}</ul><p><strong>Итого: {total:,.0f} ₸</strong></p><p><a href='/'>Вернуться к чату</a></p></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _request_hash(request: ChatRequest) -> str:
    # An exact transport retry reuses the full payload. Cart mutation has a second,
    # semantic idempotency gate in ShopTools for altered or delayed replays.
    payload = {
        "messages": [message.model_dump() for message in request.messages],
        "cart_token": request.cart_token,
        "attachments": [(item.name, item.mime, hashlib.sha256(item.data_base64.encode("ascii", errors="replace")).hexdigest())
                        for item in request.attachments],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _cached_response(session_id: str, request_hash: str) -> ChatResponse | None:
    with app.state.history_lock:
        cached = app.state.last_responses.get(session_id)
        if cached and cached[0] == request_hash:
            return cached[1].model_copy(deep=True)
    return None


def _bounded_history(messages: list[dict], max_messages: int = 40) -> list[dict]:
    kept: list[dict] = []
    characters = 0
    for message in reversed(messages[-max_messages:]):
        size = len(str(message.get("content") or ""))
        if kept and characters + size > MAX_CONTEXT_CHARS:
            break
        if size > MAX_CONTEXT_CHARS:
            message = {**message, "content": str(message.get("content") or "")[-MAX_CONTEXT_CHARS:]}
            size = MAX_CONTEXT_CHARS
        kept.append(message)
        characters += size
    return list(reversed(kept))


def _canonical_messages(session_id: str, latest_user: dict) -> list[dict]:
    """Trust only server-generated history; client history is transport context, not authority."""
    with app.state.history_lock:
        history = list(app.state.histories.get(session_id, []))
    return _bounded_history([*history, latest_user], max_messages=39)


def _session_turn_lock(session_id: str) -> RLock:
    digest = hashlib.blake2b(session_id.encode("utf-8"), digest_size=2).digest()
    index = int.from_bytes(digest, "big") % len(app.state.turn_locks)
    return app.state.turn_locks[index]


def _response(
    session_id: str,
    reply: str,
    tools: ShopTools,
    http_request: Request,
    latest_user: str = "",
    assistant_source: Literal["openai", "demo", "system", "unavailable"] = "system",
    cart_before: list[dict] | None = None,
    action_state: ActionState | None = None,
    attachment_results: list[AttachmentResult] | None = None,
) -> ChatResponse:
    reply = str(reply or "").strip() or "Не удалось подготовить ответ. Попробуйте переформулировать вопрос."
    reply = re.sub(r"(?:Ссылка\s*:\s*)?https://ekt\.kz/cart", "", reply, flags=re.IGNORECASE)
    reply = re.sub(r"[ \t]+([.,;:])", r"\1", reply)
    reply = re.sub(r"[ \t]{2,}", " ", reply).strip(" -")
    if len(reply) > MAX_MODEL_REPLY_CHARS:
        reply = reply[: MAX_MODEL_REPLY_CHARS - 1].rstrip() + "…"

    cart = tools.get_cart(session_id)
    codec: CartStateCodec | None = app.state.cart_codec
    token = codec.encode(tools, session_id) if codec else None
    cart_link = str(http_request.url_for("demo_cart").include_query_params(token=token)) if token and cart else None
    actions: list[ChatAction] = []
    if assistant_source in ("openai", "demo"):
        before = cart_before or []
        if assistant_source == "openai" and action_state and action_state.called:
            proposals = action_state.proposed
            observed = action_state.observed_articles
        else:
            proposals = demo_action_proposals(latest_user, reply, app.state.catalog, before, cart)
            observed = None
        actions = build_actions(proposals, reply, latest_user, app.state.catalog, before, cart, observed)

    return ChatResponse(
        reply=reply,
        cart=cart,
        cart_link=cart_link,
        cart_token=token,
        assistant_source=assistant_source,
        actions=actions,
        attachments=attachment_results or [],
    )


def _finish_turn(
    session_id: str,
    request_hash: str,
    messages: list[dict],
    reply: str,
    tools: ShopTools,
    http_request: Request,
    assistant_source: Literal["openai", "demo", "system", "unavailable"] = "system",
    cart_before: list[dict] | None = None,
    action_state: ActionState | None = None,
    attachment_results: list[AttachmentResult] | None = None,
) -> ChatResponse:
    response = _response(
        session_id,
        reply,
        tools,
        http_request,
        str(messages[-1].get("content") or "") if messages else "",
        assistant_source,
        cart_before,
        action_state,
        attachment_results,
    )
    with app.state.history_lock:
        app.state.histories[session_id] = _bounded_history(
            [*messages, {"role": "assistant", "content": response.reply}],
            max_messages=40,
        )
        app.state.last_responses[session_id] = (request_hash, response.model_copy(deep=True))
        app.state.last_responses.move_to_end(session_id)
        while len(app.state.last_responses) > MAX_SESSION_CACHE:
            expired_session, _ = app.state.last_responses.popitem(last=False)
            app.state.histories.pop(expired_session, None)
    return response


def _chat_unlocked(request: ChatRequest, http_request: Request) -> ChatResponse:
    codec: CartStateCodec | None = app.state.cart_codec
    tools: ShopTools = ShopTools(app.state.catalog) if codec else app.state.tools
    if codec and request.cart_token:
        try:
            codec.restore(tools, request.cart_token, request.session_id)
        except InvalidCartToken as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    cart_before = tools.get_cart(request.session_id)
    action_state = ActionState()
    incoming = [message.model_dump() for message in request.messages]
    if incoming[-1]["role"] != "user":
        return _response(
            request.session_id,
            "Последнее сообщение должно быть от клиента.",
            tools,
            http_request,
        )

    request_hash = _request_hash(request)
    cached = _cached_response(request.session_id, request_hash)
    if cached is not None:
        return cached
    latest_user = incoming[-1]
    payment_data = contains_payment_data(latest_user["content"])
    if payment_data:
        latest_user = {"role": "user", "content": "[Платёжные данные удалены]"}
    messages = _canonical_messages(request.session_id, latest_user)

    if payment_data:
        return _finish_turn(
            request.session_id,
            request_hash,
            messages,
            "Не отправляйте платёжные данные, пароли или коды в чат. Оплата проходит только на сайте при оформлении заказа.",
            tools,
            http_request,
        )

    processed = process_attachments(request.attachments)
    prior_attachment_context = history_context(incoming[:-1])
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("MODEL_NAME", "gpt-4.1-mini")
    if processed.image_urls and api_key:
        describe_images(processed, api_key, model)
    elif processed.image_urls:
        for result in processed.results:
            if result.kind == "image" and result.status == "ok":
                result.status = "error"
                result.note = "Для распознавания фото требуется ИИ-модель"
        processed.image_urls.clear()
    current_attachment_context = format_context(processed.results)
    attachment_context = (prior_attachment_context + "\n\n" + current_attachment_context).strip()[-48_000:]

    if app.state.catalog.source == "unavailable":
        return _finish_turn(
            request.session_id,
            request_hash,
            messages,
            "Каталог EKT временно недоступен. Проверьте товары и цены позже.",
            tools,
            http_request,
            assistant_source="unavailable",
            attachment_results=processed.results,
        )

    bulk_reply = bulk_add_from_context(latest_user["content"], prior_attachment_context, tools, request.session_id)
    if bulk_reply is not None:
        return _finish_turn(request.session_id, request_hash, messages, bulk_reply, tools, http_request,
                            "demo", cart_before, action_state, processed.results)

    if request.attachments and not any(result.status == "ok" for result in processed.results):
        return _finish_turn(request.session_id, request_hash, messages,
                            "Не удалось прочитать вложения. Проверьте сообщения об ошибках под файлами.",
                            tools, http_request, attachment_results=processed.results)

    demo_mode = app.state.catalog.demo_mode
    try:
        if api_key:
            extra = ({"attachment_context": attachment_context, "image_urls": processed.image_urls}
                     if attachment_context or processed.image_urls else {})
            reply = run_openai_agent(messages, request.session_id, tools, model, api_key, action_state, **extra)
            assistant_source = "openai"
        elif demo_mode:
            reply = (summarize_document(current_attachment_context, tools, request.session_id, messages)
                     if current_attachment_context else None) or run_demo_agent(messages, request.session_id, tools)
            assistant_source = "demo"
        else:
            reply = "ИИ-сервис временно недоступен. Попробуйте позже."
            assistant_source = "unavailable"
    except Exception:
        LOG.exception("Chat agent failed")
        if demo_mode:
            reply = (summarize_document(current_attachment_context, tools, request.session_id, messages)
                     if current_attachment_context else None) or run_demo_agent(messages, request.session_id, tools)
            assistant_source = "demo"
        else:
            reply = "ИИ-сервис временно недоступен. Попробуйте позже."
            assistant_source = "unavailable"
    return _finish_turn(
        request.session_id,
        request_hash,
        messages,
        reply,
        tools,
        http_request,
        assistant_source,
        cart_before,
        action_state,
        processed.results,
    )


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest, http_request: Request):
    # Serialize turns of the same anonymous session (with fixed-memory striped
    # locks) so history and cart mutations cannot race each other.
    with _session_turn_lock(request.session_id):
        return _chat_unlocked(request, http_request)
