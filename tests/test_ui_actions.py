import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent import ShopTools, TOOLS, run_openai_agent
from catalog import Catalog, normalize_product
from main import app
from ui_actions import ActionState, build_actions


class UIActionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(demo_mode=True)
        self.catalog.load()

    def actions(self, proposals, reply, latest, catalog=None, cart_before=None, cart_after=None, observed=None):
        return build_actions(proposals, reply, latest, catalog or self.catalog,
                             cart_before or [], cart_after or [], observed)

    def test_available_product_has_add_action_with_backend_stock(self):
        actions = self.actions([{"type": "add_to_cart", "article": "DEMO-LED-12"}],
                               "Светодиодная лампа DEMO-LED-12, цена 1290 ₸.", "Есть DEMO-LED-12?")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].article, "DEMO-LED-12")
        self.assertEqual(actions[0].max_qty, 24)
        self.assertEqual(actions[0].qty, 1)

    def test_zero_or_unknown_stock_cannot_be_added(self):
        self.assertEqual(self.actions([{"type": "add_to_cart", "article": "DEMO-AV-25"}],
                                      "DEMO-AV-25 нет в наличии.", "Есть DEMO-AV-25?"), [])
        catalog = Catalog(demo_mode=True)
        catalog._set_products([normalize_product({"article": "UNKNOWN-STOCK", "name": "Выключатель",
                                                  "price": 100, "stock": None})], "demo")
        self.assertEqual(self.actions([{"type": "add_to_cart", "article": "UNKNOWN-STOCK"}],
                                      "Остаток UNKNOWN-STOCK неизвестен.", "Есть UNKNOWN-STOCK?", catalog), [])

    def test_certificates_and_analogs_require_real_catalog_results(self):
        proposed = [{"type": "show_certificates", "article": "DEMO-LED-12"},
                    {"type": "show_analogs", "article": "DEMO-LED-12"}]
        actions = self.actions(proposed, "Есть лампа DEMO-LED-12.", "Есть DEMO-LED-12?")
        self.assertEqual([action.type for action in actions], ["show_analogs"])
        self.assertEqual(self.actions([{"type": "show_analogs", "article": "DEMO-AV-16"}],
                                      "Есть DEMO-AV-16.", "Есть DEMO-AV-16?"), [])
        self.assertEqual(self.actions([{"type": "show_analogs", "article": "DEMO-LED-12"}],
                                      "Аналог DEMO-LED-15 найден.", "Покажи аналог DEMO-LED-12"), [])

    def test_warehouse_action_only_when_location_would_add_information(self):
        proposal = [{"type": "show_availability", "article": "DEMO-LED-12"}]
        self.assertEqual([item.type for item in self.actions(
            proposal, "DEMO-LED-12: остаток 24 шт.", "Есть DEMO-LED-12?")],
            ["show_availability"])
        self.assertEqual(self.actions(proposal, "DEMO-LED-12: Алматы — 24 шт.",
                                      "Есть DEMO-LED-12?"), [])
        self.assertEqual(self.actions([{"type": "show_availability", "article": "DEMO-AV-25"}],
                                      "DEMO-AV-25 отсутствует.", "Есть DEMO-AV-25?"), [])

    def test_unknown_duplicate_model_fields_and_more_than_six_are_safe(self):
        proposals = [{"type": "open_cart"}, {"type": "javascript:alert(1)"},
                     {"type": "add_to_cart", "article": "DEMO-LED-12", "max_qty": 99999,
                      "label": "<script>", "message": "javascript:alert(1)"},
                     {"type": "add_to_cart", "article": "DEMO-LED-15"},
                     {"type": "show_analogs", "article": "DEMO-LED-12"},
                     {"type": "show_details", "article": "DEMO-LED-12"},
                     {"type": "show_availability", "article": "DEMO-LED-12"},
                     {"type": "clarify"}, {"type": "continue_search"}, {"type": "payment"}]
        actions = self.actions(proposals, "Найдены лампы DEMO-LED-12 и DEMO-LED-15.", "Покажи лампы")
        self.assertEqual(len(actions), 6)
        self.assertEqual(len({action.type for action in actions}), 6)
        self.assertEqual(actions[0].max_qty, 24)
        self.assertNotIn("javascript:", json.dumps([action.model_dump() for action in actions]))
        self.assertNotIn("<script>", json.dumps([action.model_dump() for action in actions]))

    def test_russian_and_kazakh_text_comes_from_backend(self):
        proposed = [{"type": "add_to_cart", "article": "DEMO-LED-12"}]
        russian = self.actions(proposed, "Лампа DEMO-LED-12 есть.", "Есть DEMO-LED-12?")[0]
        kazakh = self.actions(proposed, "Иә, DEMO-LED-12 қоймада бар.", "DEMO-LED-12 бар ма?")[0]
        self.assertEqual((russian.label, russian.message),
                         ("Добавить", "Добавь {qty} шт DEMO-LED-12 в корзину"))
        self.assertEqual((kazakh.label, kazakh.message),
                         ("Себетке қосу", "DEMO-LED-12 тауарынан {qty} дана себетке қос"))

    def test_model_tool_records_intentions_without_changing_cart(self):
        def call(name, args, number):
            return SimpleNamespace(id=f"call_{number}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))

        batches = [
            [call("search_products", {"query": "лампа"}, 1)],
            [call("set_ui_actions", {"actions": [
                {"type": "add_to_cart", "article": "DEMO-LED-12", "max_qty": 99999,
                 "label": "Ненадёжная подпись", "message": "javascript:alert(1)"},
                {"type": "show_certificates", "article": "DEMO-LED-12"},
                {"type": "open_cart"},
            ]}, 2)],
            [],
        ]
        responses = iter([SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            tool_calls=batch, content="Лампы DEMO-LED-12 и DEMO-LED-15 в каталоге.",
            model_dump=lambda batch=batch, **_: {"role": "assistant", "tool_calls": [
                {"id": item.id, "type": "function", "function": {"name": item.function.name,
                 "arguments": item.function.arguments}} for item in batch]}))]) for batch in batches])
        fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: next(responses))))
        tools = ShopTools(self.catalog)
        state = ActionState()
        with patch("openai.OpenAI", return_value=fake):
            reply = run_openai_agent([{"role": "user", "content": "Покажи лампы"}], "one", tools,
                                     "test-model", "test-key", state)
        self.assertTrue(state.called)
        self.assertEqual(tools.get_cart("one"), [])
        actions = self.actions(state.proposed, reply, "Покажи лампы", observed=state.observed_articles)
        self.assertEqual([action.type for action in actions], ["add_to_cart"])
        self.assertEqual(actions[0].max_qty, 24)
        schema = next(tool for tool in TOOLS if tool["function"]["name"] == "set_ui_actions")
        self.assertNotIn("max_qty", schema["function"]["parameters"]["properties"]["actions"]["items"]["properties"])

    def test_legacy_request_cart_token_and_one_explicit_add(self):
        settings = {"DEMO_MODE": "1", "OPENAI_API_KEY": "", "CART_SIGNING_KEY": "test-secret-for-ui-actions-cart-state-123"}
        with patch.dict(os.environ, settings), TestClient(app) as client:
            first = client.post("/api/chat", json={"session_id": "actions-session",
                                                  "messages": [{"role": "user", "content": "Есть DEMO-LED-12?"}]})
            self.assertEqual(first.status_code, 200)
            initial = first.json()
            self.assertEqual(initial["cart"], [])
            action = next(action for action in initial["actions"] if action["type"] == "add_to_cart")
            self.assertEqual(action["max_qty"], 24)
            self.assertIsNotNone(initial["cart_token"])

            messages = [{"role": "user", "content": "Есть DEMO-LED-12?"},
                        {"role": "assistant", "content": initial["reply"]},
                        {"role": "user", "content": action["message"].replace("{qty}", "2")}]
            request = {"session_id": "actions-session", "messages": messages, "cart_token": initial["cart_token"]}
            added = client.post("/api/chat", json=request).json()
            self.assertEqual(added["cart"][0]["qty"], 2)
            self.assertIsNotNone(added["cart_token"])
            request["cart_token"] = added["cart_token"]
            repeated = client.post("/api/chat", json=request).json()
            self.assertEqual(repeated["cart"][0]["qty"], 2)

    def test_cart_edit_actions_require_explicit_user_commands(self):
        tools = ShopTools(self.catalog)
        tools.add_to_cart("one", "DEMO-LED-12", 2, [{"role": "user", "content": "Добавь 2 шт DEMO-LED-12"}])
        self.assertIn("error", tools.change_quantity("one", "DEMO-LED-12", 3,
                                                      [{"role": "user", "content": "Покажи корзину"}]))
        self.assertEqual(tools.change_quantity("one", "DEMO-LED-12", 3,
                                               [{"role": "user", "content": "Измени количество DEMO-LED-12 в корзине на 3 шт"}])["qty"], 3)
        self.assertIn("error", tools.remove_from_cart("one", "DEMO-LED-12", [{"role": "user", "content": "Покажи товар"}]))
        self.assertTrue(tools.remove_from_cart("one", "DEMO-LED-12", [{"role": "user", "content": "Удали DEMO-LED-12 из корзины"}])["ok"])
        self.assertEqual(tools.get_cart("one"), [])


if __name__ == "__main__":
    unittest.main()
