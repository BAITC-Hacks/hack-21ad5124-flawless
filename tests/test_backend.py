import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import ShopTools, purchase_confirmed, run_demo_agent, run_openai_agent
from catalog import Catalog, asset_path, normalize_product
from guardrails import safe_reply
from main import app


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(demo_mode=True)
        demo_file = Path(__file__).resolve().parents[1] / "catalog_demo.json"
        self.catalog._set_products([normalize_product(item) for item in json.loads(demo_file.read_text(encoding="utf-8"))], "demo")
        self.tools = ShopTools(self.catalog)

    def test_cart_needs_explicit_confirmation_and_respects_stock(self):
        ask = [{"role": "user", "content": "Сколько стоит DEMO-LED-12?"}]
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-12", 1, ask))
        self.assertEqual(self.tools.get_cart("one"), [])

        confirmed = [{"role": "user", "content": "Добавь 23 шт DEMO-LED-12"}]
        self.assertTrue(purchase_confirmed(confirmed))
        self.assertTrue(self.tools.add_to_cart("one", "DEMO-LED-12", 23, confirmed)["ok"])
        too_many = self.tools.add_to_cart("one", "DEMO-LED-12", 2, [{"role": "user", "content": "Добавь 2 шт DEMO-LED-12"}])
        self.assertEqual(too_many["available"], 1)
        self.assertEqual(self.tools.get_cart("one")[0]["qty"], 23)
        self.assertEqual(self.tools.get_cart("other"), [])

    def test_yes_requires_a_cart_offer(self):
        self.assertFalse(purchase_confirmed([{"role": "user", "content": "Да"}]))
        self.assertFalse(purchase_confirmed([{"role": "assistant", "content": "Цена 1290 ₸"}, {"role": "user", "content": "Да"}]))
        self.assertTrue(purchase_confirmed([{"role": "assistant", "content": "Добавить DEMO-LED-12 в корзину?"}, {"role": "user", "content": "Да"}]))
        self.assertFalse(purchase_confirmed([{"role": "user", "content": "Не добавляй товар"}]))

    def test_kazakh_confirmation_and_quantity(self):
        direct = [{"role": "user", "content": "DEMO-AV-16 тауарынан 2 дана себетке қос"}]
        self.assertTrue(purchase_confirmed(direct))
        self.assertTrue(self.tools.add_to_cart("kz-direct", "DEMO-AV-16", 2, direct)["ok"])

        offer = [
            {"role": "assistant", "content": "2 дана Автоматический выключатель 1P 16 А себетке қосайын ба?"},
            {"role": "user", "content": "Иә"},
        ]
        self.assertTrue(purchase_confirmed(offer))
        self.assertTrue(self.tools.add_to_cart("kz-offer", "DEMO-AV-16", 2, offer)["ok"])
        self.assertFalse(purchase_confirmed([{"role": "user", "content": "DEMO-AV-16 себетке қоспа"}]))

    def test_cart_uses_confirmed_article_and_quantity(self):
        request = [{"role": "user", "content": "Добавь 2 шт DEMO-LED-12"}]
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-15", 2, request))
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-12", 3, request))
        self.assertEqual(self.tools.get_cart("one"), [])
        offer = [{"role": "assistant", "content": "Добавить DEMO-LED-12 в корзину?"}, {"role": "user", "content": "Да"}]
        self.assertTrue(self.tools.add_to_cart("one", "DEMO-LED-12", 1, offer)["ok"])
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-15", 1, offer))
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-12", 3, [{"role": "user", "content": "Добавь 2 DEMO-LED-12"}]))
        self.assertIn("error", self.tools.add_to_cart("one", "DEMO-LED-12", 1, [{"role": "user", "content": "Добавь DEMO-LED-12 и DEMO-LED-15"}]))
        two_offer = [{"role": "assistant", "content": "Добавить 2 шт DEMO-LED-15 в корзину?"}, {"role": "user", "content": "Да"}]
        self.assertTrue(self.tools.add_to_cart("one", "DEMO-LED-15", 2, two_offer)["ok"])
        self.assertIn("error", self.tools.add_to_cart("two", "DEMO-LED-15", 3, two_offer))

    def test_cart_replay_does_not_add_twice(self):
        request = [{"role": "user", "content": "Добавь 2 шт DEMO-LED-12"}]
        first = self.tools.add_to_cart("one", "DEMO-LED-12", 2, request)
        second = self.tools.add_to_cart("one", "DEMO-LED-12", 2, request)
        self.assertEqual(first["qty"], 2)
        self.assertEqual(second["qty"], 2)
        self.assertEqual(second["cart_link"], "https://ekt.kz/cart")
        self.assertEqual(self.tools.get_cart("one")[0]["qty"], 2)

    def test_tool_confirmation_must_match_latest_user_message(self):
        request = [{"role": "user", "content": "Добавь 2 шт DEMO-LED-12"}]
        result = self.tools.dispatch("add_to_cart", {"article": "DEMO-LED-12", "qty": 2, "user_confirmation": "добавь 5"}, "one", request)
        self.assertIn("error", result)
        self.assertEqual(self.tools.get_cart("one"), [])

    def test_logic_branch_catalog_shape_and_analog_ranking(self):
        raw = [
            {"article": "ABB16", "name": "Автомат ABB 16 А", "category": "Автоматы", "specs": {"ток": "16 А", "полюса": "1"}, "stock": {"Алматы": 0}},
            {"article": "ABB25", "name": "Автомат ABB 25 А", "category": "Автоматы", "specs": {"ток": "25 А", "полюса": "1"}, "stock": {"Алматы": 3}},
            {"article": "SE16", "name": "Автомат Schneider 16 А", "category": "Автоматы", "specs": {"ток": "16 А", "полюса": "1"}, "stock": {"Алматы": 4}},
        ]
        catalog = Catalog(demo_mode=True)
        catalog._set_products([normalize_product(item) for item in raw], "demo")
        self.assertEqual(catalog.search("автомат ABB 16А")[0]["article"], "ABB16")
        self.assertEqual(catalog.analogs("ABB16")[0]["article"], "SE16")
        self.assertEqual(catalog.get("SE16")["stock"], 4)

    def test_five_step_logic_demo_conversation(self):
        raw = [
            {"article": "515291", "name": "Автомат ABB C16 16 А", "category": "Автоматические выключатели", "specs": {"ток": "16 А", "полюса": "1"}, "stock": {"Алматы": 0}, "price": 3500},
            {"article": "515292", "name": "Автомат Schneider C16 16 А", "category": "Автоматические выключатели", "specs": {"ток": "16 А", "полюса": "1"}, "stock": {"Алматы": 4}, "price": 2900},
            {"article": "515293", "name": "Автомат IEK C25 25 А", "category": "Автоматические выключатели", "specs": {"ток": "25 А", "полюса": "1"}, "stock": {"Алматы": 5}, "price": 1900},
        ]
        catalog = Catalog(demo_mode=True)
        catalog._set_products([normalize_product(item) for item in raw], "demo")
        tools = ShopTools(catalog)
        messages = []
        for question in (
            "Есть автомат ABB на 16А?",
            "Нет в наличии? А какой аналог посоветуете?",
            "Как у вас с оплатой и доставкой по Алматы?",
            "Да, добавь 2 штуки Schneider в корзину",
            "Дай ссылку на корзину",
        ):
            messages.append({"role": "user", "content": question})
            reply = run_demo_agent(messages, "one", tools)
            messages.append({"role": "assistant", "content": reply})
        self.assertIn("515291", messages[1]["content"])
        self.assertIn("515292", messages[3]["content"])
        self.assertIn("условия покупки", messages[5]["content"].casefold())
        self.assertEqual(tools.get_cart("one")[0]["qty"], 2)
        self.assertIn("https://ekt.kz/cart", messages[-1]["content"])

    def test_current_team_demo_script(self):
        messages = []
        for question in (
            "Есть автомат на 25 А?",
            "DEMO-AV-25 нет в наличии? Какой аналог посоветуете?",
            "Как у вас с оплатой и доставкой по Алматы? Есть минимальная партия?",
            "Да, добавь 2 шт DEMO-AV-16 в корзину",
            "Покажи корзину и дай ссылку",
        ):
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": run_demo_agent(messages, "team", self.tools)})
        self.assertIn("DEMO-AV-25", messages[1]["content"])
        self.assertIn("DEMO-AV-16", messages[3]["content"])
        self.assertEqual(self.tools.get_cart("team")[0]["qty"], 2)
        self.assertIn("https://ekt.kz/cart", messages[-1]["content"])

    def test_payment_data_is_stopped_before_model(self):
        with patch.dict(os.environ, {"DEMO_MODE": "1", "OPENAI_API_KEY": "test-key"}):
            with patch("main.run_openai_agent", side_effect=AssertionError("model must not receive payment data")):
                with TestClient(app) as client:
                    answer = client.post("/api/chat", json={"session_id": "payment", "messages": [{"role": "user", "content": "Вот моя карта 4400 0000 0000 0000, оплатите"}]})
                    self.assertEqual(answer.status_code, 200)
                    self.assertIn("Не отправляйте платёжные данные", answer.json()["reply"])
                    self.assertNotIn("4400", answer.json()["reply"])

    def test_payment_data_in_history_is_redacted_before_model(self):
        observed = []
        def capture(messages, *_):
            observed.extend(messages)
            return "Готово"
        with patch.dict(os.environ, {"DEMO_MODE": "1", "OPENAI_API_KEY": "test-key"}):
            with patch("main.run_openai_agent", side_effect=capture):
                with TestClient(app) as client:
                    answer = client.post("/api/chat", json={"session_id": "payment-history", "messages": [
                        {"role": "user", "content": "Карта 4400 0000 0000 0000"},
                        {"role": "assistant", "content": "Не отправляйте платёжные данные"},
                        {"role": "user", "content": "Покажи лампы"},
                    ]})
                    self.assertEqual(answer.status_code, 200)
        self.assertEqual(observed, [{"role": "user", "content": "Покажи лампы"}])
        self.assertNotIn("4400", json.dumps(observed, ensure_ascii=False))

    def test_merged_logic_assets_are_used_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            logic = Path(directory) / "logic"
            logic.mkdir()
            (Path(directory) / "catalog_demo.json").write_text(json.dumps([{"article": "BACKEND-1", "name": "Товар бэкенда", "stock": 2}], ensure_ascii=False), encoding="utf-8")
            (Path(directory) / "purchase_conditions.txt").write_text("Уточните условия у менеджера", encoding="utf-8")
            (logic / "purchase_conditions.txt").write_text("Условия из logic", encoding="utf-8")
            (logic / "system_prompt.txt").write_text("Промпт из logic", encoding="utf-8")
            with patch("catalog.BASE_DIR", Path(directory)), patch("agent.BASE_DIR", Path(directory)):
                catalog = Catalog(demo_mode=True)
                catalog.load()
                self.assertEqual(catalog.get("BACKEND-1")["stock"], 2)
                self.assertEqual(ShopTools(catalog).get_purchase_conditions(), "Условия из logic")
                self.assertEqual(asset_path("system_prompt.txt"), logic / "system_prompt.txt")
                live = Catalog(demo_mode=False)
                self.assertEqual(ShopTools(live).get_purchase_conditions(), "Уточните условия у менеджера")

    def test_unknown_stock_stays_unknown(self):
        product = normalize_product({"article": "X", "name": "Товар", "stores": [{"quantity": "unknown"}], "price": "NaN"})
        self.assertIsNone(product["stock"])
        self.assertEqual(product["availability"], "unknown")
        self.assertIsNone(product["price"])

    def test_live_catalog_failure_never_serves_demo_prices(self):
        with patch.dict(os.environ, {"DEMO_MODE": "0", "OPENAI_API_KEY": ""}):
            with patch.object(Catalog, "_load_live", side_effect=ValueError("offline")):
                with TestClient(app) as client:
                    health = client.get("/health")
                    self.assertEqual(health.status_code, 503)
                    self.assertEqual(health.json()["catalog_source"], "unavailable")
                    answer = client.post("/api/chat", json={"session_id": "live", "messages": [{"role": "user", "content": "Сколько стоит DEMO-LED-12?"}]})
                    self.assertEqual(answer.status_code, 200)
                    self.assertIn("недоступен", answer.json()["reply"])
                    self.assertEqual(answer.json()["cart"], [])

    def test_demo_questions_endpoint_matches_logic_asset(self):
        expected = [item["user"] for item in json.loads((Path(__file__).resolve().parents[1] / "logic" / "demo_questions.json").read_text(encoding="utf-8"))["script"]]
        with patch.dict(os.environ, {"DEMO_MODE": "1", "OPENAI_API_KEY": ""}):
            with TestClient(app) as client:
                response = client.get("/api/demo-questions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"questions": expected})

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
        self.assertEqual(answer, safe_reply("Покажи DEMO-LED-12", "facts"))
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
