"""Check the public EKT demo without using account credentials."""

from __future__ import annotations

import json
import sys
from urllib.request import Request, urlopen
from uuid import uuid4


EXPECT_OPENAI = "--expect-openai" in sys.argv
ARGS = [arg for arg in sys.argv[1:] if arg != "--expect-openai"]
BASE = (ARGS[0] if ARGS else "https://ekt-ai-assistant-demo.vercel.app").rstrip("/")


def get(path: str) -> bytes:
    with urlopen(BASE + path, timeout=30) as response:
        assert response.status == 200, (path, response.status)
        return response.read()


def main() -> None:
    assert b"EKT" in get("/")
    assert b"cart_token" in get("/static/app.js")
    health = json.loads(get("/health"))
    assert health["ok"] and health["catalog_source"] in ("demo", "live") and health["products"] > 0
    demo_mode = health["catalog_source"] == "demo"

    session_id = "smoke-" + uuid4().hex
    messages: list[dict[str, str]] = []
    cart_token = None

    def ask(question: str) -> dict:
        nonlocal cart_token
        messages.append({"role": "user", "content": question})
        payload = json.dumps({"session_id": session_id, "messages": messages, "cart_token": cart_token}).encode()
        request = Request(BASE + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=60) as response:
            assert response.status == 200, response.status
            result = json.load(response)
        messages.append({"role": "assistant", "content": result["reply"]})
        cart_token = result["cart_token"]
        assert cart_token
        return result

    questions = json.loads(get("/api/demo-questions"))["questions"]
    if demo_mode:
        article = "DEMO-AV-16"
        product = ask("Есть DEMO-AV-16?")
        assert article in product["reply"] and not product["cart"]
        analog = ask("DEMO-AV-25 нет в наличии? Какой аналог посоветуете?")
        assert article in analog["reply"] and not analog["cart"]
    else:
        article = questions[0].split()[-1].rstrip("?")
        product = ask(questions[0])
        assert article.casefold() in product["reply"].casefold() and not product["cart"]
    if EXPECT_OPENAI:
        assert product["assistant_source"] == "openai", product["assistant_source"]
    add_action = next(action for action in product["actions"] if action["type"] == "add_to_cart")
    assert add_action["article"] == article and add_action["max_qty"] >= 2
    if demo_mode:
        assert not any(action["type"] == "add_to_cart" for action in analog["actions"])
    conditions = ask("Какие условия оплаты и доставки?")
    assert "достав" in conditions["reply"].lower() and not conditions["cart"]
    added = ask(add_action["message"].replace("{qty}", "2"))
    assert len(added["cart"]) == 1 and added["cart"][0]["qty"] == 2
    assert added["cart_link"].startswith(BASE + "/demo-cart?token=")
    assert "token=" not in added["reply"]
    with urlopen(added["cart_link"], timeout=30) as response:
        assert response.status == 200
        assert article in response.read().decode()
        assert response.headers["Cache-Control"] == "no-store"
    over_stock = ask(f"Да, добавь 999999 шт {article} в корзину")
    assert over_stock["cart"][0]["qty"] == 2
    resumed = ask("Покажи корзину")
    assert resumed["cart"][0]["qty"] == 2
    print(f"Public {health['catalog_source']} demo passed with {product['assistant_source']}: page, catalog, conditions, confirmation, stock limit, persistent cart, cart link")


if __name__ == "__main__":
    main()
