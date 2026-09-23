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
from pathlib import Path
from threading import RLock
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent import CART_LINK, ShopTools, contains_payment_data, run_demo_agent, run_openai_agent
from catalog import Catalog

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
    content: str = Field(min_length=1, max_length=8000)

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
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)

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
        if sum(len(message.content) for message in self.messages) > MAX_CONTEXT_CHARS:
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
    cart_link: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    catalog = Catalog()
    catalog.load()
    app.state.catalog = catalog
    app.state.tools = ShopTools(catalog)
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
    questions = load_demo_questions() if app.state.catalog.demo_mode else LIVE_EXAMPLE_QUESTIONS
    return {"questions": questions}


def _request_hash(request: ChatRequest) -> str:
    # An exact transport retry reuses the full payload. Cart mutation has a second,
    # semantic idempotency gate in ShopTools for altered or delayed replays.
    payload = [message.model_dump() for message in request.messages]
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


def _finish_turn(session_id: str, request_hash: str, messages: list[dict], reply: str, tools: ShopTools) -> ChatResponse:
    reply = str(reply or "").strip() or "Не удалось подготовить ответ. Попробуйте переформулировать вопрос."
    if len(reply) > MAX_MODEL_REPLY_CHARS:
        reply = reply[: MAX_MODEL_REPLY_CHARS - 1].rstrip() + "…"
    response = ChatResponse(reply=reply, cart=tools.get_cart(session_id), cart_link=CART_LINK)
    with app.state.history_lock:
        app.state.histories[session_id] = _bounded_history(
            [*messages, {"role": "assistant", "content": reply}],
            max_messages=40,
        )
        app.state.last_responses[session_id] = (request_hash, response.model_copy(deep=True))
        app.state.last_responses.move_to_end(session_id)
        while len(app.state.last_responses) > MAX_SESSION_CACHE:
            expired_session, _ = app.state.last_responses.popitem(last=False)
            app.state.histories.pop(expired_session, None)
    return response


def _chat_unlocked(request: ChatRequest):
    tools: ShopTools = app.state.tools
    incoming = [message.model_dump() for message in request.messages]
    if incoming[-1]["role"] != "user":
        return ChatResponse(reply="Последнее сообщение должно быть от клиента.", cart=tools.get_cart(request.session_id), cart_link=CART_LINK)

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
        )

    if app.state.catalog.source == "unavailable":
        return _finish_turn(request.session_id, request_hash, messages, "Каталог EKT временно недоступен. Проверьте товары и цены позже.", tools)

    demo_mode = app.state.catalog.demo_mode
    api_key = os.getenv("OPENAI_API_KEY")
    try:
        if api_key:
            reply = run_openai_agent(messages, request.session_id, tools, os.getenv("MODEL_NAME", "gpt-4.1-mini"), api_key)
        elif demo_mode:
            reply = run_demo_agent(messages, request.session_id, tools)
        else:
            reply = "ИИ-сервис временно недоступен. Попробуйте позже."
    except Exception:
        LOG.exception("Chat agent failed")
        reply = run_demo_agent(messages, request.session_id, tools) if demo_mode else "ИИ-сервис временно недоступен. Попробуйте позже."
    return _finish_turn(request.session_id, request_hash, messages, reply, tools)


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Serialize turns of the same anonymous session (with fixed-memory striped
    # locks) so history and cart mutations cannot race each other.
    with _session_turn_lock(request.session_id):
        return _chat_unlocked(request)
