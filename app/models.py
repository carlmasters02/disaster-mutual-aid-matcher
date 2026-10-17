"""Shared pydantic domain models. No Neo4j or LLM imports here — pure data."""
from typing import Literal

from pydantic import BaseModel, Field

Lang = Literal["ja", "en", "zh", "vi", "ko", "ne", "pt"]
Mobility = Literal["walk", "walker", "wheelchair", "bedridden"]
Need = Literal[
    "wheelchair", "walker", "bedridden", "dementia", "visual", "hearing",
    "oxygen", "dialysis", "infant", "non_japanese_speaker", "pet",
]
TimeSlot = Literal["weekday_day", "weekday_night", "weekend_all"]
NEEDS: list[str] = list(Need.__args__)  # type: ignore[attr-defined]
LANGS: list[str] = list(Lang.__args__)  # type: ignore[attr-defined]

SKILLS: list[str] = ["carry", "wheelchair_assist", "nursing", "sign_language", "first_aid"]
HELPER_ROLES: list[str] = [
    "neighbor", "民生委員", "自主防災組織", "student", "shop_owner", "foreign_resident",
]

# Fictional setting: Kyojima, Sumida-ku, Tokyo (wooden-housing dense district).
BBOX = {"min_lat": 35.694, "max_lat": 35.708, "min_lon": 139.797, "max_lon": 139.815}
BLOCK_NAMES = ["京島一丁目", "京島二丁目", "京島三丁目", "京島四丁目", "京島五丁目"]


class Person(BaseModel):
    id: str
    display_name: str
    age: int
    lat: float
    lon: float
    floor: int = 1
    lives_alone: bool = False
    language: Lang = "ja"
    needs: list[Need] = Field(default_factory=list)
    mobility: Mobility = "walk"
    helpers_required: int = 1  # 1-3
    medical_power_dependent: bool = False
    priority: int = 3  # 1-5
    consent: bool = True
    notes_freeform: str = ""
    block: str = ""  # fictional block name, e.g. 京島二丁目
    nearest_intersection: str = ""


class Helper(BaseModel):
    id: str
    display_name: str
    lat: float
    lon: float
    languages: list[Lang] = Field(default_factory=lambda: ["ja"])
    skills: list[str] = Field(default_factory=list)
    has_car: bool = False
    role: str = "neighbor"
    availability: list[TimeSlot] = Field(default_factory=list)
    reliability: float = 0.9  # 0-1
    injured: bool = False
    available: bool = True
    block: str = ""
    nearest_intersection: str = ""


class Shelter(BaseModel):
    id: str
    name: str
    kind: Literal["指定避難所", "福祉避難所"]
    lat: float
    lon: float
    capacity: int
    occupancy: int = 0
    accessible: bool = False
    has_generator: bool = False
    damaged: bool = False
    languages_supported: list[Lang] = Field(default_factory=lambda: ["ja"])
    nearest_intersection: str = ""


class Intersection(BaseModel):
    id: str
    lat: float
    lon: float


class Road(BaseModel):
    """One directed edge. The generator emits both directions as two rows."""

    id: str
    from_id: str
    to_id: str
    length_m: float
    width_m: float
    block_wall_risk: float  # 0-1
    wooden_density: float  # 0-1
    blocked: bool = False
    on_fire: bool = False
    minutes: float  # walking time at ~66 m/min, adjusted for width


class Assignment(BaseModel):
    helper_id: str
    person_id: str
    role: Literal["primary", "backup"]
    eta_min: float
    plan_version: int


class Evacuation(BaseModel):
    person_id: str
    shelter_id: str
    route_ids: list[str]
    eta_min: float
    plan_version: int


class Uncovered(BaseModel):
    person_id: str
    priority: int
    reason: Literal["no_candidates", "eta_exceeded", "capacity_exhausted", "unreachable"]


class AssignmentPlan(BaseModel):
    version: int
    time_slot: TimeSlot
    assignments: list[Assignment] = Field(default_factory=list)
    evacuations: list[Evacuation] = Field(default_factory=list)
    uncovered: list[Uncovered] = Field(default_factory=list)


class Coverage(BaseModel):
    slot: str
    total: int = 0
    covered: int = 0  # all primaries assigned + reachable shelter (headline metric)
    fully_covered: int = 0  # covered + backup
    primary_only: int = 0
    uncovered: int = 0
    uncovered_by_priority: dict[int, int] = Field(default_factory=dict)
    pct_covered: float = 0.0
    pct_fully: float = 0.0
    avg_eta_min: float = 0.0
    shelter_loads: list[dict] = Field(default_factory=list)


class Plan(BaseModel):
    id: str
    person_id: str
    version: int
    status: Literal["draft", "active", "broken", "superseded"] = "draft"
    created_at: str = ""
    doc_ja: str = ""
    doc_resident_lang: str = ""
    doc_language: Lang = "ja"
    fallback: bool = False


class Dataset(BaseModel):
    intersections: list[Intersection]
    roads: list[Road]
    people: list[Person]
    helpers: list[Helper]
    shelters: list[Shelter]
