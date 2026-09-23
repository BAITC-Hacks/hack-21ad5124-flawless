"""FastAPI entry point for the EKT shopping assistant."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import CART_LINK, ShopTools, run_demo_agent, run_openai_agent
from catalog import Catalog

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


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
    return {"ok": True, "catalog_source": app.state.catalog.source, "products": len(app.state.catalog.products)}


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    messages = [message.model_dump() for message in request.messages]
    tools: ShopTools = app.state.tools
    if messages[-1]["role"] != "user":
        return ChatResponse(reply="Последнее сообщение должно быть от клиента.", cart=tools.get_cart(request.session_id), cart_link=CART_LINK)

    demo_mode = os.getenv("DEMO_MODE", "0") == "1"
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
    return ChatResponse(reply=reply, cart=tools.get_cart(request.session_id), cart_link=CART_LINK)
