"""Seeded synthetic neighborhood generator. Pure Python — no DB, no LLM.

Everything derives from random.Random(seed) so the same seed always yields
the same neighborhood (golden demo path). All residents are fictional.
"""
import math
import random

import networkx as nx

from .models import (
    BBOX, BLOCK_NAMES, Dataset, Helper, Intersection, Person, Road, Shelter,
)

GRID_ROWS, GRID_COLS = 13, 12
N_PEOPLE = 70
N_HELPERS = 80
WALK_M_PER_MIN = 66.0
CENTER = ((BBOX["min_lat"] + BBOX["max_lat"]) / 2, (BBOX["min_lon"] + BBOX["max_lon"]) / 2)

SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
            "吉田", "山田", "松本", "井上", "木村", "清水", "森", "池田", "橋本", "石川"]
GIVEN_F = ["花子", "幸子", "和子", "節子", "美代子", "久子", "トシ", "ハル", "陽子", "恵子"]
GIVEN_M = ["一郎", "清", "茂", "実", "勇", "正夫", "博", "進", "武", "昭"]
FOREIGN = {
    "vi": ["Nguyen Thi Lan", "Tran Van Minh", "Le Thi Hoa", "Pham Van Duc"],
    "zh": ["王 秀英", "李 建国", "张 丽", "刘 伟"],
    "en": ["Emily Carter", "James Wilson", "Sarah Brown", "David Lee"],
    "ko": ["김 영숙", "이 민호", "박 지현", "최 성훈"],
    "ne": ["Sita Gurung", "Ram Thapa", "Gita Rai", "Hari Shrestha"],
    "pt": ["Maria Silva", "João Santos", "Ana Oliveira", "Carlos Souza"],
}
HELPER_GIVEN = ["健太", "翔", "美咲", "大輔", "由美", "拓也", "彩", "亮", "直樹", "真理"]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def block_of(lon: float) -> str:
    t = (lon - BBOX["min_lon"]) / (BBOX["max_lon"] - BBOX["min_lon"])
    return BLOCK_NAMES[max(0, min(4, int(t * 5)))]


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def nearest_intersection(lat: float, lon: float, ints: list[Intersection]) -> str:
    return min(ints, key=lambda i: (i.lat - lat) ** 2 + ((i.lon - lon) * 0.81) ** 2).id


# ----------------------------------------------------------------------------
def generate_intersections(rng: random.Random) -> list[Intersection]:
    dlat = (BBOX["max_lat"] - BBOX["min_lat"]) / GRID_ROWS
    dlon = (BBOX["max_lon"] - BBOX["min_lon"]) / GRID_COLS
    out = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            lat = BBOX["min_lat"] + (r + 0.5) * dlat + rng.uniform(-0.28, 0.28) * dlat
            lon = BBOX["min_lon"] + (c + 0.5) * dlon + rng.uniform(-0.28, 0.28) * dlon
            out.append(Intersection(id=f"I{r:02d}{c:02d}", lat=round(lat, 6), lon=round(lon, 6)))
    return out


def generate_roads(rng: random.Random, ints: list[Intersection]) -> list[Road]:
    by_id = {i.id: i for i in ints}
    edges: list[tuple[str, str]] = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            a = f"I{r:02d}{c:02d}"
            if c + 1 < GRID_COLS:
                edges.append((a, f"I{r:02d}{c + 1:02d}"))
            if r + 1 < GRID_ROWS:
                edges.append((a, f"I{r + 1:02d}{c:02d}"))
            if r + 1 < GRID_ROWS and c + 1 < GRID_COLS and rng.random() < 0.08:
                edges.append((a, f"I{r + 1:02d}{c + 1:02d}"))
    # drop ~7% of segments for irregularity, never disconnecting the graph
    g = nx.Graph(edges)
    for e in rng.sample(edges, k=int(len(edges) * 0.07)):
        g.remove_edge(*e)
        if not nx.is_connected(g) or min(g.degree(e[0]), g.degree(e[1])) < 2:
            g.add_edge(*e)
    roads: list[Road] = []
    for n, (a, b) in enumerate(sorted(g.edges())):
        ia, ib = by_id[a], by_id[b]
        length = haversine_m(ia.lat, ia.lon, ib.lat, ib.lon)
        mid_lat, mid_lon = (ia.lat + ib.lat) / 2, (ia.lon + ib.lon) / 2
        dn = haversine_m(mid_lat, mid_lon, *CENTER) / 800.0
        width = rng.choices([2, 3, 4, 6, 8, 11], weights=[20, 30, 25, 12, 8, 5])[0]
        wooden = _clamp(0.92 - 0.45 * dn + rng.gauss(0, 0.08), 0.05, 0.98)
        base_wall = 0.65 if width <= 3 else (0.3 if width <= 6 else 0.1)
        wall = _clamp(base_wall + rng.gauss(0, 0.1), 0.0, 0.95)
        minutes = length / WALK_M_PER_MIN * (1.2 if width <= 2 else 1.0)
        common = dict(length_m=round(length, 1), width_m=width,
                      block_wall_risk=round(wall, 2), wooden_density=round(wooden, 2),
                      minutes=round(minutes, 2))
        roads.append(Road(id=f"R{n:03d}", from_id=a, to_id=b, **common))
        roads.append(Road(id=f"R{n:03d}r", from_id=b, to_id=a, **common))
    return roads


def _ja_note(p: Person, rng: random.Random) -> str:
    rel = rng.choice(["母", "父", "祖母", "祖父", "本人", "妻"])
    mob = {"walk": "杖なしで歩けるが遅い", "walker": "歩行器を使用",
           "wheelchair": "車椅子を使用", "bedridden": "寝たきり"}[p.mobility]
    alone = "一人暮らし" if p.lives_alone else "家族と同居"
    elev = "、エレベーターなし" if p.floor >= 2 else ""
    extra = []
    if "oxygen" in p.needs:
        extra.append("在宅酸素を使っている")
    if "dialysis" in p.needs:
        extra.append("週3回透析")
    if "dementia" in p.needs:
        extra.append("軽い認知症がある")
    if "hearing" in p.needs:
        extra.append("耳が遠い")
    if "pet" in p.needs:
        extra.append("犬を飼っている")
    tail = ("。" + "、".join(extra)) if extra else ""
    return f"{rel}は{p.age}歳、{mob}、{p.floor}階に{alone}{elev}{tail}。"


def _en_note(p: Person, lang_name: str) -> str:
    mob = {"walk": "walks slowly", "walker": "uses a walker",
           "wheelchair": "uses a wheelchair", "bedridden": "is bedridden"}[p.mobility]
    alone = "lives alone" if p.lives_alone else "lives with family"
    s = f"My relative is {p.age}, {mob}, {alone} on floor {p.floor}."
    if "oxygen" in p.needs:
        s += " Uses home oxygen."
    return s + f" Speaks {lang_name} only."


LANG_NAME = {"vi": "Vietnamese", "zh": "Chinese", "en": "English", "ko": "Korean",
             "ne": "Nepali", "pt": "Portuguese", "ja": "Japanese"}


def generate_people(rng: random.Random, ints: list[Intersection], n: int = N_PEOPLE) -> list[Person]:
    mobilities = ["bedridden"] * 5 + ["wheelchair"] * 10 + ["walker"] * 14
    mobilities += ["walk"] * (n - len(mobilities))
    rng.shuffle(mobilities)
    people = []
    oxygen_left, dialysis_left = 4, 3
    for k in range(n):
        it = rng.choice(ints)
        lat = it.lat + rng.uniform(-0.00025, 0.00025)
        lon = it.lon + rng.uniform(-0.0003, 0.0003)
        mob = mobilities[k]
        young = rng.random() < 0.15
        age = rng.randint(24, 44) if young else rng.randint(66, 96)
        non_ja = rng.random() < 0.15
        lang = rng.choice(["vi", "zh", "en", "ko", "ne", "pt"]) if non_ja else "ja"
        needs: list[str] = []
        if mob != "walk":
            needs.append(mob)
        if young and mob == "walk":
            needs.append("infant")
        if not young and mob in ("bedridden", "walk", "walker") and oxygen_left and rng.random() < 0.25:
            needs.append("oxygen")
            oxygen_left -= 1
        if not young and dialysis_left and rng.random() < 0.06:
            needs.append("dialysis")
            dialysis_left -= 1
        if not young and rng.random() < 0.09:
            needs.append("dementia")
        if not young and rng.random() < 0.05:
            needs.append("hearing")
        if rng.random() < 0.04:
            needs.append("visual")
        if rng.random() < 0.07:
            needs.append("pet")
        if non_ja:
            needs.append("non_japanese_speaker")
        floor = rng.choice([1, 1, 1, 2, 2, 3, 3, 4]) if mob != "bedridden" else rng.choice([1, 2])
        lives_alone = rng.random() < 0.4 and not young
        power = "oxygen" in needs
        if power or mob == "bedridden":
            prio = 5
        elif mob == "wheelchair" or "dialysis" in needs or (lives_alone and age >= 85):
            prio = 4
        elif mob == "walker" or "dementia" in needs or floor >= 3:
            prio = 3
        else:
            prio = rng.choice([1, 2, 2])
        req = {"walk": 1, "walker": 2, "wheelchair": 2, "bedridden": 3}[mob]
        if non_ja:
            name = rng.choice(FOREIGN[lang])
        else:
            given = GIVEN_F if rng.random() < 0.6 else GIVEN_M
            name = f"{rng.choice(SURNAMES)} {rng.choice(given)}"
        p = Person(
            id=f"P-{k + 1:03d}", display_name=name, age=age, lat=round(lat, 6), lon=round(lon, 6),
            floor=floor, lives_alone=lives_alone, language=lang, needs=needs, mobility=mob,
            helpers_required=req, medical_power_dependent=power, priority=prio, consent=True,
            block=block_of(lon), nearest_intersection=nearest_intersection(lat, lon, ints),
        )
        p.notes_freeform = _ja_note(p, rng) if (lang == "ja" or rng.random() < 0.5) else _en_note(p, LANG_NAME[lang])
        people.append(p)
    return people


ROLE_WEIGHTS = {"neighbor": 40, "shop_owner": 12, "民生委員": 8, "自主防災組織": 15,
                "student": 15, "foreign_resident": 10}
# probability that a helper of this role commutes out of the neighborhood on weekdays
COMMUTE_P = {"neighbor": 0.62, "shop_owner": 0.1, "民生委員": 0.2, "自主防災組織": 0.5,
             "student": 0.75, "foreign_resident": 0.6}


def generate_helpers(rng: random.Random, ints: list[Intersection], n: int = N_HELPERS) -> list[Helper]:
    roles = list(ROLE_WEIGHTS)
    helpers = []
    for k in range(n):
        role = rng.choices(roles, weights=[ROLE_WEIGHTS[r] for r in roles])[0]
        it = rng.choice(ints)
        lat = it.lat + rng.uniform(-0.0003, 0.0003)
        lon = it.lon + rng.uniform(-0.0003, 0.0003)
        langs = ["ja"]
        if role == "foreign_resident":
            native = rng.choice(["vi", "zh", "en", "ko", "ne", "pt"])
            langs = [native] + (["ja"] if rng.random() < 0.6 else [])
        elif rng.random() < 0.15:
            langs.append("en")
        skills: set[str] = set()
        if role == "民生委員":
            skills |= {"first_aid"} | ({"nursing"} if rng.random() < 0.35 else set())
        elif role == "自主防災組織":
            skills |= {"carry", "first_aid"} | ({"wheelchair_assist"} if rng.random() < 0.5 else set())
        elif role == "student":
            skills |= {"carry"} | ({"sign_language"} if rng.random() < 0.12 else set())
        elif role in ("neighbor", "shop_owner", "foreign_resident"):
            if rng.random() < 0.5:
                skills.add("carry")
            if rng.random() < 0.2:
                skills.add("wheelchair_assist")
            if rng.random() < 0.15:
                skills.add("first_aid")
        if rng.random() < 0.08:
            skills |= {"nursing", "wheelchair_assist"}
        commuter = rng.random() < COMMUTE_P[role]
        if commuter:
            avail = ["weekday_night", "weekend_all"]
        else:
            avail = ["weekday_day", "weekend_all"] + (["weekday_night"] if rng.random() < 0.85 else [])
        if role == "foreign_resident":
            name = rng.choice(FOREIGN[langs[0]]) if langs[0] != "ja" else "Alex Kim"
        else:
            name = f"{rng.choice(SURNAMES)} {rng.choice(HELPER_GIVEN)}"
        helpers.append(Helper(
            id=f"H-{k + 1:03d}", display_name=name, lat=round(lat, 6), lon=round(lon, 6),
            languages=langs, skills=sorted(skills), has_car=rng.random() < 0.2, role=role,
            availability=avail, reliability=round(rng.betavariate(8, 2), 2),
            block=block_of(lon), nearest_intersection=nearest_intersection(lat, lon, ints),
        ))
    return helpers


SHELTER_SPECS = [
    # name, kind, grid (row, col), capacity, accessible, generator, languages
    ("京島みらい小学校", "指定避難所", (2, 2), 260, True, True, ["ja", "en"]),
    ("京島ひかり中学校", "指定避難所", (10, 3), 300, True, False, ["ja"]),
    ("曳舟さくら体育館", "指定避難所", (6, 6), 180, False, False, ["ja", "zh"]),
    ("京島ふれあい会館", "指定避難所", (3, 9), 140, True, False, ["ja"]),
    ("京島北小学校", "指定避難所", (11, 10), 220, False, True, ["ja", "en", "vi"]),
    ("福祉避難所 やすらぎ苑", "福祉避難所", (5, 1), 30, True, True, ["ja"]),
    ("福祉避難所 ひだまりホーム", "福祉避難所", (8, 10), 25, True, False, ["ja", "en"]),
]


def generate_shelters(rng: random.Random, ints: list[Intersection]) -> list[Shelter]:
    by_id = {i.id: i for i in ints}
    out = []
    for k, (name, kind, (r, c), cap, acc, gen, langs) in enumerate(SHELTER_SPECS):
        it = by_id[f"I{r:02d}{c:02d}"]
        out.append(Shelter(
            id=f"S-{k + 1}", name=name, kind=kind, lat=it.lat, lon=it.lon, capacity=cap,
            accessible=acc, has_generator=gen, languages_supported=langs,
            nearest_intersection=it.id,
        ))
    return out


def generate(seed: int) -> Dataset:
    rng = random.Random(seed)
    ints = generate_intersections(rng)
    roads = generate_roads(rng, ints)
    shelters = generate_shelters(rng, ints)
    people = generate_people(rng, ints)
    helpers = generate_helpers(rng, ints)
    return Dataset(intersections=ints, roads=roads, people=people, helpers=helpers, shelters=shelters)
