import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from catalog import normalize_product
from main import app


class StatelessDemoTests(unittest.TestCase):
    def test_live_catalog_still_uses_assistant_cart_and_hides_ekt_link(self):
        def load_sample(catalog):
            catalog._set_products([normalize_product({
                "article": "EKT-TEST", "name": "Тестовый выключатель", "category": "Низковольтная аппаратура",
                "price": 100, "quantity": 3,
            })], "live")

        def model_reply(messages, session_id, tools, model, api_key, action_state):
            result = tools.add_to_cart(session_id, "EKT-TEST", 2, messages)
            self.assertTrue(result["ok"])
            return "Добавлено. Ссылка: https://ekt.kz/cart"

        settings = {"DEMO_MODE": "0", "OPENAI_API_KEY": "test-key", "CART_SIGNING_KEY": "test-secret-for-vercel-cart-tokens-12345"}
        with patch.dict(os.environ, settings), patch("main.Catalog.load", new=load_sample), patch("main.run_openai_agent", side_effect=model_reply):
            with TestClient(app) as client:
                self.assertIn("EKT-TEST", client.get("/api/demo-questions").json()["questions"][0])
                response = client.post("/api/chat", json={"session_id": "live-test", "messages": [{"role": "user", "content": "Добавь 2 шт EKT-TEST в корзину"}]})
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["cart"][0]["qty"], 2)
                self.assertEqual(data["assistant_source"], "openai")
                self.assertIn("/demo-cart?token=", data["cart_link"])
                self.assertNotIn("https://ekt.kz/cart", data["reply"])
                self.assertIn("EKT-TEST", client.get(data["cart_link"]).text)

    def test_cart_survives_new_function_instance_and_link_shows_items(self):
        settings = {"DEMO_MODE": "1", "OPENAI_API_KEY": "", "CART_SIGNING_KEY": "test-secret-for-vercel-cart-tokens-12345"}
        with patch.dict(os.environ, settings):
            with TestClient(app) as client:
                request = {"session_id": "vercel-session", "messages": [{"role": "user", "content": "Добавь 2 шт DEMO-AV-16"}]}
                first = client.post("/api/chat", json=request)
                self.assertEqual(first.status_code, 200)
                data = first.json()
                self.assertEqual(data["cart"][0]["qty"], 2)
                self.assertEqual(data["assistant_source"], "demo")
                self.assertIn("/demo-cart?token=", data["cart_link"])
                self.assertNotIn("token=", data["reply"])
                self.assertIn("DEMO-AV-16", client.get(data["cart_link"]).text)

            # A fresh lifespan simulates a different Vercel Function instance.
            with TestClient(app) as client:
                request["cart_token"] = data["cart_token"]
                replay = client.post("/api/chat", json=request)
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json()["cart"][0]["qty"], 2)
                request["messages"] = [{"role": "user", "content": "Покажи корзину"}]
                resumed = client.post("/api/chat", json=request)
                self.assertEqual(resumed.json()["cart"][0]["qty"], 2)

    def test_token_is_bound_to_session_and_rejects_changes(self):
        settings = {"DEMO_MODE": "1", "OPENAI_API_KEY": "", "CART_SIGNING_KEY": "test-secret-for-vercel-cart-tokens-12345"}
        with patch.dict(os.environ, settings):
            with TestClient(app) as client:
                first = client.post("/api/chat", json={"session_id": "one", "messages": [{"role": "user", "content": "Добавь 1 шт DEMO-AV-16"}]}).json()
                wrong_session = client.post("/api/chat", json={"session_id": "other", "cart_token": first["cart_token"], "messages": [{"role": "user", "content": "Покажи корзину"}]})
                self.assertEqual(wrong_session.status_code, 400)
                tampered = first["cart_token"][:-1] + ("a" if first["cart_token"][-1] != "a" else "b")
                self.assertEqual(client.get("/demo-cart", params={"token": tampered}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
