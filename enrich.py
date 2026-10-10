"""Xarajat matnini tahlil qilish: imlo xatolarini tuzatish va nomi / turi / brendi / rangi /
korobkasi / kategoriyasini alohida ajratish.

Ikki usul:
  * enrich_local — tekin, lug'atlar asosida (internetsiz).
  * enrich       — ANTHROPIC_API_KEY bo'lsa Claude orqali (aniqroq, pullik), xato bo'lsa tekin usulga qaytadi.
"""

import difflib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass

log = logging.getLogger("hisob-bot.enrich")

CATEGORIES = [
    "Oziq-ovqat", "Kafe va restoran", "Transport", "Kiyim-kechak", "Poyabzal", "Aksessuarlar",
    "Elektronika", "Uy-ro'zg'or", "Kommunal to'lovlar", "Aloqa va internet", "Sog'liq",
    "Go'zallik", "Ta'lim", "Ko'ngilochar", "Sovg'alar", "Boshqa",
]

# Buyum turi: kanonik nom -> (kategoriya, yozilish variantlari / o'zaklari)
ITEMS: dict[str, tuple[str, list[str]]] = {
    "sumka": ("Aksessuarlar", ["sumka", "sumkacha", "сумка", "сумочка", "bag", "portfel", "клатч", "klatch"]),
    "ryukzak": ("Aksessuarlar", ["ryukzak", "rukzak", "рюкзак", "backpack"]),
    "hamyon": ("Aksessuarlar", ["hamyon", "koshelek", "кошелек", "кошелёк", "wallet"]),
    "soat": ("Aksessuarlar", ["soat", "часы", "watch"]),
    "ko'zoynak": ("Aksessuarlar", ["ko'zoynak", "ochki", "очки"]),
    "kamar": ("Aksessuarlar", ["kamar", "remen", "ремень"]),
    "taqinchoq": ("Aksessuarlar", ["taqinchoq", "uzuk", "zirak", "bilaguzuk", "marjon", "кольцо", "серьги"]),
    "krossovka": ("Poyabzal", ["krossovka", "krasovka", "kross", "кроссовки", "кроссовка", "sneakers"]),
    "tufli": ("Poyabzal", ["tufli", "туфли", "босоножки", "bosonojka"]),
    "etik": ("Poyabzal", ["etik", "sapog", "сапоги", "botinka", "ботинки"]),
    "shippak": ("Poyabzal", ["shippak", "tapochka", "тапочки", "шлепанцы"]),
    "kurtka": ("Kiyim-kechak", ["kurtka", "куртка", "pujik", "пуховик"]),
    "palto": ("Kiyim-kechak", ["palto", "пальто"]),
    "ko'ylak": ("Kiyim-kechak", ["ko'ylak", "koylak", "платье", "рубашка", "rubashka"]),
    "futbolka": ("Kiyim-kechak", ["futbolka", "футболка", "mayka", "майка"]),
    "shim": ("Kiyim-kechak", ["shim", "bryuki", "брюки", "штаны"]),
    "jinsi": ("Kiyim-kechak", ["jinsi", "djinsi", "джинсы", "jeans"]),
    "yubka": ("Kiyim-kechak", ["yubka", "юбка"]),
    "sviter": ("Kiyim-kechak", ["sviter", "свитер", "kofta", "кофта", "hudi", "худи"]),
    "kostyum": ("Kiyim-kechak", ["kostyum", "костюм"]),
    "sharf": ("Kiyim-kechak", ["sharf", "шарф", "ro'mol", "romol", "платок"]),
    "kepka": ("Kiyim-kechak", ["kepka", "кепка", "shapka", "шапка", "do'ppi"]),
    "paypoq": ("Kiyim-kechak", ["paypoq", "noski", "носки"]),
    "telefon": ("Elektronika", ["telefon", "телефон", "smartfon", "смартфон", "iphone", "айфон"]),
    "noutbuk": ("Elektronika", ["noutbuk", "notebook", "ноутбук", "laptop", "kompyuter", "компьютер"]),
    "planshet": ("Elektronika", ["planshet", "планшет", "ipad"]),
    "quloqchin": ("Elektronika", ["quloqchin", "naushnik", "наушники", "airpods"]),
    "zaryadka": ("Elektronika", ["zaryadka", "zaryadnik", "зарядка", "kabel", "кабель", "powerbank"]),
    "televizor": ("Elektronika", ["televizor", "телевизор", "tv"]),
    "non": ("Oziq-ovqat", ["non", "хлеб", "lepyoshka", "лепешка"]),
    "sut": ("Oziq-ovqat", ["sut", "молоко", "kefir", "кефир", "qatiq", "smetana"]),
    "go'sht": ("Oziq-ovqat", ["go'sht", "gosht", "мясо", "tovuq", "курица", "kolbasa", "колбаса"]),
    "meva": ("Oziq-ovqat", ["meva", "фрукты", "olma", "banan", "uzum", "tarvuz", "qovun", "anor", "mandarin"]),
    "sabzavot": ("Oziq-ovqat", ["sabzavot", "овощи", "kartoshka", "piyoz", "sabzi", "pomidor", "bodring"]),
    "oziq-ovqat": ("Oziq-ovqat", ["oziq-ovqat", "продукты", "produkt", "bozorlik", "bozor"]),
    "shirinlik": ("Oziq-ovqat", ["shirinlik", "shokolad", "шоколад", "tort", "торт", "pechenye"]),
    "ichimlik": ("Oziq-ovqat", ["ichimlik", "suv", "вода", "sharbat", "сок", "kola", "напиток"]),
    "ovqat": ("Kafe va restoran", ["ovqat", "obed", "обед", "tushlik", "nonushta", "kechki ovqat", "ужин",
                                   "lavash", "лаваш", "burger", "бургер", "pitsa", "pizza", "пицца",
                                   "osh", "plov", "shashlik", "somsa", "kafe", "кафе", "restoran", "ресторан"]),
    "kofe": ("Kafe va restoran", ["kofe", "кофе", "coffee", "latte", "капучино", "kapuchino"]),
    "choy": ("Kafe va restoran", ["choy", "чай"]),
    "taksi": ("Transport", ["taksi", "такси", "taxi"]),
    "avtobus": ("Transport", ["avtobus", "автобус", "metro", "метро", "marshrutka", "маршрутка", "tramvay"]),
    "benzin": ("Transport", ["benzin", "бензин", "yoqilg'i", "metan", "метан", "propan", "пропан", "zapravka"]),
    "poyezd": ("Transport", ["poyezd", "поезд", "afrosiyob", "samolyot", "самолет", "avia", "bilet", "билет"]),
    "dori": ("Sog'liq", ["dori", "лекарство", "apteka", "аптека", "vitamin", "витамин"]),
    "shifokor": ("Sog'liq", ["shifokor", "vrach", "врач", "klinika", "клиника", "analiz", "анализ", "stomatolog"]),
    "kosmetika": ("Go'zallik", ["kosmetika", "косметика", "pomada", "помада", "krem", "крем", "тушь"]),
    "atir": ("Go'zallik", ["atir", "parfyum", "духи", "парфюм", "parfum"]),
    "sartarosh": ("Go'zallik", ["sartarosh", "salon", "салон", "manikyur", "маникюр", "strijka", "стрижка"]),
    "kitob": ("Ta'lim", ["kitob", "книга", "daftar", "тетрадь", "ruchka", "ручка", "kurs", "курс", "kontrakt"]),
    "kino": ("Ko'ngilochar", ["kino", "кино", "konsert", "концерт", "teatr", "театр", "o'yin", "bouling"]),
    "obuna": ("Ko'ngilochar", ["obuna", "подписка", "netflix", "spotify", "youtube"]),
    "internet": ("Aloqa va internet", ["internet", "интернет", "wifi", "paynet", "пайнет", "balans", "trafik"]),
    "kommunal": ("Kommunal to'lovlar", ["kommunal", "коммунал", "svet", "свет", "elektr", "gaz", "газ",
                                       "issiq suv", "chiqindi", "ijara", "аренда", "kvartira"]),
    "uy-ro'zg'or": ("Uy-ro'zg'or", ["idish", "посуда", "kir yuvish", "poroshok", "порошок", "sovun", "мыло",
                                     "shampun", "шампунь", "mebel", "мебель", "gilam", "parda"]),
    "sovg'a": ("Sovg'alar", ["sovg'a", "sovga", "подарок", "gul", "цветы", "buket", "букет"]),
}

# Brendlar: kanonik nom -> (kategoriya yoki None, yozilish variantlari)
BRANDS: dict[str, tuple[str | None, list[str]]] = {
    "Polene": ("Aksessuarlar", ["polene", "polen", "полене"]),
    "Louis Vuitton": ("Aksessuarlar", ["louis vuitton", "lv", "луи виттон"]),
    "Gucci": (None, ["gucci", "гуччи"]),
    "Prada": (None, ["prada", "прада"]),
    "Chanel": (None, ["chanel", "шанель"]),
    "Dior": (None, ["dior", "диор"]),
    "Michael Kors": ("Aksessuarlar", ["michael kors", "mk"]),
    "Coach": ("Aksessuarlar", ["coach"]),
    "Charles & Keith": ("Aksessuarlar", ["charles & keith", "charles and keith", "charles keith"]),
    "Zara": ("Kiyim-kechak", ["zara", "зара"]),
    "H&M": ("Kiyim-kechak", ["h&m", "hm", "h and m"]),
    "Mango": ("Kiyim-kechak", ["mango", "манго"]),
    "LC Waikiki": ("Kiyim-kechak", ["lc waikiki", "lcw", "waikiki"]),
    "Koton": ("Kiyim-kechak", ["koton", "котон"]),
    "DeFacto": ("Kiyim-kechak", ["defacto", "de facto", "дефакто"]),
    "Lacoste": ("Kiyim-kechak", ["lacoste", "лакост"]),
    "Tommy Hilfiger": ("Kiyim-kechak", ["tommy hilfiger", "tommy"]),
    "Calvin Klein": ("Kiyim-kechak", ["calvin klein", "ck"]),
    "Levi's": ("Kiyim-kechak", ["levi's", "levis", "levi"]),
    "Nike": (None, ["nike", "найк"]),
    "Adidas": (None, ["adidas", "адидас", "adidaz"]),
    "Puma": (None, ["puma", "пума"]),
    "New Balance": ("Poyabzal", ["new balance", "nb"]),
    "Reebok": (None, ["reebok", "рибок"]),
    "Skechers": ("Poyabzal", ["skechers", "скечерс"]),
    "Converse": ("Poyabzal", ["converse", "конверс"]),
    "Vans": ("Poyabzal", ["vans", "ванс"]),
    "Apple": ("Elektronika", ["apple", "эпл", "iphone", "айфон", "ipad", "macbook", "airpods"]),
    "Samsung": ("Elektronika", ["samsung", "самсунг", "galaxy"]),
    "Xiaomi": ("Elektronika", ["xiaomi", "сяоми", "redmi", "poco"]),
    "Huawei": ("Elektronika", ["huawei", "хуавей"]),
    "Honor": ("Elektronika", ["honor", "хонор"]),
    "Artel": ("Elektronika", ["artel", "артель"]),
    "LG": ("Elektronika", ["lg"]),
    "Sony": ("Elektronika", ["sony", "сони"]),
    "Philips": ("Elektronika", ["philips", "филипс"]),
    "Bosch": ("Uy-ro'zg'or", ["bosch", "бош"]),
    "Tefal": ("Uy-ro'zg'or", ["tefal", "тефаль"]),
    "Korzinka": ("Oziq-ovqat", ["korzinka", "корзинка"]),
    "Makro": ("Oziq-ovqat", ["makro", "макро"]),
    "Havas": ("Oziq-ovqat", ["havas", "хавас"]),
    "Baraka": ("Oziq-ovqat", ["baraka market", "baraka"]),
    "Carrefour": ("Oziq-ovqat", ["carrefour", "karfur", "карфур"]),
    "Evos": ("Kafe va restoran", ["evos", "эвос"]),
    "Max Way": ("Kafe va restoran", ["max way", "maxway", "максвей"]),
    "Oqtepa Lavash": ("Kafe va restoran", ["oqtepa lavash", "oqtepa", "октепа"]),
    "Les Ailes": ("Kafe va restoran", ["les ailes", "lesailes", "лесаилес"]),
    "KFC": ("Kafe va restoran", ["kfc", "кфс"]),
    "Bellissimo": ("Kafe va restoran", ["bellissimo", "беллиссимо"]),
    "Safia": ("Kafe va restoran", ["safia", "сафия"]),
    "Yandex Go": ("Transport", ["yandex go", "yandex", "яндекс"]),
    "MyTaxi": ("Transport", ["mytaxi", "my taxi"]),
    "Uzum": (None, ["uzum market", "uzum nasiya", "узум маркет"]),
    "Wildberries": (None, ["wildberries", "wb", "вайлдберриз"]),
    "Ozon": (None, ["ozon", "озон"]),
    "Pandora": ("Aksessuarlar", ["pandora", "пандора"]),
    "L'Oreal": ("Go'zallik", ["l'oreal", "loreal", "лореаль"]),
    "Nivea": ("Go'zallik", ["nivea", "нивея"]),
    "MAC": ("Go'zallik", ["mac cosmetics"]),
    "Maybelline": ("Go'zallik", ["maybelline", "мейбелин"]),
    "Beeline": ("Aloqa va internet", ["beeline", "билайн"]),
    "Ucell": ("Aloqa va internet", ["ucell", "юселл"]),
    "Mobiuz": ("Aloqa va internet", ["mobiuz", "мобиуз", "ums"]),
    "Uzmobile": ("Aloqa va internet", ["uzmobile", "узмобайл"]),
}

COLORS: dict[str, list[str]] = {
    "qora": ["qora", "black", "черный", "чёрный", "chyorniy", "cherniy"],
    "oq": ["oq", "white", "белый", "beliy"],
    "qizil": ["qizil", "red", "красный", "krasniy"],
    "ko'k": ["ko'k", "kok", "blue", "синий", "siniy"],
    "havorang": ["havorang", "голубой", "goluboy", "light blue"],
    "yashil": ["yashil", "green", "зеленый", "зелёный", "zeleniy"],
    "sariq": ["sariq", "yellow", "желтый", "жёлтый"],
    "to'q sariq": ["to'q sariq", "orange", "оранжевый", "apelsin rang"],
    "jigarrang": ["jigarrang", "jigar rang", "brown", "коричневый", "korichneviy"],
    "kulrang": ["kulrang", "kul rang", "grey", "gray", "серый", "seriy"],
    "pushti": ["pushti", "pink", "розовый", "rozoviy"],
    "binafsha": ["binafsha", "siyohrang", "purple", "фиолетовый"],
    "bej": ["bej", "beige", "бежевый", "bejeviy"],
    "oltinrang": ["oltinrang", "oltin rang", "gold", "золотой", "zolotoy"],
    "kumushrang": ["kumushrang", "kumush rang", "silver", "серебряный"],
    "qaymoqrang": ["qaymoqrang", "krem rang", "кремовый", "sut rang", "молочный"],
    "bordo": ["bordo", "бордовый", "burgundy", "to'q qizil"],
}

_BOX_NO = re.compile(
    r"(korobkasiz|karobkasiz|karopkasiz|qutisiz|без короб\w*|korobka\w* yo'q|qutisi yo'q|box yo'q|no box)",
    re.IGNORECASE,
)
_BOX_YES = re.compile(r"(korobka|karobka|karopka|korobk|qutisi|qutili|коробк\w*|box)", re.IGNORECASE)
_BOX_WORDS_RE = re.compile(
    r"\b(korobka|karobka|karopka|qutisi|qutili|quti|box)\w*(\s+(bilan|bn|bln|yo'q))?|без\s+короб\w*|\bкоробк\w*",
    re.IGNORECASE,
)

# Chat qisqartmalari
ABBREVIATIONS = {"bn": "bilan", "bln": "bilan", "b-n": "bilan", "uchn": "uchun", "u-n": "uchun",
                 "rangl": "rangli"}

# Tuzatilmasligi kerak bo'lgan oddiy so'zlar (noto'g'ri "tuzatish"ning oldini oladi)
COMMON_WORDS = {
    "bilan", "uchun", "rangli", "rang", "yangi", "eski", "katta", "kichik", "uzun", "qisqa", "sotib",
    "oldim", "olindi", "xarid", "to'lov", "tolov", "pul", "ming", "so'm", "dona", "kilo", "litr",
    "sutka", "kunlik", "oylik", "uchta", "ikkita", "bitta", "onam", "otam", "ukam", "singlim", "uyga",
    "ishga", "bolalar", "bolaga", "do'kon", "dokon", "market", "magazin", "kecha", "bugun", "ertaga",
    "yetkazib", "dostavka", "доставка", "chegirma", "skidka",
}


@dataclass
class ItemInfo:
    description: str           # to'liq izoh, imlosi tuzatilgan
    item_name: str             # nomi (rang/korobkasiz), masalan "Polene sumka"
    item_type: str | None      # nimaligi, masalan "sumka"
    brand: str | None
    color: str | None
    has_box: bool | None       # True — korobka bilan, False — korobkasiz, None — aytilmagan
    category: str

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(word: str) -> str:
    return re.sub(r"[ʻʼ’`‘]", "'", word.lower())


def _vocabulary() -> dict[str, str]:
    """Bitta so'zli variant -> ko'rsatiladigan to'g'ri shakl."""
    vocab: dict[str, str] = {}
    for canon, (_, variants) in ITEMS.items():
        for v in variants:
            if " " not in v:
                vocab[_norm(v)] = v
    for canon, (_, variants) in BRANDS.items():
        for v in variants:
            if " " in v:
                continue
            # Faqat brend nomining o'zi yoki xato yozilishi (polen -> Polene) brendga almashtiriladi;
            # "iphone", "galaxy" kabi boshqa so'zlar o'zgarmaydi
            target = v
            for canon_word in canon.split():
                if difflib.SequenceMatcher(None, _norm(v), _norm(canon_word)).ratio() >= 0.8:
                    target = canon_word  # "yandex" -> "Yandex", "polen" -> "Polene"
                    break
            vocab[_norm(v)] = target
    for canon, variants in COLORS.items():
        for v in variants:
            if " " not in v:
                vocab[_norm(v)] = v
    for w in COMMON_WORDS:
        vocab[_norm(w)] = w
    return vocab


_VOCAB = _vocabulary()
_WORD_RE = re.compile(r"[^\W\d_]+(?:['ʻʼ’`‘-][^\W\d_]+)*", re.UNICODE)


def correct_spelling(text: str) -> str:
    """Ma'lum so'zlarga juda o'xshash xato yozilgan so'zlarni tuzatadi: 'sumak' -> 'sumka', 'polen' -> 'Polene'."""
    def fix(m: re.Match) -> str:
        word = m.group()
        key = _norm(word)
        if key in ABBREVIATIONS:
            return ABBREVIATIONS[key]
        if key in _VOCAB:
            right = _VOCAB[key]
            # brend nomini to'g'ri yozilishiga keltiramiz (polene -> Polene), oddiy so'zlarga tegmaymiz
            return right if right[:1].isupper() else word
        if len(key) < 4 or not key.isascii() and not key.replace("'", "").isascii():
            return word  # rus so'zlari qo'shimcha bilan o'zgaradi — ularni "tuzatmaymiz"
        candidates = [
            c for c in difflib.get_close_matches(key, _VOCAB.keys(), n=3, cutoff=0.8)
            if c[0] == key[0] and abs(len(c) - len(key)) <= 2
        ]
        # "sumkasi", "krossovkalar" kabi qo'shimchali to'g'ri so'zlarni "tuzatib" yubormaslik uchun
        if not candidates or any(key.startswith(c) for c in _VOCAB if len(c) >= 4):
            return word
        right = _VOCAB[candidates[0]]
        if right[:1].isupper():
            return right
        return right[0].upper() + right[1:] if word[:1].isupper() else right

    return _WORD_RE.sub(fix, text)


def _find_phrase(text_norm: str, variants: list[str]) -> bool:
    for v in variants:
        v = _norm(v)
        if not v.isascii() and " " not in v and len(v) > 4:
            # ruscha so'z oxiri o'zgaradi: "сумка" -> "сумку", "черный" -> "черную"
            v = v[:-2] if len(v) >= 6 else v[:-1]
        if len(v) <= 3 or " " in v:
            if re.search(rf"(?<![\w']){re.escape(v)}(?![\w'])", text_norm):
                return True
        elif re.search(rf"(?<![\w']){re.escape(v)}", text_norm):  # o'zak: "sumkasi", "taksiga"
            return True
    return False


def enrich_local(text: str) -> ItemInfo:
    corrected = re.sub(r"\s+", " ", correct_spelling(text)).strip()
    norm = _norm(corrected)

    brand = next((b for b, (_, vs) in BRANDS.items() if _find_phrase(norm, vs)), None)
    item_type = next((t for t, (_, vs) in ITEMS.items() if _find_phrase(norm, vs)), None)
    color = next((c for c, vs in COLORS.items() if _find_phrase(norm, vs)), None)

    has_box = None
    if _BOX_NO.search(norm):
        has_box = False
    elif _BOX_YES.search(norm):
        has_box = True

    category = (
        (ITEMS[item_type][0] if item_type else None)
        or (BRANDS[brand][0] if brand else None)
        or "Boshqa"
    )

    # Nomi: rang va korobka haqidagi so'zlarsiz
    name = _BOX_WORDS_RE.sub(" ", corrected)
    if color:
        for v in sorted(COLORS[color], key=len, reverse=True):
            name = re.sub(rf"(?<![\w']){re.escape(v)}(\s*rang(li)?)?(?![\w'])", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" ,.-") or corrected

    return ItemInfo(
        description=corrected or text,
        item_name=name[:1].upper() + name[1:] if name else name,
        item_type=item_type,
        brand=brand,
        color=color,
        has_box=has_box,
        category=category,
    )


def items_category(items: list[dict]) -> str:
    """Chekdagi mahsulotlar bo'yicha eng ko'p pul ketgan kategoriya."""
    totals: dict[str, int] = {}
    for item in items:
        category = enrich_local(f"{item.get('name', '')} {item.get('product', '')}").category
        if category != "Boshqa":
            totals[category] = totals.get(category, 0) + int(item.get("amount") or 0)
    return max(totals, key=totals.get) if totals else "Boshqa"


# --- Claude (ixtiyoriy) ---

MODEL = "claude-opus-5-5"

_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "item_name": {"type": "string"},
        "item_type": {"type": "string"},
        "brand": {"type": "string"},
        "color": {"type": "string"},
        "box": {"type": "string", "enum": ["bor", "yo'q", ""]},
        "category": {"type": "string", "enum": CATEGORIES},
    },
    "required": ["description", "item_name", "item_type", "brand", "color", "box", "category"],
    "additionalProperties": False,
}

_PROMPT = """Bu O'zbekistondagi foydalanuvchi o'z xarajati haqida yozgan izoh (summasiz). U o'zbek (lotin yoki kirill), rus yoki aralash tilda, imlo xatolari va qisqartmalar bilan yozilgan bo'lishi mumkin.

Izoh: {text}

Quyidagilarni qaytar (bo'lmasa bo'sh qator):
- description: imlo xatolari tuzatilgan to'liq izoh, asl tilida, ma'nosini o'zgartirmasdan (masalan "qora polen sumka korobkasi bn" -> "qora Polene sumka korobkasi bilan")
- item_name: buyumning qisqa nomi, rang va korobkasiz (masalan "Polene sumka")
- item_type: nimaligi bitta so'z bilan o'zbekcha (masalan "sumka", "krossovka", "taksi", "non")
- brand: brend yoki do'kon nomi to'g'ri yozilishida (masalan "Polene", "Korzinka", "Yandex Go")
- color: rangi o'zbekcha (masalan "qora", "oq", "jigarrang")
- box: buyum korobka (quti) bilan bo'lsa "bor", korobkasiz deyilgan bo'lsa "yo'q", aytilmagan bo'lsa ""
- category: ro'yxatdan eng mosi
Faqat izohda bor narsani yoz, o'zingdan qo'shma."""

_client = None


async def _enrich_claude(text: str) -> ItemInfo:
    global _client
    import anthropic

    if _client is None:
        _client = anthropic.AsyncAnthropic()
    response = await _client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _PROMPT.format(text=text)}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude javobi to'liq emas: {response.stop_reason}")
    raw = next(b.text for b in response.content if b.type == "text")
    data = json.loads(raw)
    return ItemInfo(
        description=data["description"].strip() or text,
        item_name=data["item_name"].strip() or data["description"].strip() or text,
        item_type=data["item_type"].strip().lower() or None,
        brand=data["brand"].strip() or None,
        color=data["color"].strip().lower() or None,
        has_box={"bor": True, "yo'q": False}.get(data["box"]),
        category=data["category"] if data["category"] in CATEGORIES else "Boshqa",
    )


async def enrich(text: str) -> ItemInfo:
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return await _enrich_claude(text)
        except Exception:
            log.exception("Claude bilan tahlil qilib bo'lmadi, tekin usul ishlatiladi")
    return enrich_local(text)
