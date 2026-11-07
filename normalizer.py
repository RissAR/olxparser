"""
Normalization and Data Extraction Module for OLX Kyiv Real Estate.

Features:
- Address parser with Kyiv street dictionary matching, fast n-gram lookup,
  alias/historical renames handling, and house number extraction (title -> description priority).
- Residential real estate normalizer (23 columns).
- Commercial real estate normalizer (35 columns) with strict conditional clearing.
"""

import html
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# District names in Kyiv
KYIV_DISTRICTS = [
    "Голосіївський",
    "Дарницький",
    "Деснянський",
    "Дніпровський",
    "Оболонський",
    "Печерський",
    "Подільський",
    "Святошинський",
    "Солом'янський",
    "Шевченківський",
]

# Strict commercial property types allowed
COMMERCIAL_PROPERTY_TYPES = {
    "office",
    "retail",
    "food",
    "warehouse",
    "production",
    "free_purpose",
    "medical_beauty",
    "education",
    "hotel",
}

# Strict conditional field rules for commercial properties:
# Maps field name -> set of allowed property types.
# If property_type is not in this set, field MUST be forced to "".
COMMERCIAL_CONDITIONAL_FIELDS: Dict[str, Set[str]] = {
    "business_center_name": {"office"},
    "building_class": {"office", "warehouse"},
    "first_line": {"retail", "food", "medical_beauty"},
    "separate_entrance": {"retail", "food", "medical_beauty"},
    "shop_windows": {"retail", "food"},
    "ceiling_height_m": {"warehouse", "production", "food"},
    "power_kw": {"food", "production", "warehouse"},
    "ventilation_hood": {"food"},
    "water_in_rooms": {"medical_beauty"},
    "loading_dock": {"warehouse"},
    "floor_load_t_m2": {"warehouse", "production"},
    "heated": {"warehouse", "production"},
}

# OLX Subcategory ID mapping to commercial property types
OLX_COMMERCIAL_SUBCATEGORY_MAP: Dict[int, str] = {
    3690: "office",           # Оренда офісів
    3691: "production",       # Оренда виробничих приміщень, ферм
    3692: "retail",           # Оренда торгових приміщень
    3693: "warehouse",        # Оренда складів, ангарів
    3694: "food",             # Оренда кафе, ресторанів, кав'ярень
    3695: "retail",           # Оренда МАФів
    3696: "production",       # Оренда СТО, автомийок, АЗС
    3697: "medical_beauty",   # Оренда салонів, кабінетів краси
    3698: "education",        # Оренда залів, спортивних приміщень
    3699: "medical_beauty",   # Оренда медичних приміщень
    3700: "free_purpose",     # Оренда приміщення вільного призначення
    3701: "free_purpose",     # Оренда інших приміщень
}

# Historical and colloquial street name synonyms / renames in Kyiv
STREET_SYNONYMS: Dict[str, str] = {
    "повітряних сил": "проспект Повітрофлотський",
    "повітрофлотський": "проспект Повітрофлотський",
    "повітрофлотський проспект": "проспект Повітрофлотський",
    "проспект повітрофлотський": "проспект Повітрофлотський",
    "проспект повітряних сил": "проспект Повітрофлотський",
    "змієнка": "вулиця Всеволода Змієнка",
    "всеволода змієнка": "вулиця Всеволода Змієнка",
    "драгомирова": "вулиця Драгомирова Михайла",
    "михайла драгомирова": "вулиця Драгомирова Михайла",
    "перемоги": "проспект Берестейський",
    "проспект перемоги": "проспект Берестейський",
    "берестейський": "проспект Берестейський",
    "берестейський проспект": "проспект Берестейський",
    "червоноармійська": "вулиця Велика Васильківська",
    "красноармейская": "вулиця Велика Васильківська",
    "большая васильковская": "вулиця Велика Васильківська",
    "велика васильківська": "вулиця Велика Васильківська",
    "московський": "проспект Степана Бандери",
    "московский": "проспект Степана Бандери",
    "степана бандери": "проспект Степана Бандери",
    "бандери": "проспект Степана Бандери",
    "героїв сталінграда": "проспект Володимира Івасюка",
    "героев сталинграда": "проспект Володимира Івасюка",
    "івасюка": "проспект Володимира Івасюка",
    "володимира івасюка": "проспект Володимира Івасюка",
    "кутузова": "вулиця Генерала Алмазова",
    "суворова": "вулиця Михайла Омеляновича-Павленка",
    "артема": "вулиця Січових Стрільців",
    "січових стрільців": "вулиця Січових Стрільців",
    "димитрова": "вулиця Ділова",
    "ділова": "вулиця Ділова",
    "горького": "вулиця Антоновича",
    "антоновича": "вулиця Антоновича",
    "ломоносова": "вулиця Юлії Здановської",
    "юлії здановської": "вулиця Юлії Здановської",
    "здановської": "вулиця Юлії Здановської",
    "тельмана": "вулиця Німецька",
    "німецька": "вулиця Німецька",
    "патриса лумумбы": "вулиця Іоанна Павла II",
    "патріса лумумби": "вулиця Іоанна Павла II",
    "іоанна павла": "вулиця Іоанна Павла II",
    "івана кудрі": "вулиця Джона Маккейна",
    "джона маккейна": "вулиця Джона Маккейна",
    "маккейна": "вулиця Джона Маккейна",
    "хрещатик": "вулиця Хрещатик",
    "крещатик": "вулиця Хрещатик",
    "здолбунівська": "вулиця Здолбунівська",
    "здолбуновская": "вулиця Здолбунівська",
    "саксаганського": "вулиця Саксаганського",
    "драгоманова": "вулиця Драгоманова",
    "харківське шосе": "шосе Харківське",
    "харьковское шоссе": "шосе Харківське",
    "заболотного": "вулиця Заболотного Академіка",
    "академіка заболотного": "вулиця Заболотного Академіка",
    "максимовича": "вулиця Михайла Максимовича",
    "михайла максимовича": "вулиця Михайла Максимовича",
    "трутенка": "вулиця Михайла Максимовича",
    "онуфрія трутенка": "вулиця Михайла Максимовича",
    "тадея рильського": "бульвар Тадея Рильського",
    "бульвар тадея рильського": "бульвар Тадея Рильського",
    "бул тадея рильського": "бульвар Тадея Рильського",
    "бул. тадея рильського": "бульвар Тадея Рильського",
    "рильського тадея": "бульвар Тадея Рильського",
    "бульвар рильського": "бульвар Тадея Рильського",
    "рильського": "вулиця Рильського Максима",
    "максима рильського": "вулиця Рильського Максима",
    "богатирська": "вулиця Богатирська",
    "коновальця": "вулиця Євгена Коновальця",
    "євгена коновальця": "вулиця Євгена Коновальця",
    "теліги": "вулиця Теліги Олени",
    "олени теліги": "вулиця Теліги Олени",
    "грінченка": "вулиця Грінченка Миколи",
    "миколи грінченка": "вулиця Грінченка Миколи",
    "григоренка": "проспект Григоренка Петра",
    "петра григоренка": "проспект Григоренка Петра",
    "глушкова": "проспект Глушкова Академіка",
    "академіка глушкова": "проспект Глушкова Академіка",
    "палладіна": "проспект Палладіна Академіка",
    "академіка палладіна": "проспект Палладіна Академіка",
    "вільямса": "вулиця Вільямса Академіка",
    "академіка вільямса": "вулиця Вільямса Академіка",
    "руставелі": "вулиця Руставелі Шота",
    "шота руставелі": "вулиця Руставелі Шота",
    "малевича": "вулиця Казимира Малевича",
    "казимира малевича": "вулиця Казимира Малевича",
    "тютюнника": "вулиця Василя Тютюнника",
    "василя тютюнника": "вулиця Василя Тютюнника",
    "гетьмана": "вулиця Гетьмана Вадима",
    "вадима гетьмана": "вулиця Гетьмана Вадима",
    "голего": "вулиця Миколи Голего",
    "миколи голего": "вулиця Миколи Голего",
    "верхогляда": "вулиця Драгомирова Михайла",
    "андрія верхогляда": "вулиця Драгомирова Михайла",
    "драгомирова": "вулиця Драгомирова Михайла",
}

# Ambiguous single-word street names that cause hallucinations without explicit street indicators
AMBIGUOUS_STREET_WORDS: Set[str] = {
    "нова", "новий", "новому", "нової",
    "світла", "світле", "світлий", "світло",
    "затишна", "затишний", "затишне",
    "видова", "видовий", "видове",
    "зелена", "зелений", "зелене",
    "тепла", "теплий", "тепле",
    "тиха", "тихий", "тихе",
    "газова", "газовий",
    "відпочинку",
    "бажана",
    "вокзальна", "деміївська", "голосіївська", "голосіївський", "олімпійська",
    "васильківська", "володимирський", "мальовничий", "мальовнича", "моторний", "моторна",
    "автономний", "автономна",
    "лугова", "садова", "лісова", "польова", "шкільна", "молодіжна", "квіткова", "сонячна",
    "мирна", "спортивна", "спортивний", "спортивне", "промислова", "будівельників", "червона",
    "зимовий", "зимова", "зимове", "зимові",
    "літній", "літня", "літнє",
    "весняний", "весняна", "весняне",
    "осінній", "осіння", "осіннє",
    "дитяча", "дитячий", "дитяче",
    "парковий", "паркова", "паркове",
    "курортний", "курортна", "курортне",
}

# Regex for house number extraction immediately following street match or preposition
HOUSE_NUMBER_REGEX = re.compile(
    r"""
    ^[,\s./_–—]*
    (?:(?:вул|вулиц[яіе]|ул|улиц[ае]|просп|проспект[уі]?|пр-т|бульв|бульвар[уі]?|б-р|пров|провул(?:ок|ка|ку)|пер|переул(?:ок|ка|ку)|узвіз|набережн[аея]|шосе)\.?\s*,?\s*)?
    (?:буд(?:инок)?\.?|дом|д\.?|б\.?|№)?\s*
    (
        \d{1,4}
        (?:[/-]\d{1,4})?
        (?:-?[а-яА-ЯіІїЇєЄґҐa-zA-Z])?
        (?:\s*(?:корп(?:ус)?\.?|секц(?:ія)?\.?|к\.?)\s*\d+)?
    )
    (?!\s*(?:грн|uah|usd|eur|\$|€|тис|тисяч|м2|м²|кв|кв\.?\s*м|пов(?:ерх)?|кімн|комн|хв|мин))
    (?=[\s,.;!?)<>\n\r\t]|$)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def strip_html(text: Optional[str]) -> str:
    """Removes HTML tags and decodes HTML entities while preserving line boundaries."""
    if not text:
        return ""
    clean = re.sub(r"<(?:br|/p|/div|/li|/h\d|/tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in clean.split("\n")]
    return "\n".join(line for line in lines if line)


# ==============================================================================
# Kyiv Residential Complexes (ЖК) Dictionary & Extraction (Stage 2)
# ==============================================================================
KYIV_RESIDENTIAL_COMPLEXES: Dict[str, Dict[str, Any]] = {
    "Нова Англія": {
        "district": "Голосіївський",
        "aliases": [
            "нова англія",
            "новій англії",
            "нову англію",
            "нової англії",
            "новая англия",
            "новой англии",
            "новую англию",
            "new england",
        ],
        "distinct": True,
    },
    "Respublika": {
        "district": "Голосіївський",
        "aliases": [
            "республіка",
            "республіці",
            "республіку",
            "республика",
            "республике",
            "республику",
            "respublika",
            "респубілка",
        ],
        "distinct": True,
    },
    "Комфорт Таун": {
        "district": "Дніпровський",
        "aliases": [
            "комфорт таун",
            "комфорт тауні",
            "комфорт тауне",
            "комфорттаун",
            "comfort town",
        ],
        "distinct": True,
    },
    "Great": {
        "district": "Дарницький",
        "aliases": ["great", "грейт"],
        "distinct": True,
    },
    "Варшавський": {
        "district": "Подільський",
        "aliases": [
            "варшавський плюс",
            "варшавский плюс",
            "варшавський 2",
            "варшавский 2",
            "варшавський мікрорайон",
            "варшавский микрорайон",
            "варшавський",
            "варшавский",
            "varshavsky",
        ],
        "distinct": True,
    },
    "Файна Таун": {
        "district": "Шевченківський",
        "aliases": ["файна таун", "файнатаун", "fayna town"],
        "distinct": True,
    },
    "Park Land": {
        "district": "Голосіївський",
        "aliases": ["парк ленд", "паркленд", "park land", "parkland"],
        "distinct": True,
    },
    "Svitlo Park": {
        "district": "Голосіївський",
        "aliases": ["світло парк", "світлопарк", "svitlo park"],
        "distinct": True,
    },
    "Венеція": {
        "district": "Голосіївський",
        "aliases": ["венеція", "венеция", "venezia"],
        "distinct": True,
    },
    "Кришталеві Джерела": {
        "district": "Голосіївський",
        "aliases": ["кришталеві джерела", "хрустальные источники", "kryshtalevi dzherela"],
        "distinct": True,
    },
    "Еврика": {
        "district": "Голосіївський",
        "aliases": ["еврика", "эврика", "evrika"],
        "distinct": True,
    },
    "Володимирський": {
        "district": "Голосіївський",
        "aliases": ["володимирський", "володимиський", "владимирский", "volodymyrsky"],
        "distinct": False,
    },
    "Новопечерські Липки": {
        "district": "Печерський",
        "aliases": ["новопечерські липки", "новопечерские липки", "novopecherski lypky"],
        "distinct": True,
    },
    "Новопечерська Вежа": {
        "district": "Печерський",
        "aliases": ["новопечерська вежа", "новопечерская башня", "новопечерская вежа"],
        "distinct": True,
    },
    "Бульвар Фонтанів": {
        "district": "Печерський",
        "aliases": ["бульвар фонтанів", "бульвар фонтанов"],
        "distinct": True,
    },
    "Французький квартал": {
        "district": "Печерський",
        "aliases": [
            "французький квартал 2",
            "французский квартал 2",
            "французький квартал",
            "французский квартал",
        ],
        "distinct": True,
    },
    "Tetris Hall": {
        "district": "Печерський",
        "aliases": ["тетрис холл", "тетрис хол", "тетріс хол", "tetris hall"],
        "distinct": True,
    },
    "Jack House": {
        "district": "Печерський",
        "aliases": ["jack house", "джек хаус"],
        "distinct": True,
    },
    "Taryan Towers": {
        "district": "Печерський",
        "aliases": ["taryan towers", "тарян тауерс", "тарьян тауэрс"],
        "distinct": True,
    },
    "RiverStone": {
        "district": "Дарницький",
        "aliases": ["riverstone", "рівер стоун", "ривер стоун", "ріверстоун", "риверстоун"],
        "distinct": True,
    },
    "Seven": {
        "district": "Дарницький",
        "aliases": ["seven", "севен"],
        "distinct": True,
    },
    "Зарічний": {
        "district": "Дарницький",
        "aliases": ["зарічний", "заречный", "zarichny"],
        "distinct": True,
    },
    "Славутич": {
        "district": "Дарницький",
        "aliases": ["славутич 2.0", "славутич 2", "славутич", "slavutych"],
        "distinct": False,
    },
    "Лебединий": {
        "district": "Дарницький",
        "aliases": ["лебединий", "лебединый", "lebedynyi"],
        "distinct": True,
    },
    "Поетичний": {
        "district": "Дарницький",
        "aliases": ["поетичний", "поэтический"],
        "distinct": True,
    },
    "Причал 8": {
        "district": "Дарницький",
        "aliases": ["причал 8", "причал вісім"],
        "distinct": True,
    },
    "Галактика": {
        "district": "Дніпровський",
        "aliases": ["галактика", "galaktyka"],
        "distinct": True,
    },
    "Автограф": {
        "district": "Дніпровський",
        "aliases": ["автограф", "avtograf"],
        "distinct": True,
    },
    "Русанівська Гавань": {
        "district": "Дніпровський",
        "aliases": ["русанівська гавань", "русановская гавань"],
        "distinct": True,
    },
    "Парк Авеню": {
        "district": "Голосіївський",
        "aliases": ["парк авеню vip", "парк авеню", "park avenue vip", "park avenue"],
        "distinct": True,
    },
    "Метрополіс": {
        "district": "Голосіївський",
        "aliases": ["метрополіс", "метрополис", "metropolis"],
        "distinct": True,
    },
    "White Lines": {
        "district": "Голосіївський",
        "aliases": ["white lines", "вайт лайнс"],
        "distinct": True,
    },
    "4 Сезони": {
        "district": "Голосіївський",
        "aliases": ["4 сезони", "4 сезона", "четыре сезона", "чотири сезони", "4 seasons"],
        "distinct": True,
    },
    "Сонячна Брама": {
        "district": "Голосіївський",
        "aliases": ["сонячна брама", "солнечная брама"],
        "distinct": True,
    },
    "Ліко-Град": {
        "district": "Голосіївський",
        "aliases": [
            "liko-grad/ perfect",
            "liko-grad perfect town",
            "perfect town",
            "ліко-град",
            "ліко град",
            "лико-град",
            "лико град",
            "liko grad",
        ],
        "distinct": True,
    },
    "The Lakes": {
        "district": "Голосіївський",
        "aliases": ["the lakes", "зе лейкс", "лейкс"],
        "distinct": True,
    },
    "Orange City": {
        "district": "Голосіївський",
        "aliases": ["orange city", "оранж сіті", "оранж сити", "оранж"],
        "distinct": True,
    },
    "OK'LAND": {
        "district": "Солом'янський",
        "aliases": ["ok'land", "okland", "окланд", "окленд"],
        "distinct": True,
    },
    "Династія": {
        "district": "Солом'янський",
        "aliases": ["династія", "династия", "dynasty"],
        "distinct": True,
    },
    "Time": {
        "district": "Солом'янський",
        "aliases": ["time", "тайм"],
        "distinct": False,
    },
    "Квартет": {
        "district": "Солом'янський",
        "aliases": ["квартет", "quartet"],
        "distinct": True,
    },
    "Малахіт": {
        "district": "Солом'янський",
        "aliases": ["малахіт", "малахит", "malahit"],
        "distinct": True,
    },
    "Смарагдовий": {
        "district": "Солом'янський",
        "aliases": ["смарагдовий", "изумрудный"],
        "distinct": True,
    },
    "Пори Року": {
        "district": "Солом'янський",
        "aliases": ["пори року", "времена года"],
        "distinct": False,
    },
    "Медовий": {
        "district": "Солом'янський",
        "aliases": ["медовий 2", "медовый 2", "медовий", "медовый"],
        "distinct": False,
    },
    "Geneve": {
        "district": "Солом'янський",
        "aliases": ["geneve", "женева"],
        "distinct": True,
    },
    "Sky Avenue": {
        "district": "Солом'янський",
        "aliases": ["sky avenue", "скай авеню"],
        "distinct": True,
    },
    "Genesis": {
        "district": "Солом'янський",
        "aliases": ["genesis", "генезис"],
        "distinct": True,
    },
    "Campus": {
        "district": "Солом'янський",
        "aliases": ["campus", "кампус"],
        "distinct": True,
    },
    "Нивки-Парк": {
        "district": "Святошинський",
        "aliases": ["нивки-парк", "нивки парк", "nyvky park"],
        "distinct": True,
    },
    "Святобор": {
        "district": "Святошинський",
        "aliases": ["святобор", "svyatobor"],
        "distinct": True,
    },
    "San Francisco": {
        "district": "Святошинський",
        "aliases": ["san francisco creative house", "san francisco", "сан франциско"],
        "distinct": True,
    },
    "Відпочинок": {
        "district": "Святошинський",
        "aliases": ["відпочинок", "отдых"],
        "distinct": False,
    },
    "Грюнвальд": {
        "district": "Святошинський",
        "aliases": ["грюнвальд", "grunwald"],
        "distinct": True,
    },
    "Верховина": {
        "district": "Святошинський",
        "aliases": ["верховина"],
        "distinct": False,
    },
    "Академ-Квартал": {
        "district": "Святошинський",
        "aliases": ["академ-квартал", "академ квартал"],
        "distinct": True,
    },
    "Lucky Land": {
        "district": "Святошинський",
        "aliases": ["lucky land", "лакі ленд", "лаки ленд"],
        "distinct": True,
    },
    "Діброва Парк": {
        "district": "Подільський",
        "aliases": ["діброва парк", "диброва парк", "dibrova park"],
        "distinct": True,
    },
    "Місто Квітів": {
        "district": "Подільський",
        "aliases": ["місто квітів", "город цветов"],
        "distinct": True,
    },
    "Паркове Місто": {
        "district": "Подільський",
        "aliases": ["паркове місто", "парковый город"],
        "distinct": True,
    },
    "Ліпінка": {
        "district": "Подільський",
        "aliases": ["ліпінка", "липинка", "lipinka"],
        "distinct": True,
    },
    "Крістер Град": {
        "district": "Подільський",
        "aliases": ["крістер град", "кристер град"],
        "distinct": True,
    },
    "Замковецький": {
        "district": "Подільський",
        "aliases": ["замковецький", "замковецкий"],
        "distinct": False,
    },
    "Rybalsky": {
        "district": "Подільський",
        "aliases": ["rybalsky", "рибальський", "рыбальский"],
        "distinct": True,
    },
    "Podil Plaza": {
        "district": "Подільський",
        "aliases": ["podil plaza & residence", "podil plaza", "поділ плаза", "подол плаза"],
        "distinct": True,
    },
    "Поділ Град": {
        "district": "Подільський",
        "aliases": ["поділ град", "подол град"],
        "distinct": True,
    },
    "Воздвиженка": {
        "district": "Подільський",
        "aliases": ["воздвиженка"],
        "distinct": True,
    },
    "Сирецькі Сади": {
        "district": "Подільський",
        "aliases": ["сирецькі сади", "сырецкие сады"],
        "distinct": True,
    },
    "Spas Sky": {
        "district": "Подільський",
        "aliases": ["spas sky", "спас скай"],
        "distinct": True,
    },
    "Obolon Residences": {
        "district": "Оболонський",
        "aliases": ["obolon residences", "оболонь резиденс"],
        "distinct": True,
    },
    "Park Stone": {
        "district": "Оболонський",
        "aliases": ["park stone", "парк стоун", "паркстоун"],
        "distinct": True,
    },
    "Оазис": {
        "district": "Оболонський",
        "aliases": ["оазис", "oazis"],
        "distinct": False,
    },
    "Яскравий": {
        "district": "Оболонський",
        "aliases": ["яскравий", "яркий"],
        "distinct": False,
    },
    "Міністерський": {
        "district": "Оболонський",
        "aliases": ["міністерський", "министерский"],
        "distinct": False,
    },
    "Бережанський": {
        "district": "Оболонський",
        "aliases": ["бережанський", "бережанский"],
        "distinct": False,
    },
    "Navigator": {
        "district": "Оболонський",
        "aliases": [
            "navigator 2",
            "навігатор 2",
            "навигатор 2",
            "navigator",
            "навігатор",
            "навигатор",
        ],
        "distinct": True,
    },
    "Forest Park": {
        "district": "Оболонський",
        "aliases": ["forest park", "форест парк"],
        "distinct": True,
    },
    "Престиж Хол": {
        "district": "Печерський",
        "aliases": ["prestige hall", "престиж хол", "престиж холл"],
        "distinct": True,
    },
    "Аристократ": {
        "district": "Печерський",
        "aliases": ["аристократ", "aristokrat"],
        "distinct": True,
    },
    "Alter Ego": {
        "district": "Печерський",
        "aliases": ["alter ego", "альтер его", "альтер эго"],
        "distinct": True,
    },
    "Cardinal": {
        "district": "Печерський",
        "aliases": ["cardinal", "кардинал"],
        "distinct": True,
    },
    "Richmond": {
        "district": "Печерський",
        "aliases": ["richmond", "річмонд", "ричмонд"],
        "distinct": True,
    },
    "Central Park": {
        "district": "Печерський",
        "aliases": ["central park", "централ парк"],
        "distinct": True,
    },
    "PecherSKY": {
        "district": "Печерський",
        "aliases": ["pechersky", "печерскай"],
        "distinct": True,
    },
    "Busov Hill": {
        "district": "Печерський",
        "aliases": ["busov hill", "бусов хілл", "бусов хилл"],
        "distinct": True,
    },
    "Signature": {
        "district": "Печерський",
        "aliases": ["signature", "сигнатура"],
        "distinct": True,
    },
    "Linden": {
        "district": "Печерський",
        "aliases": ["linden luxury residences", "linden", "лінден", "линден"],
        "distinct": True,
    },
    "Chelsea Tower": {
        "district": "Голосіївський",
        "aliases": ["chelsea tower", "челсі тауер", "челси тауэр"],
        "distinct": True,
    },
    "Elyseum": {
        "district": "Шевченківський",
        "aliases": ["elyseum", "елізіум", "элизиум"],
        "distinct": True,
    },
    "Покровський Посад": {
        "district": "Шевченківський",
        "aliases": ["покровський посад", "покровский посад"],
        "distinct": True,
    },
    "Ярославів Град": {
        "district": "Шевченківський",
        "aliases": ["ярославів град", "ярославов град"],
        "distinct": True,
    },
    "Львівський Квартал": {
        "district": "Шевченківський",
        "aliases": ["львівський квартал", "львовский квартал"],
        "distinct": True,
    },
    "Smart Plaza Polytech": {
        "district": "Шевченківський",
        "aliases": ["smart plaza polytech", "smart plaza політех", "смарт плаза політех"],
        "distinct": True,
    },
    "Crystal Park": {
        "district": "Шевченківський",
        "aliases": ["crystal park tower", "crystal park", "кристал парк", "кристалл парк"],
        "distinct": True,
    },
    "Герцен-Парк": {
        "district": "Шевченківський",
        "aliases": ["герцен-парк", "герцен парк"],
        "distinct": True,
    },
    "Срібна Вежа": {
        "district": "Шевченківський",
        "aliases": ["срібна вежа", "серебряная башня"],
        "distinct": True,
    },
    "Златоустівський": {
        "district": "Шевченківський",
        "aliases": ["златоустівський", "златоустовский"],
        "distinct": False,
    },
    "Старокиївський": {
        "district": "Шевченківський",
        "aliases": ["старокиївський", "старокиевский"],
        "distinct": False,
    },
    "Hoffmann Haus": {
        "district": "Шевченківський",
        "aliases": ["hoffmann haus", "гофман хаус", "хоффманн хаус"],
        "distinct": True,
    },
    "AuRoom": {
        "district": "Шевченківський",
        "aliases": ["auroom", "аурум"],
        "distinct": True,
    },
    "38 Жемчужина": {
        "district": "Голосіївський",
        "aliases": [
            "38 жемчужина",
            "38-я жемчужина",
            "тридцать восьмая жемчужина",
            "тридцять восьма перлина",
        ],
        "distinct": True,
    },
    "Chicago Central House": {
        "district": "Голосіївський",
        "aliases": ["chicago central house", "chicago", "чікаго", "чикаго"],
        "distinct": True,
    },
    "New York Concept House": {
        "district": "Голосіївський",
        "aliases": ["new york concept house", "new york", "нью йорк", "нью-йорк"],
        "distinct": True,
    },
    "Парк-хол Горький": {
        "district": "Голосіївський",
        "aliases": ["парк-хол горький", "парк холл горький"],
        "distinct": True,
    },
    "SkyLand": {
        "district": "Голосіївський",
        "aliases": ["skyland", "скайленд", "скайлен"],
        "distinct": True,
    },
    "Royal Tower": {
        "district": "Голосіївський",
        "aliases": ["royal tower", "роял тауер", "роял тауэр"],
        "distinct": True,
    },
    "Каховська": {
        "district": "Дніпровський",
        "aliases": ["каховська", "каховская"],
        "distinct": False,
    },
    "Star City": {
        "district": "Дніпровський",
        "aliases": ["star city", "стар сіті", "стар сити"],
        "distinct": True,
    },
    "Terracotta": {
        "district": "Дарницький",
        "aliases": ["terracotta", "терракотта", "теракота"],
        "distinct": True,
    },
    "Метрополія": {
        "district": "Дарницький",
        "aliases": ["метрополія 2", "метрополия 2", "метрополія", "метрополия"],
        "distinct": True,
    },
    "Урлівський": {
        "district": "Дарницький",
        "aliases": [
            "урлівський 2",
            "урловский 2",
            "урлівський 1",
            "урловский 1",
            "урлівський",
            "урловский",
        ],
        "distinct": False,
    },
    "Срібний Бриз": {
        "district": "Дніпровський",
        "aliases": ["срібний бриз", "серебряный бриз"],
        "distinct": True,
    },
    "Riviera Riverside": {
        "district": "Дніпровський",
        "aliases": ["riviera riverside", "ривьера риверсайд"],
        "distinct": True,
    },
    "Авеню 42": {
        "district": "Голосіївський",
        "aliases": ["авеню 42", "avenue 42"],
        "distinct": True,
    },
    "Деміївський квартал": {
        "district": "Голосіївський",
        "aliases": ["деміївський квартал", "демеевский квартал"],
        "distinct": True,
    },
    "Голосіївський Двір": {
        "district": "Голосіївський",
        "aliases": ["голосіївський двір", "голосеевский двор"],
        "distinct": True,
    },
    "Елегант": {
        "district": "Шевченківський",
        "aliases": ["елегант", "элегант"],
        "distinct": True,
    },
    "Standard One": {
        "district": "Голосіївський",
        "aliases": ["standard one", "standard 1", "s1"],
        "distinct": True,
    },
    "Панорама Сіті": {
        "district": "Голосіївський",
        "aliases": ["панорама сіті", "панорама сити"],
        "distinct": True,
    },
    "Concept Hall": {
        "district": "Голосіївський",
        "aliases": ["concept hall", "концепт хол", "концепт холл"],
        "distinct": True,
    },
    "Diplomat Hall": {
        "district": "Голосіївський",
        "aliases": ["diplomat hall", "дипломат хол", "дипломат холл"],
        "distinct": True,
    },
    "Resident Concept House": {
        "district": "Голосіївський",
        "aliases": ["resident concept house", "resident concept"],
        "distinct": True,
    },
    "Казка": {
        "district": "Голосіївський",
        "aliases": ["казка", "сказка"],
        "distinct": False,
    },
    "Ново-Деміївський": {
        "district": "Голосіївський",
        "aliases": [
            "ново-деміївський",
            "ново-демеевский",
            "новодеміївський",
            "новодемеевский",
        ],
        "distinct": True,
    },
}

# Compile search structures for ЖК
_COMPILED_JK_ALIASES: List[Tuple[str, str, Optional[str], bool]] = []
for _jk_name, _jk_data in KYIV_RESIDENTIAL_COMPLEXES.items():
    for _jk_alias in _jk_data["aliases"]:
        _COMPILED_JK_ALIASES.append(
            (_jk_alias, _jk_name, _jk_data.get("district"), _jk_data.get("distinct", False))
        )
_COMPILED_JK_ALIASES.sort(key=lambda x: len(x[0]), reverse=True)

JK_PREFIX_RE = re.compile(
    r"\b(?:жк|житлов\w*\s+комплекс\w*|жилой\s+комплекс\w*|ж/к|клубн\w*\s+будинок\w*|к/б)\b",
    re.IGNORECASE,
)
JK_QUOTED_RE = re.compile(
    r"\b(?:жк|житлов\w*\s+комплекс\w*|жилой\s+комплекс\w*|ж/к)\s+[«\"“'’]([^»\"”'’\n\r]{2,40})[»\"”'’]",
    re.IGNORECASE,
)
JK_STOP_WORDS_RE = re.compile(
    r"^(?:київ|києва|оренда|продаж|квартира|будинок|офіс|приміщення|центр|поділ|печерськ|голосієво)$",
    re.IGNORECASE,
)


def extract_residential_complex(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """
    Detects residential complex (ЖК) in text.
    Returns (canonical_name, district) or None.

    Priority:
    1. Check against KYIV_RESIDENTIAL_COMPLEXES aliases:
       - Match longer aliases first.
       - If alias is distinct, can match with or without explicit 'ЖК' keyword.
       - If alias is not distinct (street name collision), require explicit 'ЖК' keyword.
    2. Fallback to generic regex: 'ЖК <Name>' / 'житловий комплекс <Name>'
       extracting valid complex names in quotes.
    """
    if not text:
        return None

    clean = strip_html(text)
    lower = clean.lower()

    # 1. First check dictionary aliases (sorted by length DESC)
    for alias, canonical, district, distinct in _COMPILED_JK_ALIASES:
        pattern = r"\b" + re.escape(alias) + r"\b"
        m = re.search(pattern, lower)
        if m:
            start_pos = m.start()
            prefix_ctx = lower[max(0, start_pos - 35) : start_pos]
            suffix_ctx = lower[m.end() : min(len(lower), m.end() + 25)]

            # Reject if preceded or followed by metro station indicator
            if re.search(
                r"\b(?:метро|ст\.?\s*м\.?|станці[яєїією]\s+(?:метро)?|до\s+метро)\s*[:\-–—]?\s*$",
                prefix_ctx,
            ):
                continue
            if re.search(r"^\s*(?:метро|станці[яєїі])\b", suffix_ctx):
                continue

            # If distinct, alias alone is sufficient
            if distinct:
                return canonical, district

            # If not distinct (common word/street collision), require explicit JK indicator
            if JK_PREFIX_RE.search(prefix_ctx):
                return canonical, district

    # 2. Quoted generic ЖК: e.g. ЖК "Назва" or житловий комплекс «Назва»
    m_quoted = JK_QUOTED_RE.search(clean)
    if m_quoted:
        name = m_quoted.group(1).strip()
        name = re.sub(r"^[«\"“'’\s]+|[»\"”'’\s]+$", "", name)
        if len(name) >= 2 and not JK_STOP_WORDS_RE.match(name):
            return name, None

    return None


def clean_number_str(val: Any) -> str:
    """Formats a float or int into a dot-separated string without scientific notation or trailing zeroes."""
    if val is None or val == "":
        return ""
    try:
        f = float(str(val).replace(",", ".").strip())
        if f.is_integer():
            return str(int(f))
        return f"{f:.2f}".rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return ""


def clean_int_str(val: Any) -> str:
    """Formats an integer into a clean string without decimals or spaces."""
    if val is None or val == "":
        return ""
    try:
        f = float(str(val).replace(",", ".").strip())
        return str(int(round(f)))
    except (ValueError, TypeError):
        return ""


PREFIX_TYPE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:вул|вулиц[яіе]|ул|улиц[ае])\.?\s*$", re.IGNORECASE), "вулиця"),
    (re.compile(r"\b(?:пров|провул(?:ок|ка|ку)|пер|переул(?:ок|ка|ку))\.?\s*$", re.IGNORECASE), "провулок"),
    (re.compile(r"\b(?:просп|проспект[уі]?|пр-т)\.?\s*$", re.IGNORECASE), "проспект"),
    (re.compile(r"\b(?:бульв|бульвар[уі]?|б-р)\.?\s*$", re.IGNORECASE), "бульвар"),
    (re.compile(r"\b(?:площ[аі]|майдан|пл)\.?\s*$", re.IGNORECASE), "площа"),
    (re.compile(r"\b(?:набережн[аея])\.?\s*$", re.IGNORECASE), "набережна"),
    (re.compile(r"\b(?:узвіз|спуск)\.?\s*$", re.IGNORECASE), "узвіз"),
    (re.compile(r"\b(?:шосе)\.?\s*$", re.IGNORECASE), "шосе"),
    (re.compile(r"\b(?:але[яі])\.?\s*$", re.IGNORECASE), "алея"),
    (re.compile(r"\b(?:тупик)\.?\s*$", re.IGNORECASE), "тупик"),
    (re.compile(r"\b(?:проїзд)\.?\s*$", re.IGNORECASE), "проїзд"),
]

SUFFIX_TYPE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^\s*(?:вулиц[яіе]|улиц[ае]|вул\.?)\b", re.IGNORECASE), "вулиця"),
    (re.compile(r"^\s*(?:провул(?:ок|ка|ку)|переул(?:ок|ка|ку)|пров\.?|пер\.?)\b", re.IGNORECASE), "провулок"),
    (re.compile(r"^\s*(?:проспект[уі]?|просп\.?|пр-т\.?)\b", re.IGNORECASE), "проспект"),
    (re.compile(r"^\s*(?:бульвар[уі]?|бульв\.?|б-р\.?)\b", re.IGNORECASE), "бульвар"),
    (re.compile(r"^\s*(?:площ[аі]|майдан|пл\.?)\b", re.IGNORECASE), "площа"),
    (re.compile(r"^\s*(?:набережн[аея])\b", re.IGNORECASE), "набережна"),
    (re.compile(r"^\s*(?:узвіз|спуск)\b", re.IGNORECASE), "узвіз"),
    (re.compile(r"^\s*(?:шосе)\b", re.IGNORECASE), "шосе"),
    (re.compile(r"^\s*(?:але[яі])\b", re.IGNORECASE), "алея"),
    (re.compile(r"^\s*(?:тупик)\b", re.IGNORECASE), "тупик"),
    (re.compile(r"^\s*(?:проїзд)\b", re.IGNORECASE), "проїзд"),
]

TITLES_AND_FIRST_NAMES: Set[str] = {
    "академіка", "професора", "генерала", "маршала", "архітектора",
    "адмірала", "композитора", "письменника", "гетьмана", "інженера", "полковника", "авіаконструктора",
    "івана", "миколи", "василя", "михайла", "олександра", "петра", "володимира", "юрія",
    "павла", "степана", "андрія", "григорія", "максима", "сергія", "бориса", "георгія",
    "анатолія", "костянтина", "якова", "євгена", "віктора", "ярослава", "дмитра", "тараса",
    "ігоря", "марка", "катерини", "марії", "раїси", "олексія", "федора", "ольги", "софії",
    "лесі", "богдана", "анни", "олени", "вадима", "романа", "вікентія", "чеслава", "леоніда",
    "всеволода", "наталії", "в’ячеслава", "в'ячеслава", "антоніни", "людмили", "філіппа",
    "семена", "демида", "артема", "данила", "назарія", "зої", "лаври", "валі", "тимофія",
    "кузьми", "платона", "луки", "іллі", "кирила", "гліба", "дем'яна", "дем’яна"
}
TITLES_SET: Set[str] = TITLES_AND_FIRST_NAMES

TYPE_PRIORITY: Dict[str, int] = {
    "вулиця": 10,
    "проспект": 9,
    "бульвар": 8,
    "набережна": 7,
    "шосе": 6,
    "площа": 5,
    "майдан": 5,
    "узвіз": 4,
    "алея": 3,
    "провулок": 2,
    "проїзд": 1,
    "тупик": 1,
    "дорога": 1,
}


class KyivAddressParser:
    """
    High-precision Kyiv Address Parser.
    Extracts canonical street, house number, and district from listing titles and descriptions.
    Uses n-gram token matching against indexed Kyiv streets dictionary with context validation.
    """

    def __init__(self, json_path: Optional[str] = None):
        path = json_path or os.path.join(os.path.dirname(__file__), "kyiv_streets.json")
        self.streets_data = {}
        self.canonical_to_districts: Dict[str, List[str]] = {}
        self.search_index: Dict[str, str] = {}
        self.typed_streets: Dict[Tuple[str, str], str] = {}

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.streets_data = json.load(f)

            for s in self.streets_data.get("streets", []):
                canonical = s.get("canonical", "").replace("\u0301", "").strip()
                districts = s.get("districts", [])
                if canonical:
                    self.canonical_to_districts[canonical] = districts

                name = s.get("name", "").replace("\u0301", "").strip()
                st_type = s.get("type", "").strip().lower()

                if st_type:
                    self.typed_streets[(name.lower(), st_type)] = canonical
                    for a in s.get("aliases", []):
                        a_clean = a.replace("\u0301", "").strip().lower()
                        self.typed_streets[(a_clean, st_type)] = canonical

                # Invert 2-word names (e.g. "Заболотного Академіка" <-> "Академіка Заболотного")
                parts = name.split()
                if len(parts) == 2:
                    p1, p2 = parts[0].lower(), parts[1].lower()
                    self.search_index[f"{p1} {p2}"] = canonical
                    self.search_index[f"{p2} {p1}"] = canonical
                    if st_type:
                        self.typed_streets[(f"{p1} {p2}", st_type)] = canonical
                        self.typed_streets[(f"{p2} {p1}", st_type)] = canonical
                        self.search_index[f"{st_type} {p1} {p2}"] = canonical
                        self.search_index[f"{st_type} {p2} {p1}"] = canonical
                        self.search_index[f"{p1} {p2} {st_type}"] = canonical
                        self.search_index[f"{p2} {p1} {st_type}"] = canonical
                        if st_type == "вулиця":
                            self.search_index[f"вул {p1} {p2}"] = canonical
                            self.search_index[f"вул {p2} {p1}"] = canonical
                        elif st_type == "проспект":
                            self.search_index[f"просп {p1} {p2}"] = canonical
                            self.search_index[f"просп {p2} {p1}"] = canonical
                        elif st_type == "бульвар":
                            self.search_index[f"бул {p1} {p2}"] = canonical
                            self.search_index[f"бул {p2} {p1}"] = canonical
                        elif st_type == "провулок":
                            self.search_index[f"пров {p1} {p2}"] = canonical
                            self.search_index[f"пров {p2} {p1}"] = canonical

                    # Person streets: generate 1-gram surname index for 2-word streets with titles/first names
                    surname = None
                    if p1 in TITLES_AND_FIRST_NAMES and p2 not in TITLES_AND_FIRST_NAMES:
                        surname = p2
                    elif p2 in TITLES_AND_FIRST_NAMES and p1 not in TITLES_AND_FIRST_NAMES:
                        surname = p1
                    if surname and len(surname) >= 4:
                        if st_type:
                            self.typed_streets[(surname, st_type)] = canonical
                            self.search_index[f"{st_type} {surname}"] = canonical
                            if st_type == "вулиця":
                                self.search_index[f"вул {surname}"] = canonical
                            elif st_type == "проспект":
                                self.search_index[f"просп {surname}"] = canonical
                            elif st_type == "бульвар":
                                self.search_index[f"бул {surname}"] = canonical
                            elif st_type == "провулок":
                                self.search_index[f"пров {surname}"] = canonical
                        if surname not in self.search_index:
                            self.search_index[surname] = canonical

            # Merge search index from json with type priority
            for k, v in self.streets_data.get("search_index", {}).items():
                k_clean = k.replace("\u0301", "").strip().lower()
                v_clean = v.replace("\u0301", "").strip()
                if len(k_clean) >= 4:
                    curr_canon = self.search_index.get(k_clean)
                    if curr_canon:
                        curr_type = curr_canon.split()[0].lower() if curr_canon else ""
                        new_type = v_clean.split()[0].lower() if v_clean else ""
                        if TYPE_PRIORITY.get(new_type, 0) > TYPE_PRIORITY.get(curr_type, 0):
                            self.search_index[k_clean] = v_clean
                    else:
                        self.search_index[k_clean] = v_clean
                    new_type = v_clean.split()[0].lower() if v_clean else ""
                    if new_type:
                        self.typed_streets[(k_clean, new_type)] = v_clean

        # Merge manual synonyms and known renames
        for k, v in STREET_SYNONYMS.items():
            k_clean = k.lower()
            self.search_index[k_clean] = v
            st_type = v.split()[0].lower() if v else ""
            if st_type:
                self.typed_streets[(k_clean, st_type)] = v

        # Fix missing district for known streets
        dist_fixes = {
            "вулиця Михайла Максимовича": ["Голосіївський"],
            "бульвар Тадея Рильського": ["Голосіївський"],
            "вулиця Джона Маккейна": ["Печерський"],
            "вулиця Іоанна Павла II": ["Печерський"],
            "вулиця Всеволода Змієнка": ["Подільський"],
            "вулиця Андрія Верхогляда": ["Печерський"],
            "вулиця Драгомирова Михайла": ["Печерський"],
        }
        for st_name, dsts in dist_fixes.items():
            if st_name not in self.canonical_to_districts or not self.canonical_to_districts[st_name]:
                self.canonical_to_districts[st_name] = dsts

        # Generate Russian feminine/masculine/mixed adjective variants
        # (e.g. ракетная -> ракетна, жилянская -> жилянська, лобановского -> лобановського)
        ru_synonyms = {}
        for k, canon in list(self.search_index.items()):
            words = k.split()
            if not words:
                continue
            last = words[-1]
            ru_last = None
            if last.endswith("ська"):
                ru_last = last[:-4] + "ская"
            elif last.endswith("цька"):
                ru_last = last[:-4] + "цкая"
            elif last.endswith("на") and len(last) > 3:
                ru_last = last[:-2] + "ная"
            elif last.endswith("ва") and len(last) > 3:
                ru_last = last[:-2] + "вая"
            elif last.endswith("та") and len(last) > 3:
                ru_last = last[:-2] + "тая"
            elif last.endswith("ра") and len(last) > 3:
                ru_last = last[:-2] + "рая"
            elif last.endswith("ського"):
                ru_last = last[:-6] + "ского"

            if ru_last:
                ru_key = " ".join(words[:-1] + [ru_last])
                if ru_key not in self.search_index:
                    ru_synonyms[ru_key] = canon
                    st_type = canon.split()[0].lower() if canon else ""
                    if st_type:
                        self.typed_streets[(ru_key, st_type)] = canon

        self.search_index.update(ru_synonyms)

    def extract_street_and_house(self, text: str) -> Tuple[str, str]:
        """
        Extracts (canonical_street, house_number) from a given text segment using fast n-grams.
        Eliminates false positives by validating context (rejecting metro stations, district names,
        recreation zones, and ambiguous adjectives like 'нова', 'світла' without street indicators).
        Returns ("", "") if not found.
        """
        if not text:
            return "", ""

        clean_text = strip_html(text)
        # Separate glued prefixes like ул.Ракетная or вул.Жилянська or м.Демеевская
        clean_text = re.sub(
            r"\b(вул|ул|просп|пр-т|бул|б-р|пров|пер|м|ст\.?\s*м)\.([а-яА-ЯіІїЇєЄґҐ])",
            r"\1. \2",
            clean_text,
            flags=re.IGNORECASE,
        )
        token_matches = list(re.finditer(r"\b[\w]+(?:-[\w]+)*\b", clean_text.lower()))
        num_tokens = len(token_matches)
        if num_tokens == 0:
            return "", ""

        candidates = []

        # Try lengths from 4 down to 1
        for length in (4, 3, 2, 1):
            for i in range(num_tokens - length + 1):
                phrase = " ".join(t.group() for t in token_matches[i : i + length]).strip(".- ")
                phrase_words = phrase.split()
                real_length = len(phrase_words)
                if len(phrase) < 4 or phrase not in self.search_index:
                    continue

                start_char = token_matches[i].start()
                end_char = token_matches[i + length - 1].end()

                # Surrounding context
                prefix_ctx = clean_text[max(0, start_char - 50):start_char].lower()
                suffix_ctx = clean_text[end_char:min(len(clean_text), end_char + 30)].lower()

                # 1. Reject if preceded or followed by metro station indicator
                if re.search(
                    r"\b(?:метро|ст\.?\s*м\.?|станці[яєїією]\s+(?:метро)?|м\.?|біля\s+метро|до\s+метро(?:\s+\d+\s*(?:хв|хвилин|мин))?)\s*[:\-–—]?\s*(?:[\"«„'’(][^\"»“'’)]*|\s*)$",
                    prefix_ctx
                ):
                    continue
                if re.search(r"^\s*[\"»“'’)]?[,;\-–—]?\s*(?:-\s*\d+\s*(?:хв|хвилин|мин)\s*(?:пішки|пешком|до\s+метро)|метро|станці[яєїі])\b", suffix_ctx):
                    continue

                # 2. Reject if preceded by residential/business complex without street keyword
                if re.search(r"\b(?:жк|житлов\w*\s+комплекс\w*|клубн\w*\s+будинк\w*|комплекс\w*(?:\s+\w+){0,3}?)\s*[\"«„'’]?\s*$", prefix_ctx):
                    continue

                # 3. Reject if preceded or followed by recreation/park indicator
                if re.search(
                    r"(?:"
                    r"\b(?:зон[а-я]*|парк[а-я]*|урочищ[а-я]*|сквер[а-я]*)(?:\s+(?:ім\.?|імені|культури|та|і|відпочинку))*\s*[\"«„'’]?\s*$"
                    r"|"
                    r"\b(?:зон[а-я]*|парк[а-я]*|урочищ[а-я]*|сквер[а-я]*)\s*:\s*(?:[^;,\n]{1,30}[;,]\s*)*[\"«„'’]?\s*$"
                    r")",
                    prefix_ctx
                ):
                    continue
                if re.search(r"^\s*[\"»“'’]?\s*(?:парк[а-я]*|урочищ[а-я]*|сквер[а-я]*)\b", suffix_ctx):
                    continue

                # 4. Reject if followed by district indicator
                if re.search(r"^\s*(?:р-н[уі]?|район[уі]?|р-он[уі]?)\b", suffix_ctx):
                    continue

                # Street indicators in prefix or suffix
                has_prefix_ind = bool(re.search(
                    r"\b(?:вул|вулиц[яіе]|ул|улиц[ае]|просп|проспект[уі]?|пр-т|бульв|бульвар[уі]?|б-р|пров|провул(?:ок|ка|ку)|пер|переул(?:ок|ка|ку)|узвіз|спуск|набережн[аея]|шосе|але[яі]|площ[аі]|майдан|тупик)\.?\s*$",
                    prefix_ctx
                ))
                has_suffix_ind = bool(re.search(
                    r"^\s*(?:вулиц[яіе]|улиц[ае]|проспект[уі]?|бульвар[уі]?|провул(?:ок|ка|ку)|переул(?:ок|ка|ку)|узвіз|спуск|набережн[аея]|шосе|але[яі]|площ[аі]|майдан|тупик|\b(?:вул|ул|просп|пр-т|бульв|б-р|пров|пер)\.?)(?:\s+|[.,;!?]|$)",
                    suffix_ctx,
                    re.IGNORECASE,
                ))

                has_loc_prep = bool(re.search(r"\b(?:на|по|за\s+адресою|адреса|по\s+адресу|адрес)\s*[:\-–—]?\s*$", prefix_ctx))

                phrase_has_indicator = any(
                    w in ("вулиця", "вул", "проспект", "просп", "шосе", "бульвар", "бульв", "провулок", "пров", "узвіз", "набережна", "площа", "майдан", "тракт", "тупик", "алея")
                    for w in phrase.split()
                )

                # Extract house number from tail
                tail = clean_text[end_char:]
                house = self._extract_house_from_tail(tail)

                # 5. Single-word matches (real_length == 1) MUST have an explicit street indicator or house number
                if real_length == 1:
                    if phrase in AMBIGUOUS_STREET_WORDS:
                        if not (phrase_has_indicator or has_prefix_ind or has_suffix_ind):
                            continue
                    elif not (phrase_has_indicator or has_prefix_ind or has_suffix_ind or house or has_loc_prep):
                        continue

                # Determine detected street type if prefix/suffix indicator is present
                detected_type = ""
                for pat, t in PREFIX_TYPE_PATTERNS:
                    if pat.search(prefix_ctx):
                        detected_type = t
                        break
                if not detected_type:
                    for pat, t in SUFFIX_TYPE_PATTERNS:
                        if pat.search(suffix_ctx):
                            detected_type = t
                            break

                if detected_type and (phrase, detected_type) in self.typed_streets:
                    canonical = self.typed_streets[(phrase, detected_type)]
                else:
                    canonical = self.search_index[phrase]

                # Disambiguate ЖК Respublika: residential buildings in this complex are on бульвар Тадея Рильського
                if canonical == "вулиця Рильського Максима":
                    lower_all = (clean_text + " " + text).lower()
                    is_respublika_complex = bool(re.search(r"\b(?:жк|комплекс\w*)\s*[\"«„'’]?\s*(?:республік\w*|respublika)\b", lower_all))
                    has_tadey = "таде" in lower_all
                    if (is_respublika_complex or has_tadey) and not re.search(r"\b(?:парк\w*|урочищ\w*)\b.*?\bрильськ", lower_all):
                        canonical = "бульвар Тадея Рильського"

                # Calculate confidence score
                score = 0
                if phrase_has_indicator or has_prefix_ind or has_suffix_ind:
                    score += 100
                elif has_loc_prep:
                    score += 60
                if house:
                    score += 50
                if real_length >= 2:
                    score += 50 * real_length

                if score <= 0:
                    continue

                candidates.append((score, -start_char, real_length, canonical, house))

        if not candidates:
            return "", ""

        # Sort by score DESC, earlier start pos, longer phrase
        candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        best = candidates[0]
        return best[3], best[4]

    def _extract_house_from_tail(self, tail: str) -> str:
        """Extracts valid house number from the text trailing the street match."""
        m = HOUSE_NUMBER_REGEX.search(tail)
        if m:
            house_str = m.group(1).strip()
            # Clean up trailing punctuation or spaces
            house_str = re.sub(r"^[,\s./]+|[,\s./]+$", "", house_str)
            base_digits = re.match(r"\d+", house_str)
            if base_digits:
                num = int(base_digits.group())
                # House numbers in Kyiv are realistic (< 500)
                if num < 500:
                    return house_str.upper()
        return ""

    @staticmethod
    def extract_residential_complex(text: str) -> Optional[Tuple[str, Optional[str]]]:
        """Extracts residential complex (ЖК) and district from text."""
        return extract_residential_complex(text)

    def parse(
        self,
        title: str,
        description: Optional[str] = None,
        district_hint: Optional[str] = None,
        fallback_jk: bool = False,
    ) -> Tuple[str, str, str]:
        """
        Main address parsing entrypoint.
        Priority:
        1. Find street and house in Title.
        2. If street found in Title but house is missing, search Description for house (or street + house).
        3. If street not found in Title, search Description.
        4. Resolve district from hint or canonical street's mapped districts.
        5. If fallback_jk is True and house is missing, fallback to residential complex (ЖК).
        """
        street, house = self.extract_street_and_house(title)

        if street and not house and description:
            # Look for house in description only if it belongs to the same street
            desc_street, desc_house = self.extract_street_and_house(description)
            if desc_house and desc_street == street:
                house = desc_house

        if not street and description:
            street, house = self.extract_street_and_house(description)

        # Resolve district
        district = ""
        if street and street in self.canonical_to_districts and self.canonical_to_districts[street]:
            valid_districts = self.canonical_to_districts[street]
            if district_hint and district_hint in valid_districts:
                district = district_hint
            else:
                district = valid_districts[0]
        elif district_hint and district_hint in KYIV_DISTRICTS:
            district = district_hint

        # Stage 2 ЖК fallback rule: if house is missing and fallback_jk is enabled
        if fallback_jk and not house:
            combined = f"{title}\n{description or ''}"
            jk_info = self.extract_residential_complex(combined)
            if jk_info:
                jk_name, jk_dist = jk_info
                formatted_jk = jk_name if jk_name.startswith("ЖК ") else f"ЖК {jk_name}"
                if not street:
                    street = formatted_jk
                if not district and jk_dist:
                    district = jk_dist

        return street, house, district


# Global parser singleton instance
_address_parser: Optional[KyivAddressParser] = None


def get_address_parser() -> KyivAddressParser:
    global _address_parser
    if _address_parser is None:
        _address_parser = KyivAddressParser()
    return _address_parser


class BaseNormalizer:
    """Base helper methods for extracting parameters and text features."""

    @staticmethod
    def get_param_value(params: List[Dict[str, Any]], key: str) -> Any:
        """Finds param dict in OLX params list by key."""
        for p in params:
            if p.get("key") == key:
                val = p.get("value")
                if isinstance(val, dict):
                    return val.get("key") or val.get("value") or val.get("label")
                return val
        return None

    @staticmethod
    def get_param_label(params: List[Dict[str, Any]], key: str) -> Optional[str]:
        """Finds human-readable label in OLX params list by key."""
        for p in params:
            if p.get("key") == key:
                val = p.get("value")
                if isinstance(val, dict):
                    return val.get("label") or str(val.get("value", ""))
                return str(val) if val is not None else None
        return None

    @staticmethod
    def extract_seller(offer: Dict[str, Any], text: str) -> str:
        """Determines seller type: 'owner', 'agency', or 'developer'."""
        lower = text.lower()
        url = offer.get("url", "").lower()

        # Check for developer first
        if re.search(r"\b(?:забудовник[ау]?|застройщик[ау]?|від\s+забудовника|от\s+застройщика|девелопер|відділ\s+продажу?|отдел\s+продаж)\b", lower):
            return "developer"

        # Check for explicit owner markers in text or URL
        has_owner_url = any(k in url for k in ("vd-vlasnika", "ot-sobstvennika", "vlasnik", "sobstvennik"))
        has_owner_text = bool(re.search(
            r"\b(?:від\s+власника|от\s+собственника|от\s+хозяина|я\s+власник|я\s+собственник|я\s+хозяин|власник\b|собственник\b|без\s+посередників|без\s+посредников)",
            lower
        ))

        # Check for agency markers that contradict owner claims
        has_agency_markers = bool(re.search(
            r"\b(?:комісія\s+\d+|комиссия\s+\d+|рієлтор|риелтор|агентств|ан\s+[\"«]|комісійні|комиссионные)\b",
            lower
        ))

        if (has_owner_url or has_owner_text) and not has_agency_markers:
            return "owner"

        business = offer.get("business")
        if business is False:
            return "owner"
        if business is True:
            return "agency"

        if has_owner_text:
            return "owner"

        return "agency"

    @staticmethod
    def extract_price_and_currency(offer: Dict[str, Any], text: str) -> Tuple[str, str]:
        """Extracts clean price number and currency (UAH, USD, EUR)."""
        params = offer.get("params", [])
        for p in params:
            if p.get("key") == "price":
                val = p.get("value", {})
                if isinstance(val, dict):
                    raw_p = val.get("value")
                    curr = val.get("currency")
                    if raw_p is not None:
                        price_str = clean_int_str(raw_p)
                        currency_str = str(curr).upper() if curr else "UAH"
                        return price_str, currency_str

        # Fallback from text
        match = re.search(r"(\d[\d\s]{2,9})\s*(грн|uah|usd|\$|eur|€)", text, re.IGNORECASE)
        if match:
            p_val = match.group(1).replace(" ", "")
            curr_raw = match.group(2).lower()
            if curr_raw in ("usd", "$"):
                return clean_int_str(p_val), "USD"
            if curr_raw in ("eur", "€"):
                return clean_int_str(p_val), "EUR"
            return clean_int_str(p_val), "UAH"

        return "", ""

    @staticmethod
    def extract_commission_pct(offer: Dict[str, Any], seller: str, text: str) -> str:
        """
        Extracts commission percent (0 - 100).
        Rules:
        - If 'без комісії' or commission param indicates 0 -> '0'
        - If seller is 'owner' and commission is not mentioned -> '0'
        - If regex matches specific percentage (e.g. '50%') -> that percentage
        - If agency and not mentioned -> ''
        """
        params = offer.get("params", [])
        for p in params:
            if p.get("key") == "commission":
                val = p.get("value")
                if isinstance(val, dict):
                    if val.get("key") == "1" or val.get("label") == "Без комісії":
                        return "0"

        lower = text.lower()
        if re.search(r"\b(?:без\s*комісі[їє]|0\s*%\s*(?:комісі[їє])?|без\s*%)\b", lower):
            return "0"

        pct_match = re.search(
            r"(?:комісія|риелторские|рієлторські|комісійні|комиссия)[^\d\n\r]{0,25}(\d+)\s*%",
            lower,
        )
        if not pct_match:
            pct_match = re.search(
                r"(\d+)\s*%[^\d\n\r]{0,25}(?:комісія|риелторские|рієлторські|комісійні|комиссия)",
                lower,
            )

        if pct_match:
            try:
                num = int(pct_match.group(1))
                if 0 <= num <= 100:
                    return str(num)
            except ValueError:
                pass

        if seller == "owner":
            return "0"

        return ""


class ResidentialNormalizer(BaseNormalizer):
    """Normalizes residential long-term rent offers to 23 canonical fields."""

    @staticmethod
    def extract_rooms(offer: Dict[str, Any], text: str) -> str:
        """Extracts number of rooms."""
        params = offer.get("params", [])
        val = BaseNormalizer.get_param_value(params, "number_of_rooms_string")
        if val:
            mapping = {
                "odnokomnatnye": "1",
                "dvuhkomnatnye": "2",
                "trehkomnatnye": "3",
                "chetyrehkomnatnye": "4",
                "pyatikomnatnye": "5",
                "pyatikomnatnye_i_bolee": "5",
            }
            if str(val) in mapping:
                return mapping[str(val)]

        label = BaseNormalizer.get_param_label(params, "number_of_rooms_string")
        if label:
            m = re.search(r"(\d+)", label)
            if m:
                return m.group(1)

        # Regex fallback from text
        m = re.search(r"\b([1-9])\s*[- ]*(?:к(?:імн)?|комн?(?:аты|атная)?|кімнатна|комнатная)\b", text, re.IGNORECASE)
        if m:
            return m.group(1)

        if re.search(r"\b(?:студія|студия|смарт)\b", text, re.IGNORECASE):
            return "1"

        return ""

    @staticmethod
    def extract_building_type(offer: Dict[str, Any], text: str) -> str:
        """Maps building type to Latin tag."""
        params = offer.get("params", [])
        raw = BaseNormalizer.get_param_value(params, "property_type_appartments_sale")
        lbl = BaseNormalizer.get_param_label(params, "property_type_appartments_sale") or ""

        mapping = {
            "1": "tsarist",               # Будинок до 1917 року
            "2": "stalinka",              # Сталінка
            "3": "khrushchyovka",         # Хрущовка
            "4": "cheshka",               # Чешка
            "5": "dormitory",             # Гостинка
            "6": "special_project",       # Совмін
            "7": "dormitory",             # Гуртожиток
            "8": "special_project",       # Житловий фонд 80-90-і
            "9": "special_project",       # Житловий фонд 91-2000-і
            "10": "new_build_2010s_plus", # 2001-2010
            "11": "new_build_2010s_plus", # 2011-2020
            "12": "new_build_2010s_plus", # від 2021
        }
        if raw is not None and str(raw) in mapping:
            return mapping[str(raw)]

        lbl_lower = lbl.lower()
        if "від 2011" in lbl_lower or "від 2021" in lbl_lower or "2011-2020" in lbl_lower or "2001-2010" in lbl_lower:
            return "new_build_2010s_plus"
        if "хрущ" in lbl_lower:
            return "khrushchyovka"
        if "сталін" in lbl_lower:
            return "stalinka"
        if "чеськ" in lbl_lower or "чешк" in lbl_lower:
            return "cheshka"
        if "царськ" in lbl_lower or "дореволюц" in lbl_lower or "до 1917" in lbl_lower:
            return "tsarist"
        if "спецпроект" in lbl_lower or "совмін" in lbl_lower or "80-90" in lbl_lower or "91-2000" in lbl_lower:
            return "special_project"
        if "гуртожит" in lbl_lower or "гостинк" in lbl_lower:
            return "dormitory"

        # Search in text
        t_lower = text.lower()
        if re.search(r"\b(?:новобудов[аі]|новостро[йи]|жк|новий\s+будинок)\b", t_lower):
            return "new_build_2010s_plus"
        if re.search(r"\b(?:сталінк[аи]|сталинк[аи])\b", t_lower):
            return "stalinka"
        if re.search(r"\b(?:хрущовк[аи]|хрущевк[аи])\b", t_lower):
            return "khrushchyovka"
        if re.search(r"\b(?:чеськ[ийого]|чешк[аи])\b", t_lower):
            return "cheshka"
        if re.search(r"\b(?:царськ[ийого]|дореволюц[ійного])\b", t_lower):
            return "tsarist"
        if re.search(r"\b(?:гуртожит[окка]|гостинк[аи])\b", t_lower):
            return "dormitory"
        if re.search(r"\bспецпроект\b", t_lower):
            return "special_project"

        return ""

    @staticmethod
    def extract_renovation(offer: Dict[str, Any], text: str) -> str:
        """Maps renovation condition to Latin tag."""
        params = offer.get("params", [])
        raw = BaseNormalizer.get_param_value(params, "repair")
        mapping = {
            "1": "author",          # Авторський проект
            "2": "euro",            # Євроремонт
            "3": "cosmetic",        # Косметичний ремонт
            "4": "living",          # Житловий стан
            "5": "after_builders",   # Після будівельників
            "6": "white_box",       # Під чистове оздоблення
        }
        if raw is not None and str(raw) in mapping:
            return mapping[str(raw)]

        t_lower = text.lower()
        if re.search(r"\b(?:дизайнерськ[ийого]|авторськ[ийого]|дизайн[- ]ремонт)\b", t_lower):
            return "author"
        if re.search(r"\b(?:євроремонт[у]?|евроремонт[а]?)\b", t_lower):
            return "euro"
        if re.search(r"\bкосметичн[ийого]\b", t_lower):
            return "cosmetic"
        if re.search(r"\b(?:житлов[ийого]\s+стан|жилое\s+состояние)\b", t_lower):
            return "living"
        if re.search(r"\b(?:під\s+чистове|під\s+чистову|под\s+чистовую|white\s*box)\b", t_lower):
            return "white_box"
        if re.search(r"\b(?:після\s+будівельників|после\s+строителей|чернов[ийого])\b", t_lower):
            return "after_builders"
        if re.search(r"\b(?:потребує\s+ремонту|требует\s+ремонта|під\s+ремонт)\b", t_lower):
            return "needs_repair"

        return ""

    @staticmethod
    def extract_furniture(offer: Dict[str, Any], text: str) -> str:
        """Maps furniture presence: full, partial, none."""
        params = offer.get("params", [])
        raw = BaseNormalizer.get_param_value(params, "furnish")
        lbl = BaseNormalizer.get_param_label(params, "furnish") or ""

        if raw == "yes" or "з меблями" in lbl.lower():
            return "full"
        if raw == "no" or "без меблів" in lbl.lower():
            return "none"
        if raw == "partial" or "частково" in lbl.lower():
            return "partial"

        t_lower = text.lower()
        if re.search(r"\b(?:без\s+меблів|без\s+мебели|не\s+мебльована)\b", t_lower):
            return "none"
        if re.search(r"\b(?:частково\s+мебльована|тільки\s+кухня|частково\s+з\s+меблями)\b", t_lower):
            return "partial"
        if re.search(r"\b(?:з\s+меблями|мебльована|меблирована|повністю\s+мебльована)\b", t_lower):
            return "full"

        return ""

    @staticmethod
    def extract_appliances(offer: Dict[str, Any], text: str) -> str:
        """
        Extracts appliances as a comma-separated string of Latin tags.
        Tags: ac, washer, boiler, fridge, dishwasher, microwave, oven, tv, dryer, stove
        """
        tags: Set[str] = set()
        params = offer.get("params", [])

        # Check appliances_2 and multimedia_2 and comfort
        for p in params:
            val = p.get("value")
            keys = []
            if isinstance(val, dict):
                k = val.get("key")
                if isinstance(k, list):
                    keys.extend(k)
                elif k:
                    keys.append(str(k))
            elif isinstance(val, list):
                keys.extend(val)

            for item in keys:
                item_str = str(item).lower()
                if item_str in ("air_conditioning", "conditioner"):
                    tags.add("ac")
                elif item_str in ("washer", "washing_machine"):
                    tags.add("washer")
                elif item_str in ("boiler", "water_heater"):
                    tags.add("boiler")
                elif item_str in ("fridge", "refrigerator"):
                    tags.add("fridge")
                elif item_str in ("dishwasher",):
                    tags.add("dishwasher")
                elif item_str in ("microwave",):
                    tags.add("microwave")
                elif item_str in ("oven",):
                    tags.add("oven")
                elif item_str in ("tv", "television"):
                    tags.add("tv")
                elif item_str in ("dryer", "drying_machine"):
                    tags.add("dryer")
                elif item_str in ("stove", "cooker", "hob"):
                    tags.add("stove")

        # Text search
        t_lower = text.lower()
        if re.search(r"\b(?:кондиціонер[аи]?|кондиционер[аы]?|кондей|ac)\b", t_lower):
            tags.add("ac")
        if re.search(r"\b(?:пральн[аоюя]|стиральн[аоюя]|пралк[аи]|стиралк[аи])\b", t_lower):
            tags.add("washer")
        if re.search(r"\b(?:бойлер[а]?|водонагрівач[а]?|водонагревател[ья])\b", t_lower):
            tags.add("boiler")
        if re.search(r"\b(?:холодильник[а]?)\b", t_lower):
            tags.add("fridge")
        if re.search(r"\b(?:посудомийн[аоюя]|посудомойк[аи])\b", t_lower):
            tags.add("dishwasher")
        if re.search(r"\b(?:мікрохвильов[каи]|микроволновк[аи]|свч)\b", t_lower):
            tags.add("microwave")
        if re.search(r"\b(?:духов(?:ка| шафа)|духовк[аи]|духовой\s+шкаф)\b", t_lower):
            tags.add("oven")
        if re.search(r"\b(?:телевізор[а]?|телевизор[а]?|тв|smart\s*tv)\b", t_lower):
            tags.add("tv")
        if re.search(r"\b(?:сушильн[аоюя]\s*машин[аи]|сушарк[аи])\b", t_lower):
            tags.add("dryer")
        if re.search(r"\b(?:плит[аи]|варильн[аоюя]|варочн[аоюя])\b", t_lower):
            tags.add("stove")

        if not tags:
            return ""

        # Deterministic tag order matching manual template conventions
        order = ["ac", "washer", "boiler", "fridge", "dishwasher", "microwave", "oven", "tv", "dryer", "stove"]
        ordered = [t for t in order if t in tags] + [t for t in sorted(tags) if t not in order]
        return ",".join(ordered)

    @staticmethod
    def extract_generator(offer: Dict[str, Any], text: str) -> str:
        """Finds if generator is available in building: yes / no / ''."""
        t_lower = text.lower()
        # Check explicit negation first
        if re.search(r"\b(?:без\s+генератора?|генератора?\s+немає|генератор\s+відсутній|генератора?\s+нет)\b", t_lower):
            return "no"
        if re.search(
            r"\b(?:генератор\w*\s+на\s+будинок|будинковий\s+генератор\w*|генератор\w*\s+на\s+ліфт|ліфт\s+працює\s+від\s+генератора|є\s+генератор\w*|підключено\s+генератор\w*|з\s+генератор\w*|генератор\w*\s+на\s+опалення|генератор\w*\s+на\s+воду|резервне\s+живлення\s+будинку)\b",
            t_lower,
        ):
            return "yes"
        return ""

    @staticmethod
    def extract_inverter(text: str) -> str:
        """Finds if inverter / EcoFlow / battery station is in the flat: yes / no / ''."""
        t_lower = text.lower()
        # Check explicit negation first
        if re.search(r"\b(?:без\s+інвертор\w*|без\s+инвертор\w*|інвертора?\s+немає|инвертора?\s+нет|без\s+ecoflow|без\s+зарядної\s+станції)\b", t_lower):
            return "no"
        if re.search(
            r"\b(?:інвертор|інвектор|инвертор|ecoflow|екофлоу|bluetti|блюетті|дбж|ups|акумулятор\s+(?:у|в)\s+квартирі|зарядна\s+станція|резервне\s+живлення\s+(?:в|у)\s+квартирі)\b",
            t_lower,
        ):
            if re.search(r"\bбез\s+(?:будь-яких\s+)?(?:інвертор\w*|инвертор\w*)", t_lower):
                return "no"
            return "yes"
        return ""

    @staticmethod
    def extract_pets(offer: Dict[str, Any], text: str) -> str:
        """Determines if pets are allowed: yes / no / ''."""
        # Check explicit positive statement in title or URL first
        # (landlords frequently state 'mozhna z tvarinoyu' in title while mistakenly ticking 'no' in OLX parameters)
        title_lower = offer.get("title", "").lower()
        url = offer.get("url", "").lower()
        if re.search(r"\b(?:можна\s+з\s+(?:маленькою\s+)?тварин\w*|дозволено\s+з\s+тварин\w*|pet\s*[-]?friendly)\b", title_lower) or "mozhna-z-tvarin" in url or "z-malenkoyu-tvarinoyu" in url:
            if "bez-tvarin" not in url and not re.search(r"\bбез\s+тварин\w*\b", title_lower):
                return "yes"

        params = offer.get("params", [])
        for p in params:
            if p.get("key") == "pets":
                val = p.get("value")
                if isinstance(val, dict):
                    k = val.get("key")
                else:
                    k = val

                if isinstance(k, list):
                    if any(isinstance(x, str) and (x.startswith("yes") or x in ("cats", "dogs", "small_dog")) for x in k):
                        return "yes"
                    if any(isinstance(x, str) and x == "no" for x in k):
                        return "no"
                elif isinstance(k, str):
                    if k.startswith("yes") or k in ("cats", "dogs", "small_dog"):
                        return "yes"
                    if k == "no":
                        return "no"

        if "tvarin" in url or "pet" in url:
            if "bez-tvarin" in url:
                return "no"
            if "z-tvarin" in url or "mozhna-z-tvarin" in url:
                return "yes"

        t_lower = text.lower()
        if re.search(r"\b(?:можна\s+з\s+тварин\w*|дозволено\s+з\s+тварин\w*|з\s+тварин\w*|pet\s*[-]?friendly|дог\s*[-]?френдлі|можна\s+з\s+котик\w*|можна\s+з\s+собак\w*|тварини\s+обговорюються|можно\s+с\s+животн\w*|с\s+питомц\w*)\b", t_lower):
            return "yes"
        if re.search(r"\b(?:без\s+тварин\w*|строго\s+без\s+тварин\w*|категорично\s+без\s+тварин\w*|тварини\s+ні|не\s+можна\s+з\s+тварин\w*|без\s+котів|без\s+собак|без\s+животн\w*)\b", t_lower):
            return "no"

        return ""

    @staticmethod
    def extract_kids(text: str) -> str:
        """Determines if kids are allowed: yes / no / ''."""
        t_lower = text.lower()
        if re.search(r"\b(?:можна\s+з\s+дітьми|дозволено\s+з\s+дітьми|з\s+дитиною|сім[\'’]я\s+з\s+дітьми)\b", t_lower):
            return "yes"
        if re.search(r"\b(?:без\s+дітей|строго\s+без\s+дітей|не\s+можна\s+з\s+дітьми|тільки\s+для\s+одної\s+людини\s+або\s+пари\s+без\s+дітей)\b", t_lower):
            return "no"
        return ""

    @staticmethod
    def extract_utilities(text: str) -> str:
        """Determines if utilities are included in rent: yes / no / ''."""
        t_lower = text.lower()
        if re.search(r"\b(?:комунальні\s+(?:включено|входять|в\s+ціні)|всі\s+платежі\s+включено|без\s+доплат\s+за\s+комунальні)\b", t_lower):
            return "yes"
        if re.search(
            r"\b(?:комунальні\s+(?:окремо|оплачуються\s+окремо|за\s+лічильниками)|\+\s*комунальні|\+\s*к/?п|оплата\s+комунальних|лічильники\s+окремо|\+\s*лічильники|окремо\s+за\s+лічильниками)\b",
            t_lower,
        ):
            return "no"
        return ""

    def normalize(self, offer: Dict[str, Any], seen_date: Optional[str] = None) -> Dict[str, Any]:
        """Normalizes an offer dict into the 23 residential fields."""
        title = offer.get("title") or ""
        raw_desc = offer.get("description") or ""
        desc = strip_html(raw_desc)
        combined_text = f"{title}\n{desc}"

        # Canonical date YYYY-MM-DD
        date_str = seen_date or datetime.now().strftime("%Y-%m-%d")

        # Address parsing
        loc = offer.get("location") or {}
        district_hint = ""
        if isinstance(loc, dict):
            dist_obj = loc.get("district")
            if isinstance(dist_obj, dict):
                district_hint = dist_obj.get("name") or ""

        parser = get_address_parser()
        street, house, district = parser.parse(title, desc, district_hint, fallback_jk=True)

        # Price and currency
        price, currency = self.extract_price_and_currency(offer, combined_text)
        seller = self.extract_seller(offer, combined_text)

        # Parameters
        params = offer.get("params", [])
        rooms = self.extract_rooms(offer, combined_text)

        area_total_val = self.get_param_value(params, "total_area")
        area_total = clean_number_str(area_total_val)
        if not area_total:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)", combined_text)
            if m:
                area_total = clean_number_str(m.group(1))

        area_kitchen_val = self.get_param_value(params, "kitchen_area")
        area_kitchen = clean_number_str(area_kitchen_val)
        if not area_kitchen:
            m = re.search(r"(?:кухн[яіе]|кухонна\s+площа)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)?", combined_text, re.IGNORECASE)
            if m:
                area_kitchen = clean_number_str(m.group(1))

        # Floor and Floors total
        floor_val = self.get_param_value(params, "floor")
        floor_lbl = self.get_param_label(params, "floor") or ""
        if "цоколь" in floor_lbl.lower():
            floor = "0"
        elif "підвал" in floor_lbl.lower():
            floor = "-1"
        else:
            floor = clean_int_str(floor_val)

        if not floor:
            m = re.search(r"(\d+)\s*/\s*\d+\s*пов", combined_text, re.IGNORECASE)
            if m:
                floor = m.group(1)

        floors_total_val = self.get_param_value(params, "total_floors")
        floors_total = clean_int_str(floors_total_val)
        if not floors_total:
            m = re.search(r"\d+\s*/\s*(\d+)\s*пов", combined_text, re.IGNORECASE)
            if m:
                floors_total = m.group(1)

        building_type = self.extract_building_type(offer, combined_text)
        renovation = self.extract_renovation(offer, combined_text)
        furniture = self.extract_furniture(offer, combined_text)
        appliances = self.extract_appliances(offer, combined_text)

        generator_in_building = self.extract_generator(offer, combined_text)
        inverter_in_flat = self.extract_inverter(combined_text)
        pets_allowed = self.extract_pets(offer, combined_text)
        kids_allowed = self.extract_kids(combined_text)
        commission_pct = self.extract_commission_pct(offer, seller, combined_text)
        utilities_included = self.extract_utilities(combined_text)

        # Canonical clean URL
        url = offer.get("url", "")
        if "?" in url:
            url = url.split("?")[0]

        return {
            "url": url,
            "seen_date": date_str,
            "price": price,
            "currency": currency,
            "seller": seller,
            "street": street,
            "house": house,
            "district": district,
            "rooms": rooms,
            "area_total": area_total,
            "area_kitchen": area_kitchen,
            "floor": floor,
            "floors_total": floors_total,
            "building_type": building_type,
            "renovation": renovation,
            "furniture": furniture,
            "appliances": appliances,
            "generator_in_building": generator_in_building,
            "inverter_in_flat": inverter_in_flat,
            "pets_allowed": pets_allowed,
            "kids_allowed": kids_allowed,
            "commission_pct": commission_pct,
            "utilities_included": utilities_included,
        }


class CommercialNormalizer(BaseNormalizer):
    """
    Normalizes commercial rent offers into 35 canonical fields with strict conditional clearing.
    """

    @staticmethod
    def detect_property_type(offer: Dict[str, Any], text: str) -> str:
        """
        Determines property_type strictly in:
        office, retail, food, warehouse, production, free_purpose, medical_beauty, education, hotel.
        """
        cat_id = offer.get("category", {}).get("id")
        if cat_id in OLX_COMMERCIAL_SUBCATEGORY_MAP:
            detected = OLX_COMMERCIAL_SUBCATEGORY_MAP[cat_id]
            # If subcategory is generic free_purpose, try refining via text
            if detected != "free_purpose":
                return detected

        t_lower = text.lower()
        if re.search(r"\b(?:ресторан[ау]?|кафе|кав\'?ярн[яіе]|кофейн[яе]|піцері[яі]|пиццери[яи]|бар[у]?|пекарн[яіе]|їдальн[яі]|общепит)\b", t_lower):
            return "food"
        if re.search(r"\b(?:салон\s*краси|барбершоп|стоматологі[яі]|косметологі[яі]|кабінет\s*краси|манікюр|клінік[аи])\b", t_lower):
            return "medical_beauty"
        if re.search(r"\b(?:готель|отель|хостел|апарт[- ]готель)\b", t_lower):
            return "hotel"
        if re.search(r"\b(?:школ[аи]|дитячий\s+садок|садочок|навчальн[ийого]|курс[иів]|тренінгов[ийого]|студі[яі]\s+танців|спортзал|фітнес)\b", t_lower):
            return "education"
        if re.search(r"\b(?:склад[ау]?|ангар[ау]?|сховищ[еа]|бокс\s+для\s+зберігання|складськ[еийого]|логістичн[ийого])\b", t_lower):
            return "warehouse"
        if re.search(r"\b(?:виробництв[оа]|цех[у]?|виробнич[еийого]|майстерн[яі]|сто|автомийк[аи])\b", t_lower):
            return "production"
        if re.search(r"\b(?:магазин[ау]?|бутик[ау]?|шоурум[ау]?|торгов[аоюе]\s*площ|торговельн[аоюе]|маф|павільйон[ау]?|ритейл[у]?)\b", t_lower):
            return "retail"
        if re.search(r"\b(?:офіс[ау]?|офис[ау]?|коворкінг[у]?|коворкинг[а]?|open\s*space|кабінет[ау]?|бц)\b", t_lower):
            return "office"
        if re.search(r"\b(?:вільного\s+призначення|свободного\s+назначения|нежитлов[еийого]\s+приміщення)\b", t_lower):
            return "free_purpose"

        return "free_purpose"

    @staticmethod
    def extract_price_basis(price: str, currency: str, area_total: str, text: str) -> str:
        """
        Determines price_basis: 'month_per_m2' vs 'month_total'.
        Rules:
        - If price is a high total amount (>= 150 USD/EUR or >= 3500 UAH), it represents 'month_total',
          even if the description mentions m2 rates (e.g. '400 000 грн (625 грн/м2)').
        - If price is low (< 150 USD/EUR or < 3500 UAH) when area >= 20, it is 'month_per_m2'.
        - If text explicitly mentions per m2 and price is within plausible per-m2 range -> 'month_per_m2'.
        - Otherwise -> 'month_total'.
        """
        try:
            p_val = float(price) if price else 0.0
            a_val = float(area_total) if area_total else 0.0
        except (ValueError, TypeError):
            p_val, a_val = 0.0, 0.0

        if (currency in ("USD", "EUR") and p_val >= 150.0) or (currency == "UAH" and p_val >= 3500.0):
            return "month_total"

        if a_val >= 20.0 and p_val > 0:
            if currency in ("USD", "EUR") and p_val < 150.0:
                return "month_per_m2"
            if currency == "UAH" and p_val < 3500.0:
                return "month_per_m2"

        t_lower = text.lower()
        if re.search(r"(?:за\s*м[²2]|за\s*кв\.?\s*м|\$/м[²2]|грн/м[²2]|за\s*метр|м[²2]/міс|\$/кв\.?\s*м|грн\s+за\s+м[²2]|грн\s*/\s*м2)", t_lower):
            return "month_per_m2"

        return "month_total"

    @staticmethod
    def extract_vat(text: str) -> str:
        """Determines VAT status: yes / no / ''."""
        t_lower = text.lower()
        if re.search(r"\b(?:з\s+пдв|включаючи\s+пдв|включно\s+з\s+пдв|з\s+ндс|включая\s+ндс|в\s+т\.?ч\.?\s*(?:пдв|ндс))\b", t_lower):
            return "yes"
        if re.search(
            r"\b(?:без\s+пдв|\+\s*пдв|плюс\s+пдв|без\s+ндс|\+\s*ндс|плюс\s+ндс|пдв\s+окремо|пдв\s+не\s+входить|пдв\s+немає|немає\s+пдв|пдв\s+відсутній|пдв\s+не\s+включено)\b",
            t_lower,
        ):
            return "no"
        return ""

    @staticmethod
    def extract_opex(text: str) -> Tuple[str, str]:
        """
        Extracts OPEX status (yes / no / '') and OPEX rate in UAH/m2.
        """
        t_lower = text.lower()
        opex_inc = ""
        if re.search(r"\b(?:з\s+опекс|опекс\s+включено|opex\s+включено|експлуатаційні\s+включено|експлуатаційні\s+входять|з\s+експлуатаційними|включаючи\s+експлуатаційні)\b", t_lower):
            opex_inc = "yes"
        elif re.search(r"\b(?:\+\s*опекс|\+\s*opex|плюс\s+опекс|\+\s*експлуатаційні|експлуатаційні\s+окремо|без\s+експлуатаційних|експлуатаційні\s+окремо\s+не\s+включені)\b", t_lower):
            opex_inc = "no"

        opex_rate = ""
        m = re.search(r"(?:opex|опекс|експлуатаційні|эксплуатационные)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:грн|uah)?\s*(?:за\s*м[²2]|/м[²2]|/кв\.?\s*м)", t_lower)
        if m:
            opex_rate = clean_number_str(m.group(1))

        return opex_inc, opex_rate

    @staticmethod
    def extract_business_center_name(text: str) -> str:
        """Extracts Business Center name (e.g. 'Gulliver', 'Парус')."""
        generic_words = {
            "є", "на", "в", "у", "з", "по", "біля", "від", "до", "для", "та", "і", "або",
            "новий", "новому", "новій", "нового", "нові", "сучасний", "сучасному", "сучасна", "сучасні",
            "класу", "клас", "оренда", "аренда", "офіс", "офис", "офісів", "офісу", "офиса",
            "кабинет", "кабінет", "приміщення", "поверх", "ліфт", "ліфти", "паркінг", "парковка",
            "поштомат", "додатково", "комунальні", "квартира", "метро", "вул", "вулиця",
            "київ", "киев", "kyiv", "kiev",
            "голосіївський", "голосеевский", "печерський", "печерский", "шевченківський", "шевченковский",
            "подільський", "подольский", "оболонський", "оболонский", "дарницький", "дарницкий",
            "дніпровський", "днепровский", "деснянський", "деснянский", "солом'янський", "соломенский",
            "святошинський", "святошинский",
            "голосієво", "голосеево", "позняки", "осокорки", "оболонь", "поділ", "подол", "печерськ", "печерск",
            "центр", "район", "р-н", "пропонується", "предлагается", "здається", "сдается",
            "власник", "собственник", "без", "комісії", "комиссии"
        }

        disallowed_starters = {
            "біля", "на", "в", "у", "з", "по", "від", "до", "для", "під", "над", "при",
            "метро", "ст", "станція", "станції", "м",
            "вулиця", "вул", "просп", "проспект", "бульв", "бульвар", "пров", "провулок",
            "площа", "шосе", "узвіз", "тупик", "алея",
            "клас", "класу", "class",
            "район", "р-н", "поверх", "поверсі",
            "оренда", "аренда", "офіс", "офис", "приміщення",
            "новий", "сучасний"
        }

        def _is_valid_bc_name(name_str: str) -> bool:
            tokens = [t.lower() for t in re.findall(r"\w+", name_str) if not t.isdigit()]
            if not tokens:
                return False
            if tokens[0] in disallowed_starters:
                return False
            non_generic = [t for t in tokens if t not in generic_words and len(t) >= 2]
            if not non_generic:
                return False
            return True

        def _clean_bc_name(bc_name: str) -> str:
            words = bc_name.split()
            if len(words) > 1 and re.match(r"^\d+[а-яА-Яa-zA-Z]?$", words[-1]):
                tail_phrase = words[-2].lower()
                parser = get_address_parser()
                if tail_phrase in parser.search_index:
                    leading = " ".join(words[:-2]).strip()
                    return leading
                if len(words) == 2 and words[0].lower() in parser.search_index:
                    return ""
            parser = get_address_parser()
            if bc_name.lower() in parser.search_index:
                return ""
            return bc_name

        # 1. Quoted name after BC indicator (most accurate)
        m_q = re.search(r'(?:БЦ|бізнес[- ]центр[аіе]?|бизнес[- ]центр[ае]?|ТОЦ|ТРЦ)\s*["«„]([A-Za-zА-Яа-яІіЇїЄє0-9\s\.\-_]{2,40}?)["»“]', text)
        if m_q:
            bc = _clean_bc_name(m_q.group(1).strip())
            if bc and not bc.isdigit() and _is_valid_bc_name(bc):
                return bc

        # 2. Unquoted proper noun on the same line (no newlines) starting with capital letter
        m_u = re.search(r'(?:БЦ|бізнес[- ]центр[аіе]?|бизнес[- ]центр[ае]?|ТОЦ|ТРЦ)[ \t]+([A-ZА-ЯІЇЄ][A-Za-zА-Яа-яІіЇїЄє0-9\-_]*(?:[ \t]+[A-ZА-ЯІЇЄ0-9][A-Za-zА-Яа-яІіЇїЄє0-9\-_]*){0,2})', text)
        if m_u:
            raw_bc = m_u.group(1).strip()
            raw_bc = re.sub(r"[,\.;:]+$", "", raw_bc).strip()
            tail_ctx = text[m_u.end():min(len(text), m_u.end() + 20)].lower()
            if not re.match(r"^\s*(?:район[уі]?|р-н[уі]?|метро)", tail_ctx):
                bc = _clean_bc_name(raw_bc)
                if bc and 2 <= len(bc) <= 40 and not bc.isdigit() and _is_valid_bc_name(bc):
                    return bc

        return ""

    @staticmethod
    def extract_building_class(offer: Dict[str, Any], text: str) -> str:
        """Extracts building class: A, B, C."""
        params = offer.get("params", [])
        raw = BaseNormalizer.get_param_value(params, "office_class")
        if raw:
            s = str(raw).lower()
            if "category_a" in s or "class_a" in s:
                return "A"
            if "category_b" in s or "class_b" in s:
                return "B"
            if "category_c" in s or "class_c" in s:
                return "C"

        m = re.search(r"(?:клас[уа]?|class)\s*[:\-]?\s*[\"«]?([ABCАВС])[\"»]?", text, re.IGNORECASE)
        if m:
            c = m.group(1).upper()
            cyr_to_lat = {"А": "A", "В": "B", "С": "C"}
            return cyr_to_lat.get(c, c)
        return ""

    @staticmethod
    def extract_condition(offer: Dict[str, Any], text: str) -> str:
        """
        Maps condition strictly to: shell_and_core, white_box, ready, needs_repair.
        """
        params = offer.get("params", [])
        rep = BaseNormalizer.get_param_value(params, "repair")
        is_rep = BaseNormalizer.get_param_value(params, "is_repaired")

        if is_rep == "yes":
            return "ready"

        if rep:
            s = str(rep)
            if s in ("1", "2"):
                return "ready"
            if s == "5":
                return "shell_and_core"
            if s == "6":
                return "white_box"
            if s == "3":
                return "ready"
            if s == "4":
                return "ready"

        t_lower = text.lower()
        if re.search(r"\b(?:shell\s*&?\s*core|після\s+будівельників|від\s+забудовника|без\s+оздоблення|чернов[ийого])\b", t_lower):
            return "shell_and_core"
        if re.search(r"\b(?:white\s*box|вайт\s*бокс|під\s+чистове|під\s+чистову)\b", t_lower):
            return "white_box"
        if re.search(r"\b(?:з\s+ремонтом|готов[еийого]\s+до\s+роботи|офісний\s+ремонт|євроремонт|дизайнерськ[ийого]|хороший\s+стан)\b", t_lower):
            return "ready"
        if re.search(r"\b(?:потребує\s+ремонту|під\s+ремонт|старий\s+ремонт)\b", t_lower):
            return "needs_repair"

        return ""

    @staticmethod
    def extract_furniture(offer: Dict[str, Any], text: str) -> str:
        """Maps furniture: none, partial, full."""
        params = offer.get("params", [])
        raw = BaseNormalizer.get_param_value(params, "furnishing") or BaseNormalizer.get_param_value(params, "furniture")
        if raw == "yes":
            return "full"
        if raw == "no":
            return "none"
        if raw == "partial":
            return "partial"

        t_lower = text.lower()
        if re.search(r"\b(?:без\s+меблів|без\s+мебели|не\s+мебльован[еий])\b", t_lower):
            return "none"
        if re.search(r"\b(?:частково\s+з\s+меблями|частково\s+мебльован[еий]|меблі\s+на\s+кухні)\b", t_lower):
            return "partial"
        if re.search(r"\b(?:з\s+меблями|мебльован[еий]|меблирован[оый]|повністю\s+мебльован[еий])\b", t_lower):
            return "full"

        return ""

    @staticmethod
    def extract_generator(text: str) -> str:
        """Finds if generator is in commercial building: yes / no / ''."""
        t_lower = text.lower()
        if re.search(r"\b(?:генератор|є\s+генератор|працює\s+генератор|генератор\s+у\s+будівлі|генератор\s+на\s+бц|безперебійне\s+живлення)\b", t_lower):
            return "yes"
        if re.search(r"\b(?:без\s+генератора|генератора\s+немає)\b", t_lower):
            return "no"
        return ""

    @staticmethod
    def extract_shelter(text: str) -> str:
        """Finds if shelter is in commercial building: yes / no / ''."""
        t_lower = text.lower()
        if re.search(r"\b(?:укриття|бомбосховищ[еа]|сховище|укрытие|власне\s+укриття|паркінг[- ]укриття)\b", t_lower):
            return "yes"
        if re.search(r"\b(?:без\s+укриття|укриття\s+відсутнє)\b", t_lower):
            return "no"
        return ""

    def normalize(self, offer: Dict[str, Any], seen_date: Optional[str] = None) -> Dict[str, Any]:
        """Normalizes an offer dict into the 35 commercial fields."""
        title = offer.get("title") or ""
        raw_desc = offer.get("description") or ""
        desc = strip_html(raw_desc)
        combined_text = f"{title}\n{desc}"

        date_str = seen_date or datetime.now().strftime("%Y-%m-%d")

        # Address parsing
        loc = offer.get("location") or {}
        district_hint = ""
        if isinstance(loc, dict):
            dist_obj = loc.get("district")
            if isinstance(dist_obj, dict):
                district_hint = dist_obj.get("name") or ""

        parser = get_address_parser()
        street, house, district = parser.parse(title, desc, district_hint, fallback_jk=True)

        property_type = self.detect_property_type(offer, combined_text)
        price, currency = self.extract_price_and_currency(offer, combined_text)
        seller = self.extract_seller(offer, combined_text)

        # Total area
        params = offer.get("params", [])
        area_total_val = self.get_param_value(params, "total_area")
        area_total = clean_number_str(area_total_val)
        if not area_total:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)", combined_text)
            if m:
                area_total = clean_number_str(m.group(1))

        price_basis = self.extract_price_basis(price, currency, area_total, combined_text)
        vat_included = self.extract_vat(combined_text)
        opex_included, opex_uah_m2 = self.extract_opex(combined_text)

        # Floor and floors total
        floor_val = self.get_param_value(params, "floor")
        comm_loc = self.get_param_value(params, "comm_re_location")
        t_lower = combined_text.lower()
        if comm_loc == "basement" or "підвал" in t_lower:
            floor = "-1"
        elif "цоколь" in t_lower:
            floor = "0"
        else:
            floor = clean_int_str(floor_val)

        floors_total_val = self.get_param_value(params, "total_floors")
        floors_total = clean_int_str(floors_total_val)

        # General features
        condition = self.extract_condition(offer, combined_text)
        furniture = self.extract_furniture(offer, combined_text)

        parking_spaces = ""
        m_park = re.search(
            r"(?:паркомісць|паркомест|парковка\s+на|паркінг\s+на)\s*[:\-]?\s*(\d+)"
            r"|(?:паркінг|парковка)\s*[:\-]\s*(\d+)"
            r"|(?:паркінг|парковка)\s+(\d+)\s*(?:авто|машин\w*|місц\w*|паркомісц\w*)"
            r"|(\d+)\s*(?:паркомісць|паркомісця|паркомест|паркоместа|паркувальних\s+місць)",
            t_lower,
        )
        if m_park:
            matched_val = next((g for g in m_park.groups() if g is not None), None)
            if matched_val and 0 < int(matched_val) < 1000:
                tail_after_match = t_lower[m_park.end():m_park.end() + 20]
                if not re.match(r"^\s*(?:грн|uah|usd|eur|\$|€|тис|тисяч|т\.?\s*грн|/міс|/мес)", tail_after_match):
                    parking_spaces = clean_int_str(matched_val)

        generator = self.extract_generator(combined_text)
        shelter_in_building = self.extract_shelter(combined_text)

        min_lease_months = ""
        m_lease = re.search(r"(?:мін(?:імальний)?\s+(?:термін|строк)|мин(?:имальный)?\s+срок|оренда\s+від|аренда\s+от)\s*[:\-]?\s*(\d+)\s*(?:міс|мес)", t_lower)
        if m_lease:
            min_lease_months = clean_int_str(m_lease.group(1))
        elif re.search(r"\b(?:на\s+рік|від\s+1\s*року|від\s+року)\b", t_lower):
            min_lease_months = "12"

        commission_pct = self.extract_commission_pct(offer, seller, combined_text)

        # Type-specific candidates before conditional clearing
        business_center_name = self.extract_business_center_name(combined_text)
        building_class = self.extract_building_class(offer, combined_text)

        first_line = ""
        if re.search(r"\b(?:перш\w*\s+ліні\w*|1[- ]?\w*\s+ліні\w*|перв\w*\s+лини\w*|фасад|фасадн\w*)\b", t_lower):
            first_line = "yes"
        elif re.search(r"\b(?:друг\w*\s+ліні\w*|2[- ]?\w*\s+ліні\w*|втор\w*\s+лини\w*|у\s+дворі|во\s+дворе)\b", t_lower):
            first_line = "no"

        separate_entrance = ""
        if re.search(r"\b(?:окремий\s+вхід|отдельный\s+вход|свій\s+вхід|власний\s+вхід|фасадний\s+вхід)\b", t_lower):
            separate_entrance = "yes"
        elif re.search(r"\b(?:спільний\s+вхід|общий\s+вход|вхід\s+через\s+під\'?їзд)\b", t_lower):
            separate_entrance = "no"

        shop_windows = ""
        if re.search(r"\b(?:вітрин[и]?|витрин[ы]?|вітринні\s+вікна|панорамні\s+вікна|фасадні\s+вікна|панорамне\s+скління)\b", t_lower):
            shop_windows = "yes"

        ceiling_height_m = ""
        m_ceil = re.search(r"(?:висота\s+стел[іь]?|высота\s+потолк[ао]в?|стеля|потолок|h\s*=)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*м", t_lower)
        if m_ceil:
            ceiling_height_m = clean_number_str(m_ceil.group(1))

        power_kw = ""
        m_pow = re.search(r"(?:потужність|мощность|електроенергія|електрика|світло)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:квт|кват|кw|kw)", t_lower)
        if not m_pow:
            m_pow = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:квт|кw|kw)\b", t_lower)
        if m_pow:
            power_kw = clean_number_str(m_pow.group(1))

        ventilation_hood = ""
        if re.search(r"\b(?:витяжк[аи]|вытяжк[аи]|промислова\s+витяжка|вентканал|приточно[- ]витяжна)\b", t_lower):
            ventilation_hood = "yes"

        water_in_rooms = ""
        if re.search(r"\b(?:мокра\s+точка|мокрі\s+точки|мокрые\s+точки|вода\s+в\s+кабінетах|вода\s+в\s+кабинетах|умивальник\s+в\s+кабінеті)\b", t_lower):
            water_in_rooms = "yes"

        loading_dock = ""
        if re.search(r"\b(?:рампа|пандус|доки|dock|вантажна\s+рампа|розвантажувальна\s+рампа|доквеллер)\b", t_lower):
            loading_dock = "yes"

        floor_load_t_m2 = ""
        m_load = re.search(r"(?:навантаження\s+(?:на\s+підлогу)?|нагрузка\s+(?:на\s+пол)?)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:т/?м[²2]|тонн|т)", t_lower)
        if m_load:
            floor_load_t_m2 = clean_number_str(m_load.group(1))

        heated = ""
        if re.search(r"\b(?:холодний\s+склад|неопалюван[а-яА-Я]*|без\s+опалення|неотапливаем[а-яА-Я]*)\b", t_lower):
            heated = "no"
        elif re.search(r"\b(?:опалюван[а-яА-Я]*|отапливаем[а-яА-Я]*|тепл[а-яА-Я]*(?:\s+склад)?|опалення\s+є|з\s+опаленням)\b", t_lower):
            heated = "yes"

        # Canonical clean URL
        url = offer.get("url", "")
        if "?" in url:
            url = url.split("?")[0]

        data = {
            "url": url,
            "seen_date": date_str,
            "property_type": property_type,
            "price": price,
            "currency": currency,
            "price_basis": price_basis,
            "vat_included": vat_included,
            "opex_included": opex_included,
            "opex_uah_m2": opex_uah_m2,
            "seller": seller,
            "street": street,
            "house": house,
            "district": district,
            "business_center_name": business_center_name,
            "building_class": building_class,
            "area_total": area_total,
            "floor": floor,
            "floors_total": floors_total,
            "first_line": first_line,
            "separate_entrance": separate_entrance,
            "shop_windows": shop_windows,
            "condition": condition,
            "furniture": furniture,
            "ceiling_height_m": ceiling_height_m,
            "power_kw": power_kw,
            "parking_spaces": parking_spaces,
            "generator": generator,
            "shelter_in_building": shelter_in_building,
            "ventilation_hood": ventilation_hood,
            "water_in_rooms": water_in_rooms,
            "loading_dock": loading_dock,
            "floor_load_t_m2": floor_load_t_m2,
            "heated": heated,
            "min_lease_months": min_lease_months,
            "commission_pct": commission_pct,
        }

        # Apply strict conditional clearing
        return apply_conditional_clearing(data)


def apply_conditional_clearing(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enforces strict conditional clearing on commercial property fields.
    If property_type does not allow a field, that field is unconditionally cleared to ''.
    """
    p_type = data.get("property_type", "")
    for field, allowed_types in COMMERCIAL_CONDITIONAL_FIELDS.items():
        if p_type not in allowed_types:
            data[field] = ""
    return data


# Convenience functions
_res_normalizer = ResidentialNormalizer()
_comm_normalizer = CommercialNormalizer()


def normalize_residential_offer(offer: Dict[str, Any], seen_date: Optional[str] = None) -> Dict[str, Any]:
    """Top-level helper to normalize residential OLX offers."""
    return _res_normalizer.normalize(offer, seen_date)


def normalize_commercial_offer(offer: Dict[str, Any], seen_date: Optional[str] = None) -> Dict[str, Any]:
    """Top-level helper to normalize commercial OLX offers."""
    return _comm_normalizer.normalize(offer, seen_date)


class RieltorNormalizer:
    """
    Normalizes scraped Rieltor.ua offers (residential and commercial)
    into the canonical 23-column and 35-column datasets.
    """

    def __init__(self, address_parser: Optional[KyivAddressParser] = None):
        self.address_parser = address_parser or get_address_parser()

    def normalize_residential(
        self,
        card_data: Dict[str, Any],
        seen_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = card_data.get("url", "")
        desc = card_data.get("description", "")
        addr_text = card_data.get("address_text", "")
        district_hint = card_data.get("district_hint", "")
        chips = card_data.get("chips", [])

        # Address parsing with JK fallback
        street, house, district = self.address_parser.parse(
            addr_text,
            desc,
            district_hint=district_hint,
            fallback_jk=True,
        )

        # Check chips for explicit JK chip if house is missing and street not yet a JK
        if not house and not street.startswith("ЖК "):
            for chip in chips:
                if "жк" in chip.lower():
                    jk_clean = re.sub(r"^(?:пр[еє]міум\s+|відеотур\s+)", "", chip, flags=re.IGNORECASE).strip()
                    if not jk_clean.lower().startswith("жк"):
                        street = f"ЖК {jk_clean}"
                    else:
                        street = jk_clean
                    break

        price_val = card_data.get("price")
        price = str(price_val) if price_val is not None else ""
        currency = card_data.get("currency") or "UAH"

        seller_raw = (card_data.get("seller") or card_data.get("author") or "").lower()
        if "власник" in seller_raw or "без комісії від власника" in desc.lower():
            seller = "owner"
        elif "агент" in seller_raw or "рієлтор" in seller_raw or card_data.get("agency"):
            seller = "agency"
        else:
            seller = ResidentialNormalizer.extract_seller({}, desc)

        rooms = clean_int_str(card_data.get("rooms"))
        area_total = clean_number_str(card_data.get("area_total"))
        area_kitchen = clean_number_str(card_data.get("area_kitchen"))
        floor = clean_int_str(card_data.get("floor"))
        floors_total = clean_int_str(card_data.get("floors_total"))

        # Building type mapping by construction year or text
        built_year = card_data.get("built_year")
        if built_year:
            try:
                y = int(built_year)
                if y >= 2001:
                    building_type = "new_build_2010s_plus"
                elif 1970 <= y <= 2000:
                    building_type = "cheshka" if re.search(r"\bчеськ", desc.lower()) else "special_project"
                elif 1955 <= y < 1970:
                    building_type = "khrushchyovka"
                elif 1917 <= y < 1955:
                    building_type = "stalinka"
                else:
                    building_type = "tsarist"
            except (ValueError, TypeError):
                building_type = ResidentialNormalizer.extract_building_type({}, desc)
        else:
            building_type = ResidentialNormalizer.extract_building_type({}, desc)

        renovation = ResidentialNormalizer.extract_renovation({}, desc)
        furniture = ResidentialNormalizer.extract_furniture({}, desc)
        appliances = ResidentialNormalizer.extract_appliances({}, desc)
        generator_in_building = ResidentialNormalizer.extract_generator({}, desc)
        inverter_in_flat = ResidentialNormalizer.extract_inverter(desc)
        pets_allowed = ResidentialNormalizer.extract_pets({}, desc)
        kids_allowed = ResidentialNormalizer.extract_kids(desc)

        # Commission
        if any("без комісії" in c.lower() or "без комиссии" in c.lower() for c in chips) or re.search(r"\bбез\s+комісі[їяі]|без\s+комисси[ие]\b", desc.lower()):
            commission_pct = "0"
        else:
            commission_pct = ResidentialNormalizer.extract_commission_pct({}, seller, desc)

        utilities_included = ResidentialNormalizer.extract_utilities(desc)

        effective_date = seen_date or card_data.get("seen_date") or datetime.now().strftime("%Y-%m-%d")

        return {
            "url": url,
            "source": "rieltor",
            "seen_date": effective_date,
            "price": price,
            "currency": currency,
            "seller": seller,
            "street": street,
            "house": house,
            "district": district,
            "rooms": rooms,
            "area_total": area_total,
            "area_kitchen": area_kitchen,
            "floor": floor,
            "floors_total": floors_total,
            "building_type": building_type,
            "renovation": renovation,
            "furniture": furniture,
            "appliances": appliances,
            "generator_in_building": generator_in_building,
            "inverter_in_flat": inverter_in_flat,
            "pets_allowed": pets_allowed,
            "kids_allowed": kids_allowed,
            "commission_pct": commission_pct,
            "utilities_included": utilities_included,
        }

    def normalize_commercial(
        self,
        card_data: Dict[str, Any],
        seen_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = card_data.get("url", "")
        desc = card_data.get("description", "")
        addr_text = card_data.get("address_text", "")
        district_hint = card_data.get("district_hint", "")
        prop_type_raw = card_data.get("property_type_raw", "")
        chips = card_data.get("chips", [])

        # Address parsing with JK fallback
        street, house, district = self.address_parser.parse(
            addr_text,
            desc,
            district_hint=district_hint,
            fallback_jk=True,
        )

        # Property type
        combined_prop_text = f"{prop_type_raw} {desc}".strip()
        property_type = CommercialNormalizer.detect_property_type({"category": {}}, combined_prop_text)

        price_val = card_data.get("price")
        price = str(price_val) if price_val is not None else ""
        currency = card_data.get("currency") or "UAH"

        area_total = clean_number_str(card_data.get("area_total"))
        if not area_total:
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)", desc)
            if m:
                area_total = clean_number_str(m.group(1))

        price_basis = CommercialNormalizer.extract_price_basis(price, currency, area_total, desc)
        vat_included = CommercialNormalizer.extract_vat(desc)
        opex_included, opex_uah_m2 = CommercialNormalizer.extract_opex(desc)

        seller_raw = (card_data.get("seller") or card_data.get("author") or "").lower()
        if "власник" in seller_raw or "без комісії від власника" in desc.lower():
            seller = "owner"
        elif "агент" in seller_raw or "рієлтор" in seller_raw or card_data.get("agency"):
            seller = "agency"
        else:
            seller = CommercialNormalizer.extract_seller({}, desc)

        business_center_name = CommercialNormalizer.extract_business_center_name(desc)
        building_class = CommercialNormalizer.extract_building_class({}, desc)

        floor = clean_int_str(card_data.get("floor"))
        floors_total = clean_int_str(card_data.get("floors_total"))

        t_lower = desc.lower()
        first_line = ""
        if re.search(r"\b(?:перш\w*\s+ліні\w*|1[- ]?\w*\s+ліні\w*|перв\w*\s+лини\w*|фасад|фасадн\w*)\b", t_lower):
            first_line = "yes"
        elif re.search(r"\b(?:друг\w*\s+ліні\w*|2[- ]?\w*\s+ліні\w*|втор\w*\s+лини\w*|у\s+дворі|во\s+дворе)\b", t_lower):
            first_line = "no"

        separate_entrance = ""
        if re.search(r"\b(?:окремий\s+вхід|отдельный\s+вход|свій\s+вхід|власний\s+вхід|фасадний\s+вхід)\b", t_lower):
            separate_entrance = "yes"
        elif re.search(r"\b(?:спільний\s+вхід|общий\s+вход|вхід\s+через\s+під\'?їзд)\b", t_lower):
            separate_entrance = "no"

        shop_windows = ""
        if re.search(r"\b(?:вітрин[и]?|витрин[ы]?|вітринні\s+вікна|панорамні\s+вікна|фасадні\s+вікна|панорамне\s+скління)\b", t_lower):
            shop_windows = "yes"

        condition = CommercialNormalizer.extract_condition({}, desc)
        furniture = CommercialNormalizer.extract_furniture({}, desc)

        ceiling_height_m = ""
        m_ceil = re.search(r"(?:висота\s+стел[іь]?|высота\s+потолк[ао]в?|стеля|потолок|h\s*=)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*м", t_lower)
        if m_ceil:
            ceiling_height_m = clean_number_str(m_ceil.group(1))

        power_kw = ""
        m_pow = re.search(r"(?:потужність|мощность|електроенергія|електрика|світло)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:квт|кват|кw|kw)", t_lower)
        if not m_pow:
            m_pow = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:квт|кw|kw)\b", t_lower)
        if m_pow:
            power_kw = clean_number_str(m_pow.group(1))

        parking_spaces = ""
        m_park = re.search(
            r"(?:паркомісць|паркомест|парковка\s+на|паркінг\s+на)\s*[:\-]?\s*(\d+)"
            r"|(?:паркінг|парковка)\s*[:\-]\s*(\d+)"
            r"|(?:паркінг|парковка)\s+(\d+)\s*(?:авто|машин\w*|місц\w*|паркомісц\w*)"
            r"|(\d+)\s*(?:паркомісць|паркомісця|паркомест|паркоместа|паркувальних\s+місць)",
            t_lower,
        )
        if m_park:
            matched_val = next((g for g in m_park.groups() if g is not None), None)
            if matched_val and 0 < int(matched_val) < 1000:
                tail_after_match = t_lower[m_park.end():m_park.end() + 20]
                if not re.match(r"^\s*(?:грн|uah|usd|eur|\$|€|тис|тисяч|т\.?\s*грн|/міс|/мес)", tail_after_match):
                    parking_spaces = clean_int_str(matched_val)

        generator = CommercialNormalizer.extract_generator(desc)
        shelter_in_building = CommercialNormalizer.extract_shelter(desc)

        ventilation_hood = ""
        if re.search(r"\b(?:витяжк[аи]|вытяжк[аи]|промислова\s+витяжка|вентканал|приточно[- ]витяжна)\b", t_lower):
            ventilation_hood = "yes"

        water_in_rooms = ""
        if re.search(r"\b(?:мокра\s+точка|мокрі\s+точки|мокрые\s+точки|вода\s+в\s+кабінетах|вода\s+в\s+кабинетах|умивальник\s+в\s+кабінеті)\b", t_lower):
            water_in_rooms = "yes"

        loading_dock = ""
        if re.search(r"\b(?:рампа|пандус|доки|dock|вантажна\s+рампа|розвантажувальна\s+рампа|доквеллер)\b", t_lower):
            loading_dock = "yes"

        floor_load_t_m2 = ""
        m_load = re.search(r"(?:навантаження\s+(?:на\s+підлогу)?|нагрузка\s+(?:на\s+пол)?)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*(?:т/?м[²2]|тонн|т)", t_lower)
        if m_load:
            floor_load_t_m2 = clean_number_str(m_load.group(1))

        heated = ""
        if re.search(r"\b(?:холодний\s+склад|неопалюван[а-яА-Я]*|без\s+опалення|неотапливаем[а-яА-Я]*)\b", t_lower):
            heated = "no"
        elif re.search(r"\b(?:опалюван[а-яА-Я]*|отапливаем[а-яА-Я]*|тепл[а-яА-Я]*(?:\s+склад)?|опалення\s+є|з\s+опаленням)\b", t_lower):
            heated = "yes"

        min_lease_months = ""
        m_lease = re.search(r"(?:мін(?:імальний)?\s+(?:термін|строк)|мин(?:имальный)?\s+срок|оренда\s+від|аренда\s+от)\s*[:\-]?\s*(\d+)\s*(?:міс|мес)", t_lower)
        if m_lease:
            min_lease_months = clean_int_str(m_lease.group(1))
        elif re.search(r"\b(?:на\s+рік|від\s+1\s*року|від\s+року)\b", t_lower):
            min_lease_months = "12"

        if any("без комісії" in c.lower() or "без комиссии" in c.lower() for c in chips) or re.search(r"\bбез\s+комісі[їяі]|без\s+комисси[ие]\b", t_lower):
            commission_pct = "0"
        else:
            commission_pct = CommercialNormalizer.extract_commission_pct({}, seller, desc)

        effective_date = seen_date or card_data.get("seen_date") or datetime.now().strftime("%Y-%m-%d")

        data = {
            "url": url,
            "source": "rieltor",
            "seen_date": effective_date,
            "property_type": property_type,
            "price": price,
            "currency": currency,
            "price_basis": price_basis,
            "vat_included": vat_included,
            "opex_included": opex_included,
            "opex_uah_m2": opex_uah_m2,
            "seller": seller,
            "street": street,
            "house": house,
            "district": district,
            "business_center_name": business_center_name,
            "building_class": building_class,
            "area_total": area_total,
            "floor": floor,
            "floors_total": floors_total,
            "first_line": first_line,
            "separate_entrance": separate_entrance,
            "shop_windows": shop_windows,
            "condition": condition,
            "furniture": furniture,
            "ceiling_height_m": ceiling_height_m,
            "power_kw": power_kw,
            "parking_spaces": parking_spaces,
            "generator": generator,
            "shelter_in_building": shelter_in_building,
            "ventilation_hood": ventilation_hood,
            "water_in_rooms": water_in_rooms,
            "loading_dock": loading_dock,
            "floor_load_t_m2": floor_load_t_m2,
            "heated": heated,
            "min_lease_months": min_lease_months,
            "commission_pct": commission_pct,
        }
        return apply_conditional_clearing(data)


_rieltor_normalizer = RieltorNormalizer()


def normalize_rieltor_residential_offer(card_data: Dict[str, Any], seen_date: Optional[str] = None) -> Dict[str, Any]:
    """Top-level helper to normalize residential Rieltor offers."""
    return _rieltor_normalizer.normalize_residential(card_data, seen_date)


def normalize_rieltor_commercial_offer(card_data: Dict[str, Any], seen_date: Optional[str] = None) -> Dict[str, Any]:
    """Top-level helper to normalize commercial Rieltor offers."""
    return _rieltor_normalizer.normalize_commercial(card_data, seen_date)
