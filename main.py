"""FastAPI entry point for the EKT shopping assistant."""

from __future__ import annotations

import json
import logging
import os
from html import escape
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    cart_token: str | None = Field(default=None, max_length=8192)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    catalog = Catalog()
    catalog.load()
    app.state.catalog = catalog
    app.state.tools = ShopTools(catalog)
    signing_key = os.getenv("CART_SIGNING_KEY")
    app.state.cart_codec = CartStateCodec(signing_key) if signing_key else None
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
    status = {"ok": catalog.source != "unavailable", "catalog_source": catalog.source, "products": len(catalog.products)}
    return JSONResponse(status, status_code=200 if status["ok"] else 503)


@app.get("/api/demo-questions")
def demo_questions():
    if not app.state.catalog.demo_mode:
        products = app.state.catalog.products
        available = next((p for p in products if p["stock"] is not None and p["stock"] >= 2 and p["price"] is not None and p["price"] > 0), None)
        unavailable = next((p for p in products if p["stock"] == 0 and app.state.catalog.analogs(p["article"])), None)
        if available:
            article = available["article"]
            return {"questions": [
                f"Какие характеристики и наличие у товара {article}?",
                f"Подберите аналог для {unavailable['article']}" if unavailable else f"Какие похожие товары есть для {article}?",
                "Какие условия оплаты и доставки?",
                f"Да, добавь 2 шт {article} в корзину",
                "Покажи корзину",
            ]}
        return {"questions": ["Какие товары есть в наличии?", "Какие условия оплаты и доставки?"]}
    return {"questions": load_demo_questions()}


@app.get("/demo-cart", response_class=HTMLResponse)
def demo_cart(token: str):
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
        f"<li><span>{escape(item['name'])} ({escape(item['article'])}) × {item['qty']}</span>"
        f"<strong>{item['price'] * item['qty']:,.0f} ₸</strong></li>" if item["price"] is not None else
        f"<li><span>{escape(item['name'])} ({escape(item['article'])}) × {item['qty']}</span><strong>Цена неизвестна</strong></li>"
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


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest, http_request: Request):
    messages = [message.model_dump() for message in request.messages]
    codec: CartStateCodec | None = app.state.cart_codec
    tools: ShopTools = ShopTools(app.state.catalog) if codec else app.state.tools
    if codec and request.cart_token:
        try:
            codec.restore(tools, request.cart_token, request.session_id)
        except InvalidCartToken as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    cart_before = tools.get_cart(request.session_id)
    action_state = ActionState()

    def response(reply: str, assistant_source: Literal["openai", "demo", "system", "unavailable"] = "system") -> ChatResponse:
        cart = tools.get_cart(request.session_id)
        token = codec.encode(tools, request.session_id) if codec else None
        cart_link = str(http_request.url_for("demo_cart").include_query_params(token=token)) if token and cart else None
        clean_reply = reply.replace(CART_LINK, "кнопка «Открыть корзину ассистента»")
        actions: list[ChatAction] = []
        if assistant_source in ("openai", "demo"):
            proposals = (action_state.proposed if assistant_source == "openai" and action_state.called else
                         demo_action_proposals(messages[-1]["content"], clean_reply, app.state.catalog, cart_before, cart))
            observed = action_state.observed_articles if assistant_source == "openai" and action_state.called else None
            actions = build_actions(proposals, clean_reply, messages[-1]["content"], app.state.catalog,
                                    cart_before, cart, observed)
        return ChatResponse(reply=clean_reply, cart=cart, cart_link=cart_link, cart_token=token,
                            assistant_source=assistant_source, actions=actions)

    if messages[-1]["role"] != "user":
        return response("Последнее сообщение должно быть от клиента.")

    if contains_payment_data(messages[-1]["content"]):
        return response("Не отправляйте платёжные данные в чат. Оплата проходит только на сайте при оформлении заказа.")

    messages = [{**message, "content": "[Платёжные данные удалены]" if contains_payment_data(message["content"]) else message["content"]} for message in messages]

    if app.state.catalog.source == "unavailable":
        return response("Каталог EKT временно недоступен. Проверьте товары и цены позже.")

    demo_mode = os.getenv("DEMO_MODE", "0") == "1"
    api_key = os.getenv("OPENAI_API_KEY")
    try:
        if api_key:
            reply = run_openai_agent(messages, request.session_id, tools, os.getenv("MODEL_NAME", "gpt-4.1-mini"), api_key, action_state)
            assistant_source = "openai"
        elif demo_mode:
            reply = run_demo_agent(messages, request.session_id, tools)
            assistant_source = "demo"
        else:
            reply = "ИИ-сервис временно недоступен. Попробуйте позже."
            assistant_source = "unavailable"
    except Exception:
        LOG.exception("Chat agent failed")
        if demo_mode:
            reply = run_demo_agent(messages, request.session_id, tools)
            assistant_source = "demo"
        else:
            reply = "ИИ-сервис временно недоступен. Попробуйте позже."
            assistant_source = "unavailable"
    return response(reply, assistant_source)
