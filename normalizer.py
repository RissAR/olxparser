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
    "верхогляда": "вулиця Андрія Верхогляда",
    "андрія верхогляда": "вулиця Андрія Верхогляда",
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
    "патриса лумумбы": "вулиця Іоанна Павла ІІ",
    "патріса лумумби": "вулиця Іоанна Павла ІІ",
    "іоанна павла": "вулиця Іоанна Павла ІІ",
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
    "рильського": "вулиця Рильського Максима",
    "максима рильського": "вулиця Рильського Максима",
    "богатирська": "вулиця Богатирська",
    "коновальця": "вулиця Євгена Коновальця",
    "євгена коновальця": "вулиця Євгена Коновальця",
    "теліги": "вулиця Олени Теліги",
    "олени теліги": "вулиця Олени Теліги",
    "грінченка": "вулиця Миколи Грінченка",
    "миколи грінченка": "вулиця Миколи Грінченка",
    "григоренка": "проспект Петра Григоренка",
    "петра григоренка": "проспект Петра Григоренка",
    "глушкова": "проспект Академіка Глушкова",
    "академіка глушкова": "проспект Академіка Глушкова",
    "палладіна": "проспект Академіка Палладіна",
    "академіка палладіна": "проспект Академіка Палладіна",
    "вільямса": "вулиця Академіка Вільямса",
    "академіка вільямса": "вулиця Академіка Вільямса",
    "руставелі": "вулиця Шота Руставелі",
    "шота руставелі": "вулиця Шота Руставелі",
    "малевича": "вулиця Казимира Малевича",
    "казимира малевича": "вулиця Казимира Малевича",
    "тютюнника": "вулиця Василя Тютюнника",
    "василя тютюнника": "вулиця Василя Тютюнника",
    "гетьмана": "вулиця Вадима Гетьмана",
    "вадима гетьмана": "вулиця Вадима Гетьмана",
    "голего": "вулиця Миколи Голего",
    "миколи голего": "вулиця Миколи Голего",
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
    "вокзальна", "деміївська", "голосіївська", "голосіївський", "олімпійська",
    "лугова", "садова", "лісова", "польова", "шкільна", "молодіжна", "квіткова", "сонячна",
    "мирна", "спортивна", "промислова", "будівельників", "червона",
}

# Regex for house number extraction immediately following street match or preposition
HOUSE_NUMBER_REGEX = re.compile(
    r"""
    ^[,\s./_–—]*
    (?:буд(?:инок)?\.?|дом|д\.?|№)?\s*
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
    """Removes HTML tags and decodes HTML entities."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


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

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.streets_data = json.load(f)

            for s in self.streets_data.get("streets", []):
                canonical = s.get("canonical", "").strip()
                districts = s.get("districts", [])
                if canonical:
                    self.canonical_to_districts[canonical] = districts

                name = s.get("name", "").strip()
                st_type = s.get("type", "").strip().lower()

                # Invert 2-word names (e.g. "Заболотного Академіка" <-> "Академіка Заболотного")
                parts = name.split()
                if len(parts) == 2:
                    p1, p2 = parts[0].lower(), parts[1].lower()
                    self.search_index[f"{p1} {p2}"] = canonical
                    self.search_index[f"{p2} {p1}"] = canonical
                    if st_type:
                        self.search_index[f"{st_type} {p1} {p2}"] = canonical
                        self.search_index[f"{st_type} {p2} {p1}"] = canonical
                        self.search_index[f"{p1} {p2} {st_type}"] = canonical
                        self.search_index[f"{p2} {p1} {st_type}"] = canonical

            # Fix missing district for known streets
            if "вулиця Михайла Максимовича" in self.canonical_to_districts and not self.canonical_to_districts["вулиця Михайла Максимовича"]:
                self.canonical_to_districts["вулиця Михайла Максимовича"] = ["Голосіївський"]

            # Merge search index from json
            for k, v in self.streets_data.get("search_index", {}).items():
                k_clean = k.strip().lower()
                if len(k_clean) >= 4:
                    self.search_index[k_clean] = v.strip()

        # Merge manual synonyms and known renames
        for k, v in STREET_SYNONYMS.items():
            self.search_index[k.lower()] = v

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
        token_matches = list(re.finditer(r"[\w\-]+", clean_text.lower()))
        num_tokens = len(token_matches)
        if num_tokens == 0:
            return "", ""

        candidates = []

        # Try lengths from 4 down to 1
        for length in (4, 3, 2, 1):
            for i in range(num_tokens - length + 1):
                phrase = " ".join(t.group() for t in token_matches[i : i + length]).strip(".- ")
                if len(phrase) < 4 or phrase not in self.search_index:
                    continue

                canonical = self.search_index[phrase]
                start_char = token_matches[i].start()
                end_char = token_matches[i + length - 1].end()

                # Surrounding context
                prefix_ctx = clean_text[max(0, start_char - 30):start_char].lower()
                suffix_ctx = clean_text[end_char:min(len(clean_text), end_char + 30)].lower()

                # 1. Reject if preceded by metro station indicator
                if re.search(r"\b(?:метро|ст\.?\s*м\.?|станці[яєї]\s+метро|м\.?)\s*$", prefix_ctx):
                    continue

                # 2. Reject if preceded by residential complex without street keyword
                if re.search(r"\b(?:жк|житлов(?:ий|ого|ому)\s+комплекс[уі]?|клубн(?:ий|ого|ому)\s+будинк[уі]?)\s*$", prefix_ctx):
                    continue

                # 3. Reject if preceded by recreation/park indicator
                if re.search(r"\b(?:зона|парк|урочище)\s*$", prefix_ctx):
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
                    r"^\s*(?:вул|вулиц[яіе]|ул|улиц[ае]|просп|проспект[уі]?|пр-т|бульв|бульвар[уі]?|б-р|пров|провул(?:ок|ка|ку)|пер|переул(?:ок|ка|ку)|узвіз|спуск|набережн[аея]|шосе|але[яі]|площ[аі]|майдан|тупик)\.?",
                    suffix_ctx
                ))

                phrase_has_indicator = any(
                    w in ("вулиця", "вул", "проспект", "просп", "шосе", "бульвар", "бульв", "провулок", "пров", "узвіз", "набережна", "площа", "майдан", "тракт", "тупик", "алея")
                    for w in phrase.split()
                )

                # 5. Ambiguous single-word streets MUST have a street indicator
                if length == 1 and phrase in AMBIGUOUS_STREET_WORDS:
                    if not (has_prefix_ind or has_suffix_ind or phrase_has_indicator):
                        continue

                # Extract house number from tail
                tail = clean_text[end_char:]
                house = self._extract_house_from_tail(tail)

                # Calculate confidence score
                score = 0
                if phrase_has_indicator or has_prefix_ind or has_suffix_ind:
                    score += 100
                if house:
                    score += 50
                if length >= 2:
                    score += 50 * length

                candidates.append((score, -start_char, length, canonical, house))

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

    def parse(
        self,
        title: str,
        description: Optional[str] = None,
        district_hint: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """
        Main address parsing entrypoint.
        Priority:
        1. Find street and house in Title.
        2. If street found in Title but house is missing, search Description for house (or street + house).
        3. If street not found in Title, search Description.
        4. Resolve district from hint or canonical street's mapped districts.
        """
        street, house = self.extract_street_and_house(title)

        if street and not house and description:
            # Look for house in description
            desc_street, desc_house = self.extract_street_and_house(description)
            if desc_house:
                house = desc_house
                if not street and desc_street:
                    street = desc_street

        if not street and description:
            street, house = self.extract_street_and_house(description)

        # Resolve district
        district = ""
        if district_hint and district_hint in KYIV_DISTRICTS:
            district = district_hint
        elif street and street in self.canonical_to_districts:
            districts = self.canonical_to_districts[street]
            if districts:
                district = districts[0]

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
            "6": "needs_repair",    # Під чистове оздоблення
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
        if re.search(
            r"\b(?:генератор\s+на\s+будинок|будинковий\s+генератор|генератор\s+на\s+ліфт|ліфт\s+працює\s+від\s+генератора|є\s+генератор|підключено\s+генератор|генератор\s+на\s+опалення|генератор\s+на\s+воду|резервне\s+живлення\s+будинку)\b",
            t_lower,
        ):
            return "yes"
        if re.search(r"\b(?:без\s+генератора|генератора\s+немає|генератор\s+відсутній)\b", t_lower):
            return "no"
        return ""

    @staticmethod
    def extract_inverter(text: str) -> str:
        """Finds if inverter / EcoFlow / battery station is in the flat: yes / no / ''."""
        t_lower = text.lower()
        if re.search(
            r"\b(?:інвертор|інвектор|инвертор|ecoflow|екофлоу|bluetti|блюетті|дбж|ups|акумулятор\s+(?:у|в)\s+квартирі|зарядна\s+станція|резервне\s+живлення\s+(?:в|у)\s+квартирі)\b",
            t_lower,
        ):
            return "yes"
        if re.search(r"\b(?:без\s+інвертора|інвертора\s+немає)\b", t_lower):
            return "no"
        return ""

    @staticmethod
    def extract_pets(offer: Dict[str, Any], text: str) -> str:
        """Determines if pets are allowed: yes / no / ''."""
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

        url = offer.get("url", "").lower()
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
        street, house, district = parser.parse(title, desc, district_hint)

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
        # 1. Quoted name after BC indicator (most accurate)
        m_q = re.search(r'(?:БЦ|бізнес[- ]центр[аіе]?|бизнес[- ]центр[ае]?|ТОЦ|ТРЦ)\s*["«„]([A-Za-zА-Яа-яІіЇїЄє0-9\s\.\-_]{2,40}?)["»“]', text)
        if m_q:
            bc = m_q.group(1).strip()
            if bc and not bc.isdigit():
                return bc

        # 2. Unquoted proper noun on the same line (no newlines) starting with capital letter
        m_u = re.search(r'(?:БЦ|бізнес[- ]центр[аіе]?|бизнес[- ]центр[ае]?|ТОЦ|ТРЦ)[ \t]+([A-ZА-ЯІЇЄ][A-Za-zА-Яа-яІіЇїЄє0-9\-_]*(?:[ \t]+[A-ZА-ЯІЇЄ0-9][A-Za-zА-Яа-яІіЇїЄє0-9\-_]*){0,2})', text)
        if m_u:
            bc = m_u.group(1).strip()
            bc = re.sub(r"[,\.;:]+$", "", bc).strip()
            generic_words = {
                "є", "на", "в", "у", "з", "по", "біля", "від", "до", "для", "та", "і", "або",
                "новий", "новому", "новій", "класу", "клас", "оренда", "аренда", "офіс", "офис",
                "кабинет", "кабінет", "приміщення", "поверх", "ліфт", "ліфти", "паркінг", "парковка",
                "поштомат", "додатково", "комунальні", "квартира", "метро", "вул", "вулиця"
            }
            tokens = [t.lower() for t in re.findall(r"\w+", bc)]
            if tokens and not any(t in generic_words for t in tokens):
                if 2 <= len(bc) <= 40 and not bc.isdigit():
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
        street, house, district = parser.parse(title, desc, district_hint)

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
