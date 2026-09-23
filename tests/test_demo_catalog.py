import json
import unittest
from pathlib import Path

from agent import ShopTools, run_demo_agent
from catalog import Catalog


class DemoCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(__file__).resolve().parents[1] / "catalog_demo.json"
        cls.raw = json.loads(cls.path.read_text(encoding="utf-8"))
        cls.catalog = Catalog(demo_mode=True)
        cls.catalog.load()

    def test_catalog_is_broad_and_structurally_valid(self):
        self.assertGreaterEqual(len(self.raw), 30)
        self.assertLessEqual(len(self.raw), 50)
        self.assertEqual(len({item["id"] for item in self.raw}), len(self.raw))
        self.assertEqual(len({item["article"] for item in self.raw}), len(self.raw))
        for item in self.raw:
            with self.subTest(article=item.get("article")):
                for field in ("id", "article", "name", "category", "price", "stock"):
                    self.assertIn(field, item)
                self.assertTrue(item["article"].startswith("DEMO-"))
                self.assertIsInstance(item["characteristics"], dict)
                self.assertTrue(item["characteristics"])
                self.assertGreaterEqual(item["price"], 0)
                self.assertGreaterEqual(item["stock"], 0)

    def test_off_script_queries_have_demo_results(self):
        expected = {
            "кабель ВВГ 3х2.5": "DEMO-CAB-VVG-3X25",
            "автомат ABB 40А": "DEMO-ABB-SH201-C40",
            "светильник 36 Вт": "DEMO-LUM-PANEL-36",
        }
        for query, article in expected.items():
            with self.subTest(query=query):
                self.assertIn(article, {item["article"] for item in self.catalog.search(query)})

    def test_off_script_queries_work_through_demo_agent(self):
        tools = ShopTools(self.catalog)
        expected = {
            "Покажи кабель ВВГ 3х2.5": "Кабель ВВГнг-LS 3х2.5 мм² медный",
            "Есть автомат ABB на 40А?": "Автоматический выключатель ABB SH201 C40 1P 40 А",
            "Найди светильник на 36 Вт": "Светодиодный светильник",
        }
        for index, (query, product_name) in enumerate(expected.items()):
            with self.subTest(query=query):
                reply = run_demo_agent([{"role": "user", "content": query}], f"off-script-{index}", tools)
                self.assertIn(product_name, reply)
                self.assertNotIn("DEMO-", reply)

    def test_canonical_demo_products_are_unchanged(self):
        expected = {
            "DEMO-LED-12": ("Светодиодная лампа EKT 12 Вт E27 4000 К", 1290, 24),
            "DEMO-LED-15": ("Светодиодная лампа EKT 15 Вт E27 4000 К", 1590, 11),
            "DEMO-AV-16": ("Автоматический выключатель 1P 16 А", 2150, 8),
            "DEMO-AV-25": ("Автоматический выключатель 1P 25 А", 2450, 0),
        }
        for article, facts in expected.items():
            product = self.catalog.get(article)
            self.assertIsNotNone(product)
            self.assertEqual((product["name"], product["price"], product["stock"]), facts)


if __name__ == "__main__":
    unittest.main()
