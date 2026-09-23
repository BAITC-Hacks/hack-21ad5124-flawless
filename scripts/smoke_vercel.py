"""Check the public EKT demo without using account credentials."""

from __future__ import annotations

import json
import sys
from urllib.request import Request, urlopen
from uuid import uuid4


BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://ekt-ai-assistant-demo.vercel.app").rstrip("/")


def get(path: str) -> bytes:
    with urlopen(BASE + path, timeout=30) as response:
        assert response.status == 200, (path, response.status)
        return response.read()


def main() -> None:
    assert b"EKT" in get("/")
    assert b"cart_token" in get("/static/app.js")
    health = json.loads(get("/health"))
    assert health["ok"] and health["catalog_source"] == "demo" and health["products"] > 0

    session_id = "smoke-" + uuid4().hex
    messages: list[dict[str, str]] = []
    cart_token = None

    def ask(question: str) -> dict:
        nonlocal cart_token
        messages.append({"role": "user", "content": question})
        payload = json.dumps({"session_id": session_id, "messages": messages, "cart_token": cart_token}).encode()
        request = Request(BASE + "/api/chat", data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            assert response.status == 200, response.status
            result = json.load(response)
        messages.append({"role": "assistant", "content": result["reply"]})
        cart_token = result["cart_token"]
        assert cart_token
        return result

    product = ask("Есть DEMO-AV-16?")
    assert "DEMO-AV-16" in product["reply"] and not product["cart"]
    analog = ask("DEMO-AV-25 нет в наличии? Какой аналог посоветуете?")
    assert "DEMO-AV-16" in analog["reply"] and not analog["cart"]
    conditions = ask("Какие условия оплаты и доставки?")
    assert "достав" in conditions["reply"].lower() and not conditions["cart"]
    added = ask("Да, добавь 2 шт DEMO-AV-16 в корзину")
    assert len(added["cart"]) == 1 and added["cart"][0]["qty"] == 2
    assert added["cart_link"].startswith(BASE + "/demo-cart?token=")
    with urlopen(added["cart_link"], timeout=30) as response:
        assert response.status == 200
        assert "DEMO-AV-16" in response.read().decode()
        assert response.headers["Cache-Control"] == "no-store"
    over_stock = ask("Да, добавь 99 шт DEMO-AV-16 в корзину")
    assert over_stock["cart"][0]["qty"] == 2
    resumed = ask("Покажи корзину")
    assert resumed["cart"][0]["qty"] == 2
    print("Public demo passed: page, catalog, analog, conditions, confirmation, stock limit, persistent cart, cart link")


if __name__ == "__main__":
    main()
