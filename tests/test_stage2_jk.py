"""
Unit tests for Stage 2: Residential Complex (ЖК) Rule Implementation in OLX Normalizer.

Verifies:
1. Compilation and integrity of KYIV_RESIDENTIAL_COMPLEXES dictionary.
2. Direct and alias extraction (Ukrainian, Russian, Latin transliterations).
3. Anti-hallucination: rejection of metro stations, recreation zones, and street collisions without explicit ЖК indicator.
4. Fallback to generic quoted residential complexes.
5. OLX Residential Normalizer rule:
   - if house is missing and no street was found, street = f"ЖК {jk_name}"
   - if house is present, street is preserved as canonical street
   - if street is present, street is preserved as canonical street
   - district is automatically resolved from ЖК when missing
6. OLX Commercial Normalizer parity for commercial premises in residential complexes.
7. KyivAddressParser fallback_jk flag backward-compatibility.
"""

import unittest
from normalizer import (
    KYIV_RESIDENTIAL_COMPLEXES,
    CommercialNormalizer,
    KyivAddressParser,
    ResidentialNormalizer,
    extract_residential_complex,
    get_address_parser,
    normalize_commercial_offer,
    normalize_residential_offer,
)


class TestStage2ResidentialComplex(unittest.TestCase):
    def setUp(self):
        self.parser = get_address_parser()
        self.res_normalizer = ResidentialNormalizer()
        self.com_normalizer = CommercialNormalizer()

    def test_jk_dictionary_contents(self):
        """Verifies dictionary completeness and metadata."""
        self.assertGreaterEqual(len(KYIV_RESIDENTIAL_COMPLEXES), 50)
        required_complexes = [
            "Нова Англія",
            "Respublika",
            "Комфорт Таун",
            "Great",
            "Варшавський",
            "Файна Таун",
            "Park Land",
            "Svitlo Park",
            "Венеція",
            "Кришталеві Джерела",
            "Еврика",
            "Володимирський",
            "Новопечерські Липки",
            "Бульвар Фонтанів",
            "Французький квартал",
            "Tetris Hall",
            "Jack House",
            "Taryan Towers",
            "RiverStone",
            "Seven",
            "Зарічний",
        ]
        for name in required_complexes:
            self.assertIn(name, KYIV_RESIDENTIAL_COMPLEXES)
            data = KYIV_RESIDENTIAL_COMPLEXES[name]
            self.assertIn("district", data)
            self.assertIn("aliases", data)
            self.assertIn("distinct", data)
            self.assertTrue(len(data["aliases"]) > 0)

    def test_extract_residential_complex_direct(self):
        """Tests extraction of canonical residential complexes from text."""
        cases = [
            ("Оренда 1-кімнатної квартири в ЖК «Нова Англія»", "Нова Англія", "Голосіївський"),
            ("Оренда 2к квартири у ЖК Great на Осокорках", "Great", "Дарницький"),
            ("Здам квартиру в ЖК Комфорт Таун, Соборності", "Комфорт Таун", "Дніпровський"),
            ("Оренда в ЖК Варшавський Плюс", "Варшавський", "Подільський"),
            ("Квартира в ЖК Respublika, метро Теремки", "Respublika", "Голосіївський"),
            ("Оренда 1к квартири ЖК Svitlo Park", "Svitlo Park", "Голосіївський"),
            ("Здам смарт-квартиру в ЖК \"Парк Ленд\"", "Park Land", "Голосіївський"),
            ("Видова квартира в ЖК Венеція", "Венеція", "Голосіївський"),
            ("Оренда в ЖК Кришталеві Джерела", "Кришталеві Джерела", "Голосіївський"),
            ("Оренда 1-кімн квартири в ЖК «Еврика»", "Еврика", "Голосіївський"),
            ("Преміальні апартаменти в ЖК Taryan Towers", "Taryan Towers", "Печерський"),
            ("Оренда в ЖК Tetris Hall", "Tetris Hall", "Печерський"),
            ("Квартира в ЖК RiverStone з панорамою Дніпра", "RiverStone", "Дарницький"),
            ("ЖК Севен, стильна 2-кімнатна квартира", "Seven", "Дарницький"),
        ]
        for text, exp_name, exp_district in cases:
            res = extract_residential_complex(text)
            self.assertIsNotNone(res, f"Failed to extract JK from: '{text}'")
            name, district = res
            self.assertEqual(name, exp_name, f"Expected {exp_name}, got {name} in '{text}'")
            self.assertEqual(district, exp_district, f"Expected {exp_district}, got {district} in '{text}'")

    def test_extract_residential_complex_aliases_and_translit(self):
        """Tests Russian declensions, English transliterations, and colloquial forms."""
        cases = [
            ("Квартира в Новой Англии от хозяина", "Нова Англія"),
            ("Оренда в Comfort Town, мебльована", "Комфорт Таун"),
            ("Здам квартиру в parkland з ремонтом", "Park Land"),
            ("Квартира в svitlo park, генератор на ліфт", "Svitlo Park"),
            ("Оренда в ЖК Респубілка", "Respublika"),
            ("ЖК «38 Жемчужина» / Kadorr Group", "38 Жемчужина"),
        ]
        for text, exp_name in cases:
            res = extract_residential_complex(text)
            self.assertIsNotNone(res, f"Failed alias match for: '{text}'")
            name, _ = res
            self.assertEqual(name, exp_name)

    def test_extract_residential_complex_anti_hallucination(self):
        """Verifies that non-distinct words or metro stations do not cause hallucinations."""
        negative_cases = [
            "Квартира поруч з чудовою зоною відпочинку біля озера",
            "Станція метро Славутич 5 хвилин пішки",
            "Поруч метро Вокзальна та ботанічний сад",
            "Оренда квартири на вул. Володимирська, 15",
            "Спортивна територія для прогулянок з дітьми",
            "У кімнаті гарний диван та велике ліжко",
        ]
        for text in negative_cases:
            res = extract_residential_complex(text)
            self.assertIsNone(res, f"Hallucination detected! Unexpected JK match in: '{text}' -> {res}")

    def test_extract_residential_complex_generic_quotes(self):
        """Tests extraction of unknown complex names wrapped in quotes following ЖК keyword."""
        res1 = extract_residential_complex('Оренда в ЖК "Смарагдове Місто", 3 поверх')
        self.assertIsNotNone(res1)
        self.assertEqual(res1[0], "Смарагдове Місто")

        res2 = extract_residential_complex("Оренда в житловому комплексі «Зелений Острів»")
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "Зелений Острів")

    def test_residential_normalizer_jk_fallback_rule(self):
        """
        Tests the core client requirement:
        If house number is missing, street is automatically populated with f"ЖК {jk_name}".
        """
        offer1 = {
            "title": "Оренда 1-кімнатної квартири в ЖК «Нова Англія»",
            "description": "Квартира з євроремонтом, меблями та технікою.",
        }
        res1 = normalize_residential_offer(offer1)
        self.assertEqual(res1["street"], "ЖК Нова Англія")
        self.assertEqual(res1["house"], "")
        self.assertEqual(res1["district"], "Голосіївський")

        offer2 = {
            "title": "Здам в оренду смарт-квартиру в ЖК \"Парк Ленд\"",
            "description": "Автономне опалення, закрита територія.",
        }
        res2 = normalize_residential_offer(offer2)
        self.assertEqual(res2["street"], "ЖК Park Land")
        self.assertEqual(res2["house"], "")
        self.assertEqual(res2["district"], "Голосіївський")

        offer3 = {
            "title": "Оренда 2к у ЖК Great біля Дніпра",
            "description": "Перша здача, видова, панорамні вікна.",
        }
        res3 = normalize_residential_offer(offer3)
        self.assertEqual(res3["street"], "ЖК Great")
        self.assertEqual(res3["house"], "")
        self.assertEqual(res3["district"], "Дарницький")

        offer4 = {
            "title": "Оренда квартири в ЖК Комфорт Таун",
            "description": "Гарний стан, охорона 24/7.",
        }
        res4 = normalize_residential_offer(offer4)
        self.assertEqual(res4["street"], "ЖК Комфорт Таун")
        self.assertEqual(res4["house"], "")
        self.assertEqual(res4["district"], "Дніпровський")

    def test_preserve_real_street_when_house_present(self):
        """Verifies that an explicit street and house number are NOT overwritten by the ЖК name."""
        offer = {
            "title": "Квартира в ЖК Нова Англія, вул. Михайла Максимовича, 28-Е",
            "description": "Будинок Ліверпуль, є генератор на воду та ліфт.",
        }
        res = normalize_residential_offer(offer)
        self.assertEqual(res["street"], "вулиця Михайла Максимовича")
        self.assertEqual(res["house"], "28-Е")
        self.assertEqual(res["district"], "Голосіївський")

        offer2 = {
            "title": "Видова квартира в ЖК Кришталеві Джерела, вул. Заболотного 148В",
            "description": "Поруч парк Феофанія.",
        }
        res2 = normalize_residential_offer(offer2)
        self.assertEqual(res2["street"], "вулиця Заболотного Академіка")
        self.assertEqual(res2["house"], "148В")

    def test_preserve_real_street_when_street_present_without_house(self):
        """Verifies that when a valid street is already found, it is preserved even without house."""
        offer = {
            "title": "Оренда квартири, вул. Антоновича, ЖК New York",
            "description": "Центр Києва, метро Олімпійська.",
        }
        res = normalize_residential_offer(offer)
        self.assertEqual(res["street"], "вулиця Антоновича")
        self.assertEqual(res["house"], "")

    def test_commercial_normalizer_jk_fallback(self):
        """Verifies commercial normalizer fills street with ЖК when address is absent."""
        offer = {
            "title": "Оренда офісу 40 м2, ВЛАСНИК , ЖК «Нова Англія», Новая Англия",
            "description": "Офіс з ремонтом, цоколь з вікнами, закрита територія.",
        }
        res = normalize_commercial_offer(offer)
        self.assertEqual(res["street"], "ЖК Нова Англія")
        self.assertEqual(res["house"], "")
        self.assertEqual(res["district"], "Голосіївський")

    def test_address_parser_fallback_jk_toggle(self):
        """
        Verifies KyivAddressParser backward-compatibility:
        - parse(..., fallback_jk=False) returns ("", "", "") for safe street-only matching.
        - parse(..., fallback_jk=True) returns ("ЖК <Name>", "", "<District>") for fallback.
        """
        s_off, h_off, _ = self.parser.parse("ЖК Нова Англія", fallback_jk=False)
        self.assertEqual(s_off, "")
        self.assertEqual(h_off, "")

        s_on, h_on, d_on = self.parser.parse("ЖК Нова Англія", fallback_jk=True)
        self.assertEqual(s_on, "ЖК Нова Англія")
        self.assertEqual(h_on, "")
        self.assertEqual(d_on, "Голосіївський")


if __name__ == "__main__":
    unittest.main()
