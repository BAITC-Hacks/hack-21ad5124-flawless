import unittest
from pathlib import Path

from agent import ShopTools, run_demo_agent
from catalog import Catalog
from guardrails import cart_success_reply, safe_reply


class GuidedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(demo_mode=True)
        self.catalog.load()
        self.tools = ShopTools(self.catalog)

    def test_product_search_does_not_make_customer_use_articles(self):
        reply = run_demo_agent(
            [{"role": "user", "content": "Хочу лампы"}],
            "selection",
            self.tools,
        )
        self.assertNotIn("DEMO-", reply)
        self.assertNotIn("артикул", reply.casefold())
        self.assertLessEqual(reply.count(") "), 3)
        self.assertIn("Выберите номер или название", reply)

    def test_number_selects_one_of_the_last_three_options(self):
        first = run_demo_agent(
            [{"role": "user", "content": "Хочу лампы"}],
            "number-selection",
            self.tools,
        )
        reply = run_demo_agent(
            [
                {"role": "user", "content": "Хочу лампы"},
                {"role": "assistant", "content": first},
                {"role": "user", "content": "2"},
            ],
            "number-selection",
            self.tools,
        )
        self.assertIn("Светодиодная лампа EKT 15 Вт", reply)
        self.assertIn("Добавить 1 шт", reply)
        self.assertNotIn("артикул", reply.casefold())

    def test_one_yes_after_specific_offer_adds_without_repeat_confirmation(self):
        offer = "Добавить 1 шт Светодиодная лампа EKT 12 Вт E27 4000 К в корзину?"
        reply = run_demo_agent(
            [
                {"role": "assistant", "content": offer},
                {"role": "user", "content": "Да"},
            ],
            "one-confirmation",
            self.tools,
        )
        self.assertIn("Добавлено в корзину", reply)
        self.assertNotIn("Ссылка", reply)
        self.assertNotIn("https://", reply)
        self.assertEqual(self.tools.get_cart("one-confirmation")[0]["qty"], 1)

    def test_safe_fallbacks_never_request_an_article(self):
        self.assertNotIn("артикул", safe_reply("помоги", "facts").casefold())
        self.assertNotIn("артикул", safe_reply("помоги", "tools").casefold())
        reply = cart_success_reply("добавь", {"name": "Лампа", "article": "INTERNAL-1", "qty": 1})
        self.assertNotIn("INTERNAL-1", reply)
        self.assertNotIn("http", reply)

    def test_system_prompt_encodes_selection_policy(self):
        prompt = (Path(__file__).parents[1] / "logic" / "system_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("Никогда не проси клиента назвать, уточнить или подтвердить артикул", prompt)
        self.assertIn("максимум 3", prompt)
        self.assertIn("Не перечисляй всю корзину", prompt)
        self.assertIn("Если ни одно действие не помогает", prompt)

    def test_attachment_ui_replaces_demo_questions(self):
        static = Path(__file__).parents[1] / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="photo-input"', html)
        self.assertIn('id="image-preview"', html)
        self.assertIn('id="find-by-photo"', html)
        self.assertIn('id="remove-photo"', html)
        self.assertNotIn('id="demo-button"', html)
        self.assertIn("URL.createObjectURL(file)", script)
        self.assertNotIn("loadDemoQuestions()", script)


if __name__ == "__main__":
    unittest.main()
