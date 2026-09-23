import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import ShopTools, purchase_confirmed, run_openai_agent
from catalog import Catalog
from main import app


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(demo_mode=True)
        self.catalog.load()
        self.tools = ShopTools(self.catalog)

    def test_cart_needs_explicit_confirmation_and_respects_stock(self):
        ask = [{"role": "user", "content": "Сколько стоит DEMO-LED-12?"}]
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-12", 1, ask))
        self.assertEqual(self.tools.get_cart("one"), [])

        confirmed = [{"role": "user", "content": "Добавь 23 шт DEMO-LED-12"}]
        self.assertTrue(purchase_confirmed(confirmed))
        self.assertTrue(self.tools.add_to_cart("one", "DEMO-LED-12", 23, confirmed)["ok"])
        too_many = self.tools.add_to_cart("one", "DEMO-LED-12", 2, confirmed)
        self.assertEqual(too_many["available"], 1)
        self.assertEqual(self.tools.get_cart("one")[0]["qty"], 23)
        self.assertEqual(self.tools.get_cart("other"), [])

    def test_yes_requires_a_cart_offer(self):
        self.assertFalse(purchase_confirmed([{"role": "user", "content": "Да"}]))
        self.assertFalse(purchase_confirmed([{"role": "assistant", "content": "Цена 1290 ₸"}, {"role": "user", "content": "Да"}]))
        self.assertTrue(purchase_confirmed([{"role": "assistant", "content": "Добавить DEMO-LED-12 в корзину?"}, {"role": "user", "content": "Да"}]))
        self.assertFalse(purchase_confirmed([{"role": "user", "content": "Не добавляй товар"}]))

    def test_search_and_analogs(self):
        self.assertEqual(self.catalog.search("лампа")[0]["article"], "DEMO-LED-12")
        self.assertEqual(self.catalog.analogs("DEMO-LED-12")[0]["article"], "DEMO-LED-15")
        self.assertEqual(self.catalog.analogs("DEMO-AV-16"), [])

    def test_openai_tool_cycle_binds_session_and_checks_confirmation(self):
        calls = [
            SimpleNamespace(id="call_1", function=SimpleNamespace(name="add_to_cart", arguments='{"session_id":"other","article":"DEMO-LED-12","qty":2}'))
        ]
        responses = iter([
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=calls, content=None, model_dump=lambda **_: {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "add_to_cart", "arguments": calls[0].function.arguments}}]}))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content="Готово"))]),
        ])
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: next(responses))))
        with patch("openai.OpenAI", return_value=fake):
            answer = run_openai_agent([{"role": "user", "content": "Покажи DEMO-LED-12"}], "one", self.tools, "test-model", "test-key")
        self.assertEqual(answer, "Готово")
        self.assertEqual(self.tools.get_cart("one"), [])
        self.assertEqual(self.tools.get_cart("other"), [])

    def test_demo_http_endpoint_and_static_page(self):
        with patch.dict(os.environ, {"DEMO_MODE": "1", "OPENAI_API_KEY": ""}):
            with TestClient(app) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/health").json()["catalog_source"], "demo")
                search = client.post("/api/chat", json={"session_id": "demo", "messages": [{"role": "user", "content": "Покажи лампы"}]})
                self.assertEqual(search.status_code, 200)
                self.assertEqual(search.json()["cart"], [])
                add = client.post("/api/chat", json={"session_id": "demo", "messages": [{"role": "user", "content": "Добавь 2 шт DEMO-LED-12"}]})
                self.assertEqual(add.status_code, 200)
                self.assertEqual(add.json()["cart"][0]["qty"], 2)
                self.assertEqual(add.json()["cart_link"], "https://ekt.kz/cart")


if __name__ == "__main__":
    unittest.main()
