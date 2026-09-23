import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import MAX_CART_QUANTITY, ShopTools, parse_quantity, purchase_confirmed, run_demo_agent
from catalog import Catalog, normalize_product


class AdversarialCartTests(unittest.TestCase):
    def setUp(self):
        demo_file = Path(__file__).resolve().parents[1] / "catalog_demo.json"
        raw = json.loads(demo_file.read_text(encoding="utf-8"))
        self.catalog = Catalog(demo_mode=True)
        self.catalog._set_products([normalize_product(item) for item in raw], "demo")
        self.tools = ShopTools(self.catalog)

    @staticmethod
    def user(text):
        return [{"role": "user", "content": text}]

    @staticmethod
    def offer(text, answer="Да"):
        return [
            {"role": "assistant", "content": text},
            {"role": "user", "content": answer},
        ]

    def test_quantity_status_absent(self):
        parsed = parse_quantity("Добавь DEMO-LED-12")
        self.assertEqual((parsed.status, parsed.value), ("absent", None))

    def test_quantity_digit_with_unit(self):
        self.assertEqual(parse_quantity("Добавь 7 шт DEMO-LED-12"), ("valid", 7))

    def test_quantity_bare_digit_after_cart_verb(self):
        self.assertEqual(parse_quantity("Добавляй 7 DEMO-LED-12"), ("valid", 7))

    def test_quantity_russian_words_one_through_ten(self):
        words = ["один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять"]
        for value, word in enumerate(words, 1):
            with self.subTest(word=word):
                self.assertEqual(parse_quantity(f"Оформляй {word} DEMO-LED-12"), ("valid", value))

    def test_quantity_russian_gender_forms(self):
        self.assertEqual(parse_quantity("Добавь одна штука DEMO-LED-12"), ("valid", 1))
        self.assertEqual(parse_quantity("Добавь две штуки DEMO-LED-12"), ("valid", 2))

    def test_quantity_kazakh_words_one_through_ten_with_dana(self):
        words = ["бір", "екі", "үш", "төрт", "бес", "алты", "жеті", "сегіз", "тоғыз", "он"]
        for value, word in enumerate(words, 1):
            with self.subTest(word=word):
                self.assertEqual(parse_quantity(f"DEMO-AV-16 тауарынан {word} дана себетке қос"), ("valid", value))

    def test_quantity_negative_is_invalid(self):
        self.assertEqual(parse_quantity("Добавь -5 шт DEMO-LED-12").status, "invalid")

    def test_quantity_zero_is_invalid(self):
        self.assertEqual(parse_quantity("Добавь 0 DEMO-LED-12").status, "invalid")

    def test_quantity_decimal_dot_and_comma_are_invalid(self):
        for text in ("Добавь 2.5 шт DEMO-LED-12", "Добавь 2,5 DEMO-LED-12"):
            with self.subTest(text=text):
                self.assertEqual(parse_quantity(text).status, "invalid")

    def test_quantity_million_and_vague_words_are_invalid(self):
        for word in ("миллион", "много", "несколько", "двенадцать", "тридцать", "пятьсот", "two"):
            with self.subTest(word=word):
                self.assertEqual(parse_quantity(f"Добавь {word} DEMO-LED-12").status, "invalid")
        self.assertEqual(parse_quantity("DEMO-AV-16 тауарынан он бір дана себетке қос").status, "invalid")

    def test_unknown_word_before_quantity_unit_is_invalid(self):
        self.assertEqual(parse_quantity("Добавь абракадабра штук DEMO-LED-12").status, "invalid")

    def test_quantity_limit_is_enforced_by_parser(self):
        self.assertEqual(parse_quantity(f"Добавь {MAX_CART_QUANTITY} DEMO-LED-12"), ("valid", MAX_CART_QUANTITY))
        self.assertEqual(parse_quantity(f"Добавь {MAX_CART_QUANTITY + 1} DEMO-LED-12").status, "invalid")
        self.assertEqual(parse_quantity(f"Добавь {'9' * 5000} DEMO-LED-12").status, "invalid")

    def test_conflicting_quantities_are_invalid(self):
        self.assertEqual(parse_quantity("Добавь 2 шт или 3 шт DEMO-LED-12").status, "invalid")
        for text in ("Добавь 2-3 штуки DEMO-LED-12", "Добавь две или три штуки DEMO-LED-12"):
            with self.subTest(text=text):
                self.assertEqual(parse_quantity(text).status, "invalid")

    def test_article_digits_are_not_a_quantity(self):
        self.assertEqual(parse_quantity("Добавь DEMO-LED-12").status, "absent")

    def test_numeric_article_is_not_mistaken_for_quantity_by_gate(self):
        catalog = Catalog(demo_mode=True)
        catalog._set_products([
            normalize_product({"article": "515291", "name": "Реле EKT", "stock": 3})
        ], "demo")
        tools = ShopTools(catalog)
        messages = self.user("Добавь 515291")
        self.assertTrue(tools.add_to_cart("numeric-article", "515291", 1, messages)["ok"])

    def test_bare_quantity_before_verb_is_rejected_instead_of_defaulting(self):
        messages = self.user("2 DEMO-LED-12 добавь")
        self.assertFalse(purchase_confirmed(messages, self.catalog))
        self.assertIn("error", self.tools.add_to_cart("pre-verb", "DEMO-LED-12", 1, messages))

    def test_direct_verb_variants_are_confirmations(self):
        for text in (
            "Добавляй DEMO-LED-12",
            "Оформляй DEMO-LED-12",
            "Беру DEMO-LED-12",
            "Берём две DEMO-LED-12",
        ):
            with self.subTest(text=text):
                self.assertTrue(purchase_confirmed(self.user(text), self.catalog))

    def test_throw_verb_requires_cart_word(self):
        self.assertFalse(purchase_confirmed(self.user("Кидай DEMO-LED-12"), self.catalog))
        self.assertTrue(purchase_confirmed(self.user("Кидай в корзину 2 DEMO-LED-12"), self.catalog))

    def test_two_such_works_only_after_concrete_offer(self):
        messages = self.offer("Добавить DEMO-LED-12 в корзину?", "Кидай в корзину два таких")
        result = self.tools.add_to_cart("such", "DEMO-LED-12", 2, messages)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qty"], 2)

    def test_take_only_three_uses_the_immediate_concrete_offer(self):
        messages = self.offer("Добавить DEMO-LED-12 в корзину?", "Беру, только три штуки")
        result = self.tools.add_to_cart("take-three", "DEMO-LED-12", 3, messages)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qty"], 3)

    def test_two_such_without_offer_is_rejected(self):
        messages = self.user("Кидай в корзину два таких")
        self.assertFalse(purchase_confirmed(messages, self.catalog))
        self.assertIn("error", self.tools.add_to_cart("such", "DEMO-LED-12", 2, messages))

    def test_cancellation_words_always_reject(self):
        phrases = (
            "Добавь DEMO-LED-12, хотя нет",
            "Добавь DEMO-LED-12, не надо",
            "Добавь DEMO-LED-12, передумал",
            "Добавь DEMO-LED-12, отмена",
            "DEMO-LED-12 себетке қос, жоқ",
            "DEMO-LED-12 керек емес, себетке қос",
            "DEMO-LED-12 себетке қоспа",
            "DEMO-LED-12 себетке салма",
        )
        for text in phrases:
            with self.subTest(text=text):
                self.assertFalse(purchase_confirmed(self.user(text), self.catalog))

    def test_trailing_negation_prevents_mutation(self):
        messages = self.user("Добавь 2 шт DEMO-LED-12, хотя нет, не надо")
        self.assertIn("error", self.tools.add_to_cart("cancel", "DEMO-LED-12", 2, messages))
        self.assertEqual(self.tools.get_cart("cancel"), [])

    def test_question_mark_request_is_not_confirmation(self):
        messages = self.user("Добавишь 2 шт DEMO-LED-12 в корзину?")
        self.assertFalse(purchase_confirmed(messages, self.catalog))

    def test_modal_question_without_question_mark_is_not_confirmation(self):
        for text in (
            "Можно добавить DEMO-LED-12",
            "Можете добавить DEMO-LED-12",
            "Добавить ли DEMO-LED-12",
            "Могу добавить DEMO-LED-12",
        ):
            with self.subTest(text=text):
                self.assertFalse(purchase_confirmed(self.user(text), self.catalog))

    def test_generic_consents_require_and_accept_concrete_offer(self):
        for index, answer in enumerate(("Ну давай", "Да конечно", "Хорошо", "Ок погнали", "Әрине")):
            with self.subTest(answer=answer):
                messages = self.offer("Добавить DEMO-LED-12 в корзину?", answer)
                self.assertTrue(purchase_confirmed(messages, self.catalog))
                self.assertTrue(self.tools.add_to_cart(f"consent-{index}", "DEMO-LED-12", 1, messages)["ok"])

    def test_generic_consent_without_offer_is_rejected(self):
        for answer in ("Ну давай", "Да конечно", "Хорошо", "Ок погнали", "Әрине"):
            with self.subTest(answer=answer):
                self.assertFalse(purchase_confirmed(self.user(answer), self.catalog))

    def test_offer_must_be_immediately_previous_assistant_message(self):
        messages = [
            {"role": "assistant", "content": "Добавить DEMO-LED-12 в корзину?"},
            {"role": "user", "content": "А сколько стоит?"},
            {"role": "user", "content": "Да"},
        ]
        self.assertFalse(purchase_confirmed(messages, self.catalog))

    def test_offer_by_exact_product_name_is_concrete(self):
        name = self.catalog.get("DEMO-AV-16")["name"]
        messages = self.offer(f"2 дана {name} себетке қосайын ба?", "Иә")
        self.assertTrue(self.tools.add_to_cart("name-offer", "DEMO-AV-16", 2, messages)["ok"])

    def test_multi_product_offer_is_ambiguous(self):
        messages = self.offer(
            "DEMO-LED-12, DEMO-LED-15 и DEMO-AV-16. Какой добавить в корзину?",
            "Да",
        )
        self.assertFalse(purchase_confirmed(messages, self.catalog))

    def test_negative_assistant_offer_cannot_be_confirmed(self):
        messages = self.offer("Не надо добавлять DEMO-LED-12 в корзину.", "Да")
        self.assertFalse(purchase_confirmed(messages, self.catalog))

    def test_quantity_is_inherited_from_concrete_offer(self):
        messages = self.offer("Добавить 3 шт DEMO-LED-12 в корзину?", "Хорошо")
        result = self.tools.add_to_cart("inherit", "DEMO-LED-12", 3, messages)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qty"], 3)

    def test_stock_count_before_offer_is_not_inherited_as_order_quantity(self):
        messages = self.offer("В наличии 5 шт. Добавить DEMO-LED-12 в корзину?", "Да")
        self.assertTrue(self.tools.add_to_cart("stock-text", "DEMO-LED-12", 1, messages)["ok"])
        self.assertIn("error", self.tools.add_to_cart("stock-text-2", "DEMO-LED-12", 5, messages))

    def test_model_quantity_must_equal_user_quantity(self):
        messages = self.user("Добавь две штуки DEMO-LED-12")
        self.assertIn("error", self.tools.add_to_cart("mismatch", "DEMO-LED-12", 3, messages))
        self.assertEqual(self.tools.get_cart("mismatch"), [])

    def test_model_article_must_equal_user_article(self):
        messages = self.user("Добавь 2 шт DEMO-LED-12")
        self.assertIn("error", self.tools.add_to_cart("article", "DEMO-LED-15", 2, messages))

    def test_two_articles_in_one_instruction_are_rejected(self):
        messages = self.user("Добавь DEMO-LED-12 и DEMO-LED-15")
        self.assertFalse(purchase_confirmed(messages, self.catalog))

    def test_three_products_second_and_that_are_fail_safe(self):
        assistant = "Варианты: DEMO-LED-12, DEMO-LED-15, DEMO-AV-16. Какой добавить в корзину?"
        for answer in ("Добавь второй", "Добавь тот"):
            with self.subTest(answer=answer):
                messages = self.offer(assistant, answer)
                self.assertFalse(purchase_confirmed(messages, self.catalog))
                self.assertIn("error", self.tools.add_to_cart(answer, "DEMO-LED-15", 1, messages))

    def test_identical_replay_is_idempotent(self):
        messages = self.user("Добавь 2 DEMO-LED-12")
        first = self.tools.add_to_cart("replay", "DEMO-LED-12", 2, messages)
        second = self.tools.add_to_cart("replay", "DEMO-LED-12", 2, messages)
        self.assertEqual((first["qty"], second["qty"]), (2, 2))

    def test_concurrent_identical_replay_is_idempotent(self):
        messages = self.user("Добавь 2 DEMO-LED-12")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: self.tools.add_to_cart("concurrent", "DEMO-LED-12", 2, messages),
                range(16),
            ))
        self.assertTrue(all(result.get("ok") for result in results))
        self.assertEqual(self.tools.get_cart("concurrent")[0]["qty"], 2)

    def test_identical_replay_is_isolated_between_sessions(self):
        messages = self.user("Добавь 2 DEMO-LED-12")
        self.tools.add_to_cart("session-a", "DEMO-LED-12", 2, messages)
        self.tools.add_to_cart("session-b", "DEMO-LED-12", 2, messages)
        self.assertEqual(self.tools.get_cart("session-a")[0]["qty"], 2)
        self.assertEqual(self.tools.get_cart("session-b")[0]["qty"], 2)

    def test_two_flows_with_same_yes_bind_to_their_own_offer(self):
        first = self.offer("Добавить 2 шт DEMO-LED-12 в корзину?", "Да")
        second = self.offer("Добавить 3 шт DEMO-LED-15 в корзину?", "Да")
        self.assertTrue(self.tools.add_to_cart("dual", "DEMO-LED-12", 2, first)["ok"])
        self.assertTrue(self.tools.add_to_cart("dual", "DEMO-LED-15", 3, second)["ok"])
        cart = {item["article"]: item["qty"] for item in self.tools.get_cart("dual")}
        self.assertEqual(cart, {"DEMO-LED-12": 2, "DEMO-LED-15": 3})

    def test_demo_agent_uses_shared_word_quantity_parser(self):
        messages = self.user("Оформляй три DEMO-LED-12")
        reply = run_demo_agent(messages, "demo-word", self.tools)
        self.assertIn("3 шт", reply)
        self.assertEqual(self.tools.get_cart("demo-word")[0]["qty"], 3)

    def test_demo_agent_does_not_mutate_on_invalid_quantity(self):
        for index, text in enumerate((
            "Добавь -5 шт DEMO-LED-12",
            "Добавь 2,5 шт DEMO-LED-12",
            "Добавь миллион DEMO-LED-12",
        )):
            with self.subTest(text=text):
                run_demo_agent(self.user(text), f"demo-invalid-{index}", self.tools)
                self.assertEqual(self.tools.get_cart(f"demo-invalid-{index}"), [])

    def test_tool_quantity_cap_is_checked_before_stock(self):
        messages = self.user(f"Добавь {MAX_CART_QUANTITY} шт DEMO-LED-12")
        result = self.tools.add_to_cart("cap", "DEMO-LED-12", MAX_CART_QUANTITY + 1, messages)
        self.assertIn("error", result)
        self.assertNotIn("available", result)

    def test_boolean_quantity_is_rejected(self):
        messages = self.user("Добавь DEMO-LED-12")
        self.assertIn("error", self.tools.add_to_cart("bool", "DEMO-LED-12", True, messages))

    def test_kazakh_direct_command_mutates_exact_quantity(self):
        messages = self.user("DEMO-AV-16 тауарынан үш дана себетке қос")
        result = self.tools.add_to_cart("kz", "DEMO-AV-16", 3, messages)
        self.assertTrue(result["ok"])
        self.assertEqual(result["qty"], 3)


if __name__ == "__main__":
    unittest.main()
