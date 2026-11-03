"""
Unit tests for Stage 1 backend fixes according to approved TZ:
- Inverter and generator negation priority
- Protection against false street matches on adjectives (зимовий період, спортивна територія)
- Boulevard Tadeya Rylskoho normalization in ЖК Respublika
- Russian street name endings support (ул.Ракетная, 24)
- Pets allowed title priority
- Cross-district promoted ad filtering
"""

import unittest
from normalizer import KyivAddressParser, ResidentialNormalizer
from scraper import is_valid_rental_offer


class TestStage1Fixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = KyivAddressParser()

    def test_inverter_negation_priority(self):
        # Case ID11jacc: title states 'без інвертор'
        title = "Від власника (без інвертор) оренда 1к кв 40.9 кв.м"
        res = ResidentialNormalizer.extract_inverter(title)
        self.assertEqual(res, "no")

        # Inverter definitely present
        title_pos = "Оренда з інвертором та EcoFlow"
        self.assertEqual(ResidentialNormalizer.extract_inverter(title_pos), "yes")

    def test_generator_negation_priority(self):
        desc_no = "Оренда квартири, без генератора в будинку"
        self.assertEqual(ResidentialNormalizer.extract_generator({}, desc_no), "no")

        desc_yes = "Будинок з генератором на ліфт та опалення"
        self.assertEqual(ResidentialNormalizer.extract_generator({}, desc_yes), "yes")

    def test_seasonal_adjective_not_treated_as_street(self):
        # Case ID11qUDL: 'на зимовий період' must NOT match 'провулок Зимовий'
        text = "Встигніть орендувати автономну квартиру в центрі на зимовий період!!! За адресою: Горького, 109. ЖК Володимирський."
        street, house, district = self.parser.parse(text)
        self.assertEqual(street, "вулиця Антоновича")
        self.assertEqual(house, "109")
        self.assertEqual(district, "Голосіївський")

    def test_infrastructure_adjective_not_treated_as_street(self):
        # Case ID11dqg2: 'спортивна територія' must NOT match 'площа Спортивна'
        text = "У ЖК Нова Англія є: - спортивна територія та прогулянки"
        street, house, _ = self.parser.parse(text)
        self.assertNotEqual(street, "площа Спортивна")
        self.assertEqual(street, "")

    def test_respublika_tadeya_rylskoho_boulevard(self):
        # Case ID11AuGm: ЖК Respublika addresses belong to бульвар Тадея Рильського
        text = "Оренда 1к квартири в ЖК Respublika на бул. Тадея Рильського 4"
        street, house, district = self.parser.parse(text)
        self.assertEqual(street, "бульвар Тадея Рильського")
        self.assertEqual(house, "4")
        self.assertEqual(district, "Голосіївський")

        # Even if short 'Рильського 3' in ЖК Respublika
        text2 = "Оренда квартири в ЖК Республіка, Рильського 3"
        street2, house2, district2 = self.parser.parse(text2)
        self.assertEqual(street2, "бульвар Тадея Рильського")
        self.assertEqual(house2, "3")

    def test_russian_street_name_ending(self):
        # Case ID11xTss: 'ул.Ракетная, 24'
        text = "Сдам 1к квартиру м.Демеевская по ул.Ракетная, 24"
        street, house, district = self.parser.parse(text)
        self.assertEqual(street, "вулиця Ракетна")
        self.assertEqual(house, "24")
        self.assertEqual(district, "Голосіївський")

    def test_pets_title_priority_over_conflicting_checkbox(self):
        # Case ID10WgOP: title says 'можна з маленькою твариною', but checkbox has 'no'
        offer = {
            "title": "Укриття перша здача 1-кімнатна квартира тераса Лобановського 126Г можна з маленькою твариною",
            "url": "https://www.olx.ua/d/uk/obyavlenie/ukrittya-persha-zdacha-1-kmnatna-kvartira-terasa-lobanovskogo-126g-mozhna-z-malenkoyu-tvarinoyu-ID10WgOP.html",
            "params": [{"key": "pets", "value": {"key": ["no"]}}],
        }
        res = ResidentialNormalizer.extract_pets(offer, offer["title"])
        self.assertEqual(res, "yes")

    def test_cross_district_promoted_ad_rejection(self):
        # Ad is promoted from Оболонський district injected into Голосіївський search
        offer_obolon_promo = {
            "title": "Затишна невелика квартира в оренду Богатирська вул",
            "offer_type": "rent",
            "location": {"district": {"name": "Оболонський"}},
            "promotion": {"top_ad": True, "highlighted": True},
            "params": [
                {"key": "price", "value": 11500},
                {"key": "total_area", "value": 23},
            ],
        }
        # In Голосіївський slice, it MUST be rejected
        self.assertFalse(is_valid_rental_offer(offer_obolon_promo, "residential", target_district="Голосіївський"))
        # In Оболонський slice, it MUST be accepted
        self.assertTrue(is_valid_rental_offer(offer_obolon_promo, "residential", target_district="Оболонський"))


    def test_api_preview_csv_endpoint(self):
        import asyncio
        from app import preview_csv_file
        from fastapi import HTTPException

        async def _run():
            # Test valid file
            res = await preview_csv_file("olx_residential_kyiv.csv", limit=5)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["filename"], "olx_residential_kyiv.csv")
            self.assertGreaterEqual(len(res["headers"]), 20)
            self.assertLessEqual(len(res["rows"]), 5)

            # Test invalid traversal attempt
            with self.assertRaises(HTTPException) as cm:
                await preview_csv_file("../secret.csv")
            self.assertEqual(cm.exception.status_code, 400)

            # Test non-existent file
            with self.assertRaises(HTTPException) as cm:
                await preview_csv_file("non_existent_file_12345.csv")
            self.assertEqual(cm.exception.status_code, 404)

        asyncio.run(_run())

    def test_id11prtc_park_false_positive_rejection(self):
        # In ID11prTc, text mentions "парки: ВДНГ; Максима Рильського" and "ТРЦ Республіка"
        # Must NOT match "бульвар Тадея Рильського"
        text = "Поряд парки: ВДНГ; Максима Рильського; Феофанія. Поруч ТРЦ Республіка. Вул. Васильківська 45"
        street, house, district = self.parser.parse(text)
        self.assertNotEqual(street, "бульвар Тадея Рильського")
        self.assertEqual(street, "вулиця Васильківська")
        self.assertEqual(house, "45")

    def test_bc_clean_trailing_street_and_house(self):
        from normalizer import CommercialNormalizer
        # Should strip address/house suffix from BC name
        bc1 = CommercialNormalizer.extract_business_center_name("Оренда офісу в БЦ Кадор Жилянская 68")
        self.assertEqual(bc1, "Кадор")

        # If the string was purely an address, BC should be empty
        bc2 = CommercialNormalizer.extract_business_center_name("Оренда офісу в БЦ Малевича 86б")
        self.assertEqual(bc2, "")

    def test_white_box_renovation(self):
        # Key "6" -> "white_box"
        offer_key = {"params": [{"key": "repair", "value": "6"}]}
        res_key = ResidentialNormalizer.extract_renovation(offer_key, "")
        self.assertEqual(res_key, "white_box")

        # Text "під чистове оздоблення"
        res_text1 = ResidentialNormalizer.extract_renovation({}, "Квартира після будівельників, під чистове оздоблення")
        self.assertEqual(res_text1, "white_box")

        # Text "white box"
        res_text2 = ResidentialNormalizer.extract_renovation({}, "Стан - white box, готова до фінішного ремонту")
        self.assertEqual(res_text2, "white_box")

    def test_multilot_range_rejection(self):
        offer1 = {
            "title": "Оренда офісу 30-200 м2 в центрі",
            "params": [{"key": "price", "value": 10000}, {"key": "total_area", "value": 50}],
        }
        self.assertFalse(is_valid_rental_offer(offer1, "commercial"))

        offer2 = {
            "title": "Оренда приміщень від 30 м2 до 200 м2",
            "params": [{"key": "price", "value": 10000}, {"key": "total_area", "value": 50}],
        }
        self.assertFalse(is_valid_rental_offer(offer2, "commercial"))

    def test_get_stats_without_repeated_ddl(self):
        from database import get_stats
        stats = get_stats()
        self.assertIn("residential_count", stats)
        self.assertIn("commercial_count", stats)


if __name__ == "__main__":
    unittest.main()

