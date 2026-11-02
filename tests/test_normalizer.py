"""
Unit tests for normalizer.py module.
Tests address parser, residential normalization (23 fields),
commercial normalization (35 fields), and strict conditional clearing rules.
"""

import unittest
from database import RESIDENTIAL_COLUMNS, COMMERCIAL_COLUMNS
from normalizer import (
    KyivAddressParser,
    get_address_parser,
    normalize_residential_offer,
    normalize_commercial_offer,
    apply_conditional_clearing,
)


class TestAddressParser(unittest.TestCase):
    """Tests for KyivAddressParser."""

    @classmethod
    def setUpClass(cls):
        cls.parser = get_address_parser()

    def test_basic_street_and_house_in_title(self):
        title = "Здам 2к квартиру, вул. Драгоманова, 2"
        street, house, district = self.parser.parse(title)
        self.assertEqual(street, "вулиця Драгоманова")
        self.assertEqual(house, "2")
        self.assertEqual(district, "Дарницький")

    def test_street_with_letter_suffix(self):
        title = "Оренда квартири, вулиця Здолбунівська, 7Д, Позняки"
        street, house, district = self.parser.parse(title)
        self.assertEqual(street, "вулиця Здолбунівська")
        self.assertEqual(house, "7Д")
        self.assertEqual(district, "Дарницький")

    def test_highway_and_house_without_comma(self):
        title = "Оренда приміщення 34 м² Харківське шосе 19 в укритті підвальне приміщення"
        street, house, district = self.parser.parse(title)
        self.assertEqual(street, "шосе Харківське")
        self.assertEqual(house, "19")

    def test_prospect_and_hyphenated_house(self):
        title = "Оренда 1к, Берестейський проспект, 67-А"
        street, house, district = self.parser.parse(title)
        self.assertEqual(street, "проспект Берестейський")
        self.assertEqual(house, "67-А")

    def test_fractional_house_number(self):
        title = "Офіс на Хрещатик, 15/4 біля метро"
        street, house, district = self.parser.parse(title)
        self.assertEqual(street, "вулиця Хрещатик")
        self.assertEqual(house, "15/4")
        self.assertEqual(district, "Шевченківський")

    def test_title_street_description_house_priority(self):
        title = "Здам квартиру в гуртожитку Максима Березовського 6000"
        desc = "Здам квартиру-кімнату в гуртожитку, Київ, вул. Максима Березовського 7, 4 поверх"
        street, house, district = self.parser.parse(title, desc)
        self.assertEqual(street, "вулиця Максима Березовського")
        self.assertEqual(house, "7")
        self.assertEqual(district, "Солом'янський")

    def test_price_and_area_not_treated_as_house_numbers(self):
        title = "Оренда 1к квартири 65м2 на Драгоманова, 18000 грн"
        street, house, _ = self.parser.parse(title)
        self.assertEqual(street, "вулиця Драгоманова")
        self.assertEqual(house, "")

    def test_renamed_streets_and_synonyms(self):
        # Повітряних Сил (колишній Повітрофлотський)
        title = "Квартира в ЖК OK'LAND, проспект Повітряних Сил, 56-Г"
        street, house, _ = self.parser.parse(title)
        self.assertEqual(street, "проспект Повітрофлотський")
        self.assertEqual(house, "56-Г")

        # Змієнка
        title2 = "Оренда в ЖК Варшавський, вул. Змієнка, 30"
        street2, house2, _ = self.parser.parse(title2)
        self.assertEqual(street2, "вулиця Всеволода Змієнка")
        self.assertEqual(house2, "30")

    def test_district_hint_override(self):
        title = "Оренда на вул. Саксаганського 37К"
        street, house, district = self.parser.parse(title, district_hint="Голосіївський")
        self.assertEqual(street, "вулиця Саксаганського")
        self.assertEqual(house, "37К")
        self.assertEqual(district, "Голосіївський")


class TestResidentialNormalizer(unittest.TestCase):
    """Tests for ResidentialNormalizer (23 fields)."""

    def test_residential_matching_template_row(self):
        mock_offer = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/PRIMER-ID.html?tracking=123",
            "business": False,
            "title": "Здам 1к квартиру вулиця Здолбунівська, 7Д",
            "description": (
                "Дарницький район. Новобудова від 2021 року. Євроремонт. "
                "Повністю з меблями. Встановлено кондиціонер, пральна машина, бойлер, холодильник. "
                "У будинку є генератор на опалення та ліфт. У квартирі інвертора немає. "
                "Можна з тваринами, можна з дітьми. Без комісії від власника. "
                "Комунальні оплачуються окремо за лічильниками."
            ),
            "location": {"district": {"name": "Дарницький"}},
            "params": [
                {"key": "price", "value": {"value": 18000, "currency": "UAH"}},
                {"key": "number_of_rooms_string", "value": {"key": "odnokomnatnye", "label": "1 кімната"}},
                {"key": "total_area", "value": {"key": "42", "label": "42 м²"}},
                {"key": "kitchen_area", "value": {"key": "10", "label": "10 м²"}},
                {"key": "floor", "value": {"key": "9", "label": "9"}},
                {"key": "total_floors", "value": {"key": "16", "label": "16"}},
                {"key": "property_type_appartments_sale", "value": {"key": "12", "label": "Житловий фонд від 2021 р."}},
                {"key": "repair", "value": {"key": "2", "label": "Євроремонт"}},
                {"key": "furnish", "value": {"key": "yes", "label": "З меблями"}},
                {"key": "appliances_2", "value": {"key": ["air_conditioning", "washer", "boiler", "fridge"]}},
                {"key": "pets", "value": {"key": ["yes"]}},
                {"key": "commission", "value": {"key": "1", "label": "Без комісії"}},
            ],
        }

        result = normalize_residential_offer(mock_offer, seen_date="2026-11-03")

        # Verify all 23 columns exist and match template order
        self.assertEqual(list(result.keys()), RESIDENTIAL_COLUMNS)

        self.assertEqual(result["url"], "https://www.olx.ua/d/uk/obyavlenie/PRIMER-ID.html")
        self.assertEqual(result["seen_date"], "2026-11-03")
        self.assertEqual(result["price"], "18000")
        self.assertEqual(result["currency"], "UAH")
        self.assertEqual(result["seller"], "owner")
        self.assertEqual(result["street"], "вулиця Здолбунівська")
        self.assertEqual(result["house"], "7Д")
        self.assertEqual(result["district"], "Дарницький")
        self.assertEqual(result["rooms"], "1")
        self.assertEqual(result["area_total"], "42")
        self.assertEqual(result["area_kitchen"], "10")
        self.assertEqual(result["floor"], "9")
        self.assertEqual(result["floors_total"], "16")
        self.assertEqual(result["building_type"], "new_build_2010s_plus")
        self.assertEqual(result["renovation"], "euro")
        self.assertEqual(result["furniture"], "full")
        self.assertEqual(result["appliances"], "ac,washer,boiler,fridge")
        self.assertEqual(result["generator_in_building"], "yes")
        self.assertEqual(result["inverter_in_flat"], "no")
        self.assertEqual(result["pets_allowed"], "yes")
        self.assertEqual(result["kids_allowed"], "yes")
        self.assertEqual(result["commission_pct"], "0")
        self.assertEqual(result["utilities_included"], "no")

    def test_agency_and_commission_extraction(self):
        mock_offer = {
            "business": True,
            "title": "Оренда 2к біля метро, комісія 50%",
            "description": "Рієлторські послуги 50%. Інвертор в квартирі EcoFlow, світло є.",
            "params": [],
        }
        res = normalize_residential_offer(mock_offer)
        self.assertEqual(res["seller"], "agency")
        self.assertEqual(res["commission_pct"], "50")
        self.assertEqual(res["inverter_in_flat"], "yes")

    def test_sparse_residential_offer(self):
        sparse = {"url": "https://www.olx.ua/test"}
        res = normalize_residential_offer(sparse)
        self.assertEqual(len(res), 23)
        self.assertEqual(res["url"], "https://www.olx.ua/test")
        self.assertEqual(res["street"], "")
        self.assertEqual(res["house"], "")
        self.assertEqual(res["rooms"], "")


class TestCommercialNormalizer(unittest.TestCase):
    """Tests for CommercialNormalizer (35 fields) and conditional clearing."""

    def test_commercial_matching_template_row(self):
        mock_offer = {
            "url": "https://www.olx.ua/d/uk/obyavlenie/PRIMER-ID.html?utm_source=olx",
            "business": True,
            "category": {"id": 3690},  # Оренда офісів
            "title": "Оренда офісу 85 м2, вул. Велика Васильківська 72",
            "description": (
                "Голосіївський район. Офіс у бізнес-центрі, клас B. "
                "Площа 85 кв.м, 5/9 поверх. Стан ready (з ремонтом), частково меблі. "
                "Висота стелі 3.0 м (не повинна бути збережена для офісу!). "
                "Потужність 15 кВт. Паркінг 2 місця. У будівлі є генератор та укриття. "
                "Ціна 1200 $ на місяць. ПДВ немає. Експлуатаційні окремо не включені. "
                "Оренда від 12 місяців. Комісія рієлтора 50%."
            ),
            "location": {"district": {"name": "Голосіївський"}},
            "params": [
                {"key": "price", "value": {"value": 1200, "currency": "USD"}},
                {"key": "total_area", "value": {"key": "85", "label": "85 м²"}},
                {"key": "floor", "value": {"key": "5", "label": "5"}},
                {"key": "total_floors", "value": {"key": "9", "label": "9"}},
                {"key": "office_class", "value": {"key": "category_b", "label": "Категорія B"}},
                {"key": "furnishing", "value": {"key": "partial"}},
                {"key": "is_repaired", "value": {"key": "yes"}},
            ],
        }

        result = normalize_commercial_offer(mock_offer, seen_date="2026-11-03")

        # Verify all 35 columns exist and match template order
        self.assertEqual(list(result.keys()), COMMERCIAL_COLUMNS)

        self.assertEqual(result["url"], "https://www.olx.ua/d/uk/obyavlenie/PRIMER-ID.html")
        self.assertEqual(result["seen_date"], "2026-11-03")
        self.assertEqual(result["property_type"], "office")
        self.assertEqual(result["price"], "1200")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["price_basis"], "month_total")
        self.assertEqual(result["vat_included"], "no")
        self.assertEqual(result["opex_included"], "no")
        self.assertEqual(result["opex_uah_m2"], "")
        self.assertEqual(result["seller"], "agency")
        self.assertEqual(result["street"], "вулиця Велика Васильківська")
        self.assertEqual(result["house"], "72")
        self.assertEqual(result["district"], "Голосіївський")
        self.assertEqual(result["building_class"], "B")
        self.assertEqual(result["area_total"], "85")
        self.assertEqual(result["floor"], "5")
        self.assertEqual(result["floors_total"], "9")
        self.assertEqual(result["condition"], "ready")
        self.assertEqual(result["furniture"], "partial")
        self.assertEqual(result["parking_spaces"], "2")
        self.assertEqual(result["generator"], "yes")
        self.assertEqual(result["shelter_in_building"], "yes")
        self.assertEqual(result["min_lease_months"], "12")
        self.assertEqual(result["commission_pct"], "50")

        # STRICT CONDITIONAL CLEARING VERIFICATION FOR OFFICE:
        # ceiling_height_m, power_kw, ventilation_hood, loading_dock MUST BE EMPTY FOR OFFICE
        self.assertEqual(result["ceiling_height_m"], "", "ceiling_height_m must be cleared for office")
        self.assertEqual(result["power_kw"], "", "power_kw must be cleared for office")
        self.assertEqual(result["ventilation_hood"], "", "ventilation_hood must be cleared for office")
        self.assertEqual(result["water_in_rooms"], "", "water_in_rooms must be cleared for office")
        self.assertEqual(result["loading_dock"], "", "loading_dock must be cleared for office")
        self.assertEqual(result["floor_load_t_m2"], "", "floor_load_t_m2 must be cleared for office")
        self.assertEqual(result["heated"], "", "heated must be cleared for office")

    def test_price_basis_calculation(self):
        # Case A: Explicitly per m2 in text
        res_a = normalize_commercial_offer({
            "title": "Оренда приміщення 100 м2, 25 $/м²",
            "params": [{"key": "price", "value": {"value": 25, "currency": "USD"}}],
        })
        self.assertEqual(res_a["price_basis"], "month_per_m2")

        # Case B: Disproportionately low price for large area (e.g. 50 USD for 100 m2)
        res_b = normalize_commercial_offer({
            "title": "Офіс 100 кв.м",
            "params": [
                {"key": "price", "value": {"value": 50, "currency": "USD"}},
                {"key": "total_area", "value": {"key": "100"}},
            ],
        })
        self.assertEqual(res_b["price_basis"], "month_per_m2")

        # Case C: Disproportionately low price in UAH (1500 UAH for 80 m2)
        res_c = normalize_commercial_offer({
            "title": "Приміщення 80 кв.м",
            "params": [
                {"key": "price", "value": {"value": 1500, "currency": "UAH"}},
                {"key": "total_area", "value": {"key": "80"}},
            ],
        })
        self.assertEqual(res_c["price_basis"], "month_per_m2")

        # Case D: Normal total price
        res_d = normalize_commercial_offer({
            "title": "Офіс 50 кв.м",
            "params": [
                {"key": "price", "value": {"value": 25000, "currency": "UAH"}},
                {"key": "total_area", "value": {"key": "50"}},
            ],
        })
        self.assertEqual(res_d["price_basis"], "month_total")

    def test_conditional_clearing_warehouse_vs_retail(self):
        # Warehouse test
        wh_offer = {
            "category": {"id": 3693},  # Склад
            "title": "Склад 500 м2, рампа, висота стелі 6.5 м, 50 кВт, навантаження 5 т/м2, теплий",
            "description": "БЦ Gulliver клас A, окремий вхід з фасаду, вітринні вікна, вода в кабінетах",
            "params": [],
        }
        wh_res = normalize_commercial_offer(wh_offer)
        self.assertEqual(wh_res["property_type"], "warehouse")
        self.assertEqual(wh_res["loading_dock"], "yes")
        self.assertEqual(wh_res["ceiling_height_m"], "6.5")
        self.assertEqual(wh_res["power_kw"], "50")
        self.assertEqual(wh_res["floor_load_t_m2"], "5")
        self.assertEqual(wh_res["heated"], "yes")
        self.assertEqual(wh_res["building_class"], "A")
        # Disallowed fields for warehouse must be cleared:
        self.assertEqual(wh_res["business_center_name"], "")
        self.assertEqual(wh_res["first_line"], "")
        self.assertEqual(wh_res["separate_entrance"], "")
        self.assertEqual(wh_res["shop_windows"], "")
        self.assertEqual(wh_res["water_in_rooms"], "")
        self.assertEqual(wh_res["ventilation_hood"], "")

        # Retail test
        ret_offer = {
            "category": {"id": 3692},  # Торгове приміщення
            "title": "Магазин 70 м2 на першій лінії, окремий вхід, великі вітрини",
            "description": "Рампа, висота стелі 4 м, навантаження 3 т/м2, БЦ Парус",
            "params": [],
        }
        ret_res = normalize_commercial_offer(ret_offer)
        self.assertEqual(ret_res["property_type"], "retail")
        self.assertEqual(ret_res["first_line"], "yes")
        self.assertEqual(ret_res["separate_entrance"], "yes")
        self.assertEqual(ret_res["shop_windows"], "yes")
        # Disallowed fields for retail must be cleared:
        self.assertEqual(ret_res["business_center_name"], "")
        self.assertEqual(ret_res["building_class"], "")
        self.assertEqual(ret_res["ceiling_height_m"], "")
        self.assertEqual(ret_res["loading_dock"], "")
        self.assertEqual(ret_res["floor_load_t_m2"], "")
        self.assertEqual(ret_res["heated"], "")

    def test_basement_and_ground_floor(self):
        offer_basement = {
            "title": "Оренда офісу в підвалі",
            "params": [{"key": "comm_re_location", "value": "basement"}],
        }
        res_b = normalize_commercial_offer(offer_basement)
        self.assertEqual(res_b["floor"], "-1")

        offer_ground = {
            "title": "Оренда приміщення цоколь 50 м2",
            "params": [],
        }
        res_g = normalize_commercial_offer(offer_ground)
        self.assertEqual(res_g["floor"], "0")

    def test_audit_fixes_points_1_to_8(self):
        parser = get_address_parser()

        # Point 1: Address Parser Anti-Hallucination
        s1, h1, _ = parser.parse("ЖК Нова Англія")
        self.assertEqual(s1, "")

        s2, _, _ = parser.parse("Квартира світла та простора")
        self.assertEqual(s2, "")

        s3, _, _ = parser.parse("Затишна невелика квартира в оренду, Богатирська вул.")
        self.assertEqual(s3, "вулиця Богатирська")

        s4, h4, _ = parser.parse("Видова квартира в ЖК Кришталеві Джерела, вул. Заболотного 148В")
        self.assertEqual(s4, "вулиця Заболотного Академіка")
        self.assertEqual(h4, "148В")

        s5, _, _ = parser.parse("Поруч зона відпочинку")
        self.assertEqual(s5, "")

        s6, _, _ = parser.parse("Є газова плита")
        self.assertEqual(s6, "")

        # Point 2: Building type OLX mapping
        from normalizer import ResidentialNormalizer
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "1"}]}, ""), "tsarist")
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "2"}]}, ""), "stalinka")
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "3"}]}, ""), "khrushchyovka")
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "4"}]}, ""), "cheshka")
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "5"}]}, ""), "dormitory")
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "6"}]}, ""), "special_project")
        self.assertEqual(ResidentialNormalizer.extract_building_type({"params": [{"key": "property_type_appartments_sale", "value": "8"}]}, ""), "special_project")

        # Point 3: Large total price does not get tagged month_per_m2
        res_p3 = normalize_commercial_offer({
            "title": "Оренда великого офісу",
            "description": "Офіс 640 м2, ціна 400 000 грн (625 грн/м2 з ПДВ)",
            "params": [
                {"key": "price", "value": {"value": 400000, "currency": "UAH"}},
                {"key": "total_area", "value": {"key": "640"}},
            ],
        })
        self.assertEqual(res_p3["price_basis"], "month_total")

        res_p3_usd = normalize_commercial_offer({
            "title": "Оренда офісу 500 м2",
            "description": "Ціна 9500$ (19 $/м2)",
            "params": [
                {"key": "price", "value": {"value": 9500, "currency": "USD"}},
                {"key": "total_area", "value": {"key": "500"}},
            ],
        })
        self.assertEqual(res_p3_usd["price_basis"], "month_total")

        # Point 4: Seller owner identification despite business: True
        res_p4 = normalize_residential_offer({
            "url": "https://www.olx.ua/d/uk/obyavlenie/vd-vlasnika-bez-komisiyi.html",
            "business": True,
            "title": "Оренда 1к квартири від власника!",
            "params": [],
        })
        self.assertEqual(res_p4["seller"], "owner")

        # Developer identification
        res_p4_dev = normalize_residential_offer({
            "business": True,
            "title": "Оренда від забудовника без посередників",
            "params": [],
        })
        self.assertEqual(res_p4_dev["seller"], "developer")

        # Point 5: Business center name anti-hallucination
        from normalizer import CommercialNormalizer
        self.assertEqual(CommercialNormalizer.extract_business_center_name("У БЦ є два ліфти."), "")
        self.assertEqual(CommercialNormalizer.extract_business_center_name("Аренда офиса БЦ 1-2 кабинета 14-30 кв.м."), "")
        self.assertEqual(CommercialNormalizer.extract_business_center_name("у новому бізнес-центрі\nОренда офісу 1503 м2"), "")
        self.assertEqual(CommercialNormalizer.extract_business_center_name('БЦ "Gulliver"'), "Gulliver")
        self.assertEqual(CommercialNormalizer.extract_business_center_name("БЦ Toronto-Kyiv"), "Toronto-Kyiv")

        # Point 6: Parking spaces false positive on price
        res_p6 = normalize_commercial_offer({
            "title": "Центр, м.Олімпійська, оренда приміщення з ремонтом, парковка, 25тис.грн",
            "params": [],
        })
        self.assertEqual(res_p6["parking_spaces"], "")

        # Point 7: Pets array with yes_ prefix
        res_p7 = normalize_residential_offer({
            "title": "1к квартира",
            "params": [{"key": "pets", "value": ["yes_cat", "yes_small_dog"]}],
        })
        self.assertEqual(res_p7["pets_allowed"], "yes")

        res_p7_url = normalize_residential_offer({
            "url": "https://www.olx.ua/d/uk/obyavlenie/mozhna-z-tvarinoyu.html",
            "title": "1к квартира",
            "params": [],
        })
        self.assertEqual(res_p7_url["pets_allowed"], "yes")

        # Point 8: Rooms 5-room key pyatikomnatnye
        res_p8 = normalize_residential_offer({
            "title": "Квартира",
            "params": [{"key": "number_of_rooms_string", "value": "pyatikomnatnye"}],
        })
        self.assertEqual(res_p8["rooms"], "5")

    def test_audit_bugs_1_to_4(self):
        """Dedicated tests for audit bugs 1, 2, 3, 4."""
        parser = get_address_parser()

        # Bug 1: 1-gram hallucinations, recreation zones, complex, and metro
        s, h, _ = parser.parse("Сучасний дизайнерський автономний офіс Голосієво 318 м2... Вид на мальовничий Голосіївський парк")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("Двокімнатна в новобудові автономний ремонт моторний поверх")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("Оренда квартири в ЖК Республіка, багато зон для відпочинку")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("Оренда квартири в ЖК Республіка, із зонами відпочинку поруч")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("Оренда 1-к автономної квартири Володимирський")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("комплексі бізнес-класу «Володимирський»")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("ЖК «Нова Англія», Метро «Васильківська» — 10 хвилин пішки")
        self.assertEqual(s, "")

        s, h, _ = parser.parse("поруч зі станціями метро «Площа Українських Героїв»")
        self.assertEqual(s, "")

        # Bug 2: Type collisions (вулиця vs площа/провулок)
        s, h, d = parser.parse("вул. Голосіївська 13Б")
        self.assertEqual(s, "вулиця Голосіївська")
        self.assertEqual(h, "13Б")
        self.assertEqual(d, "Голосіївський")

        s, h, d = parser.parse("вул. Деміївська, 16")
        self.assertEqual(s, "вулиця Деміївська")
        self.assertEqual(h, "16")
        self.assertEqual(d, "Голосіївський")

        # Bug 3: Accents, repeated street keyword, title streets, canonical naming
        s, h, d = parser.parse("Оренда офісу, Поділ, по вул.Хорива б.25/12")
        self.assertEqual(s, "вулиця Хорива")
        self.assertEqual(h, "25/12")
        self.assertEqual(d, "Подільський")

        s, h, d = parser.parse("Оренда. 2-кімнт. вул.Володимиро-Либідська вул.,16 Метро Палац Україна")
        self.assertEqual(s, "вулиця Володимиро-Либідська")
        self.assertEqual(h, "16")

        s, h, d = parser.parse("вул. Булаховського 5")
        self.assertEqual(s, "вулиця Булаховського Академіка")
        self.assertEqual(h, "5")
        self.assertEqual(d, "Святошинський")

        s, h, d = parser.parse("вул. Вернадського, 10")
        self.assertEqual(s, "бульвар Вернадського Академіка")
        self.assertEqual(h, "10")
        self.assertEqual(d, "Святошинський")

        s, h, d = parser.parse("Оренда квартири, вул. Глушкова 9")
        self.assertEqual(s, "проспект Глушкова Академіка")
        self.assertEqual(h, "9")
        self.assertEqual(d, "Голосіївський")

        # Bug 4: Business center hallucinations
        from normalizer import CommercialNormalizer
        self.assertEqual(CommercialNormalizer.extract_business_center_name("Без% аренда офиса отдельный блок 2-3 каб, 66-90 кв.м Бизнес-центр Киев метро Голосеевский"), "")
        self.assertEqual(CommercialNormalizer.extract_business_center_name("бізнес-центр Голосіївський район"), "")
        self.assertEqual(CommercialNormalizer.extract_business_center_name("сучасний бізнес-центр\nГолосіївський район"), "")


if __name__ == "__main__":
    unittest.main()

