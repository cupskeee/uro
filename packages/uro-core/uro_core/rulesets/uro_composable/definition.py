"""The game definition: the settings `uro-composable` is built with (D-114).

Everything a game is arrives here as DATA and is validated here, an unknown key refused. The
vocabulary (what a rating, a track or an action is called) is the definition's own: nothing in
this package names a game word. The pieces are the ones the design fixed and no more: a step
scale and ratings, tracks, tags, modifiers, actions, choices, and the optional gear, encounter,
foes, lists, ready-made characters and credits. Every part is optional; a definition with none
of them is a game with no checks.

Cross-references are checked at parse (a rating an action rolls, a track an effect moves, a tag a
modifier reads), so a definition that validates can't fail on a missing name mid-beat."""

from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from uro_core.rulesets.base import THREAT_LEVELS

_STRICT = ConfigDict(extra="forbid", populate_by_name=True)
Key = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
Text = Annotated[str, StringConstraints(max_length=400)]
Difficulty = Literal["easy", "normal", "hard", "extreme"]
Band = Literal["fail", "cost", "success", "great"]
DIFFICULTIES: tuple[Difficulty, ...] = ("easy", "normal", "hard", "extreme")
BANDS: tuple[Band, ...] = ("fail", "cost", "success", "great")
Who = Literal["self", "target"]
ORDERS = ("alternate", "simultaneous")  # an encounter's `by`, when it isn't a rating

_MAX_ITEMS = 64  # of anything a definition lists: settings stay small enough to pin per campaign
_DICE = re.compile(r"^([+-]?)(\d{1,2})d(\d{1,3})([+-]\d{1,3})?$")


def parse_dice(expr: str) -> tuple[int, int, int, int]:
    """`"2d6+1"`, `"-1d8"` → (sign, count, sides, bonus). Refuses anything else."""
    m = _DICE.match(expr.replace(" ", ""))
    if m is None:
        raise ValueError(f"{expr!r} isn't dice (like 2d6, -1d8, 1d6+2)")
    sign, count, sides, bonus = m.groups()
    n, s = int(count), int(sides)
    if not 1 <= n <= 20 or not 2 <= s <= 100:
        raise ValueError(f"{expr!r}: 1 to 20 dice of 2 to 100 sides")
    return (-1 if sign == "-" else 1), n, s, int(bonus or 0)


class Scale(BaseModel):
    """The step scale every rating sits on. `words` names each step, lowest first."""

    model_config = _STRICT

    min: int = Field(0, ge=0, le=20)
    max: int = Field(5, ge=1, le=20)
    default: int | None = None  # the step of a rating nobody set; None: the middle, rounded down
    words: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Scale:
        if self.min >= self.max:
            raise ValueError("scale.min is below scale.max")
        if self.words and len(self.words) != self.max - self.min + 1:
            raise ValueError(f"scale.words: one word per step ({self.max - self.min + 1})")
        if self.default is not None and not self.min <= self.default <= self.max:
            raise ValueError("scale.default is on the scale")
        return self

    @property
    def default_step(self) -> int:
        return self.default if self.default is not None else (self.min + self.max) // 2

    def clamp(self, step: int) -> int:
        return max(self.min, min(self.max, step))

    def word(self, step: int) -> str:
        return self.words[self.clamp(step) - self.min] if self.words else ""


class Rating(BaseModel):
    model_config = _STRICT

    key: Key
    label: Text = ""
    group: Text = ""
    describes: Text = ""  # one line the planner chooses by
    default: int | None = None


class Per(BaseModel):
    model_config = _STRICT

    rating: Key
    times: int = Field(1, ge=-20, le=20)


class Formula(BaseModel):
    """The one derived value: a base plus a rating's step times a number."""

    model_config = _STRICT

    base: int = Field(0, ge=-1000, le=1000)
    per: Per


class Renew(BaseModel):
    """What sets a track back to its start: this many days of downtime, and/or a fight's end."""

    model_config = _STRICT

    downtime_days: int | None = Field(None, ge=1, le=36500)
    encounter: bool = False


class Track(BaseModel):
    """A counter that rises and falls. `role = "harm"` marks the one that takes injury (a
    character is out of a fight at its `out_at` end); `role = "advance"` the one that opens the
    advancement choice when full. `at_min` / `at_max` name a tag worn while it sits at that end."""

    model_config = _STRICT

    key: Key
    label: Text = ""
    min: int = Field(0, ge=-1000, le=1000)
    max: int | Formula = 5
    start: int | Literal["min", "max"] | Formula = "min"
    renew: Renew | None = None
    at_min: Key | None = None
    at_max: Key | None = None
    role: Literal["harm", "advance"] | None = None
    out_at: Literal["min", "max"] | None = None

    @model_validator(mode="after")
    def _check(self) -> Track:
        if isinstance(self.max, int) and self.max <= self.min:
            raise ValueError(f"track {self.key}: max is above min")
        if self.role == "harm" and self.out_at is None:
            self.out_at = "min"
        if self.role != "harm" and self.out_at is not None:
            raise ValueError(f"track {self.key}: out_at is for the harm track")
        return self


class Tag(BaseModel):
    model_config = _STRICT

    key: Key
    label: Text = ""
    describes: Text = ""
    # until something removes it (the default), to the end of a fight, or until a rest of at
    # least this many days (one stretch of downtime; days of play don't add up)
    lasts: Literal["until_removed", "encounter"] | Annotated[int, Field(ge=1, le=36500)] = (
        "until_removed"
    )


class When(BaseModel):
    model_config = _STRICT

    has: list[Key] = Field(default_factory=list, max_length=8)
    lacks: list[Key] = Field(default_factory=list, max_length=8)


class Change(BaseModel):
    model_config = _STRICT

    step: int = Field(0, ge=-5, le=5)
    band: int = Field(0, ge=-1, le=1)

    @model_validator(mode="after")
    def _check(self) -> Change:
        if not self.step and not self.band:
            raise ValueError("a modifier's change moves the step or the band")
        return self


class Modifier(BaseModel):
    """A standing change to checks. `applies_to` names ratings or actions (or "all"); `when` reads
    the holder's tags; `side = "against"` is for checks made against the holder; of the modifiers
    sharing a `stack` only the largest counts."""

    model_config = _STRICT

    key: Key | None = None
    applies_to: Literal["all"] | list[Key] = "all"
    when: When | None = None
    side: Literal["self", "against"] = "self"
    change: Change
    stack: Key | None = None


class Adjust(BaseModel):
    """Move a track: `by` a number or dice, or `to` a value."""

    model_config = _STRICT

    adjust: Key
    by: int | str | None = None
    to: int | None = None
    who: Who = "self"

    @model_validator(mode="after")
    def _check(self) -> Adjust:
        if (self.by is None) == (self.to is None):
            raise ValueError(f"adjust {self.adjust}: exactly one of by, to")
        if isinstance(self.by, str):
            parse_dice(self.by)
        if isinstance(self.by, int) and not -1000 <= self.by <= 1000:
            raise ValueError(f"adjust {self.adjust}: by is within ±1000")
        return self


class AddTag(BaseModel):
    model_config = _STRICT

    tag: Key
    who: Who = "self"


class RemoveTag(BaseModel):
    model_config = _STRICT

    untag: Key
    who: Who = "self"


class Leave(BaseModel):
    """The one acting gets away from the fight (nothing, outside one)."""

    model_config = _STRICT

    leave: Literal[True]


Effect = Adjust | AddTag | RemoveTag | Leave


class Cost(BaseModel):
    model_config = _STRICT

    track: Key
    by: int = Field(1, ge=1, le=1000)


class Action(BaseModel):
    """What the planner may invoke. One affordance per rating in `uses` (`<key>` when there is
    one, `<key>.<rating>` when several). `harm` makes it a fight's action (and, when the game has
    encounters, the one that starts a fight); `in_fight` allows a harmless one in a fight too."""

    model_config = _STRICT

    key: Key
    label: Text = ""
    describes: Text = ""
    uses: list[Key] = Field(min_length=1, max_length=8)
    triggers: list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]] = Field(
        default_factory=list, max_length=8
    )
    difficulty: Difficulty = "normal"
    cost: Cost | None = None
    harm: bool = False
    in_fight: bool = False
    effects: dict[Band, Annotated[list[Effect], Field(max_length=12)]] = Field(default_factory=dict)

    def ids(self) -> list[tuple[str, str]]:
        """(affordance id, rating) for each rating this action may roll."""
        if len(self.uses) == 1:
            return [(self.key, self.uses[0])]
        return [(f"{self.key}.{r}", r) for r in self.uses]


class GearRule(BaseModel):
    """What a tag on a held thing means: a modifier its holder has while holding it."""

    model_config = _STRICT

    tag: Key
    modifier: Modifier


class Encounter(BaseModel):
    """Turn order: by a rating (highest first), `alternate` (as they entered) or `simultaneous`
    (everyone commits, then the round resolves at once). `award` is given to each player
    character still standing when a fight ends."""

    model_config = _STRICT

    by: str = "alternate"
    award: Adjust | None = None


class FoeTier(BaseModel):
    """A ready level of foe: every rating at `steps`, the named tracks at these sizes. `threat`
    lists the engine's danger words that pick this tier (the only way to one: a spec can't ask
    for a tier, or a player's could)."""

    model_config = _STRICT

    key: Key
    steps: int
    tracks: dict[Key, int] = Field(default_factory=dict)
    threat: list[str] = Field(default_factory=list)


class HeldThing(BaseModel):
    model_config = _STRICT

    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    tags: list[Key] = Field(default_factory=list, max_length=8)


class Option(BaseModel):
    """One thing a pick may take, and what it gives: tags, steps on ratings, room on tracks,
    things held from the start."""

    model_config = _STRICT

    key: Key
    label: Text = ""
    describes: Text = ""
    tags: list[Key] = Field(default_factory=list, max_length=8)
    ratings: dict[Key, int] = Field(default_factory=dict)
    tracks: dict[Key, int] = Field(default_factory=dict)
    holdings: list[HeldThing] = Field(default_factory=list, max_length=12)


class Caps(BaseModel):
    model_config = _STRICT

    min: int | None = None
    max: int | None = None


class ArrayChoice(BaseModel):
    """Assign these steps, one each, to as many ratings."""

    model_config = _STRICT

    choice: Literal["array"]
    steps: list[int] = Field(min_length=1, max_length=_MAX_ITEMS)
    into: Literal["ratings"] = "ratings"


class BudgetChoice(BaseModel):
    """Spend points on steps. `cost` is the price of one step, or a list: the price of reaching
    each step above the scale's lowest. At creation every rating starts at its default."""

    model_config = _STRICT

    choice: Literal["budget"]
    points: int = Field(ge=0, le=1000)
    cost: int | list[int] = 1
    caps: Caps = Field(default_factory=Caps)
    then: Adjust | None = None  # advancement only: what happens once it is answered

    def price(self, step: int, scale: Scale) -> int:
        """What reaching `step` from the one below costs."""
        if isinstance(self.cost, int):
            return self.cost
        return self.cost[step - scale.min - 1]


class RollBand(BaseModel):
    model_config = _STRICT

    upto: int
    step: int


class RollChoice(BaseModel):
    """Uro rolls `dice` for each rating, in order; a total up to a band's `upto` gives its step
    (a total above them all gives the last band's)."""

    model_config = _STRICT

    choice: Literal["roll"]
    dice: str
    steps: list[RollBand] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _check(self) -> RollChoice:
        parse_dice(self.dice)
        if [b.upto for b in self.steps] != sorted({b.upto for b in self.steps}):
            raise ValueError("roll.steps: `upto` rises from band to band")
        return self


class PickChoice(BaseModel):
    """Take `count` options from a list in `lists`."""

    model_config = _STRICT

    choice: Literal["pick"]
    key: Key | None = None  # how a spec names it; default: the list's name
    source: Key = Field(alias="from")
    count: int = Field(1, ge=1, le=12)
    then: Adjust | None = None

    @property
    def name(self) -> str:
        return self.key or self.source


CreationChoice = Annotated[
    ArrayChoice | BudgetChoice | RollChoice | PickChoice, Field(discriminator="choice")
]
AdvanceChoice = Annotated[BudgetChoice | PickChoice, Field(discriminator="choice")]


class ReadyMade(BaseModel):
    """A character offered for any world playing this game: a name, a line, and its spec."""

    model_config = _STRICT

    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    line: Text = ""
    spec: dict[str, Any] = Field(default_factory=dict)


class Definition(BaseModel):
    model_config = _STRICT

    id: Annotated[str, StringConstraints(max_length=80)] = ""
    version: int = Field(1, ge=1)
    name: Text = ""
    pitch: Text = ""
    credits: list[Text] = Field(default_factory=list, max_length=_MAX_ITEMS)
    scale: Scale = Field(default_factory=lambda: Scale.model_validate({}))
    ratings: list[Rating] = Field(default_factory=list, max_length=_MAX_ITEMS)
    tracks: list[Track] = Field(default_factory=list, max_length=_MAX_ITEMS)
    tags: list[Tag] = Field(default_factory=list, max_length=_MAX_ITEMS)
    modifiers: list[Modifier] = Field(default_factory=list, max_length=_MAX_ITEMS)
    actions: list[Action] = Field(default_factory=list, max_length=_MAX_ITEMS)
    gear: list[GearRule] = Field(default_factory=list, max_length=_MAX_ITEMS)
    lists: dict[Key, Annotated[list[Option], Field(max_length=_MAX_ITEMS)]] = Field(
        default_factory=dict, max_length=_MAX_ITEMS
    )
    creation: list[CreationChoice] = Field(default_factory=list, max_length=12)
    advancement: AdvanceChoice | None = None
    encounter: Encounter | None = None
    foes: list[FoeTier] = Field(default_factory=list, max_length=_MAX_ITEMS)
    ready_made: list[ReadyMade] = Field(default_factory=list, max_length=_MAX_ITEMS)

    # --- lookups ---

    def rating(self, key: str) -> Rating:
        return next(r for r in self.ratings if r.key == key)

    def track(self, key: str) -> Track:
        return next(t for t in self.tracks if t.key == key)

    def base_step(self, key: str) -> int:
        r = self.rating(key)
        return r.default if r.default is not None else self.scale.default_step

    @property
    def harm_track(self) -> Track | None:
        return next((t for t in self.tracks if t.role == "harm"), None)

    @property
    def advance_track(self) -> Track | None:
        return next((t for t in self.tracks if t.role == "advance"), None)

    def affordance(self, affordance_id: str) -> tuple[Action, str] | None:
        """The action and rating an affordance id names, or None."""
        for a in self.actions:
            for aid, rating in a.ids():
                if aid == affordance_id:
                    return a, rating
        return None

    # --- validation: every name a part uses is one the definition declares ---

    @model_validator(mode="after")
    def _check(self) -> Definition:
        problems: list[str] = []
        ratings = [r.key for r in self.ratings]
        tracks = [t.key for t in self.tracks]
        tags = [t.key for t in self.tags]
        for kind, keys in (("rating", ratings), ("track", tracks), ("tag", tags)):
            for dup in sorted(k for k, n in Counter(keys).items() if n > 1):
                problems.append(f"{kind} {dup!r} is declared twice")
        for key in sorted(set(ratings) & set(ORDERS)):
            problems.append(f"rating {key!r}: that word is a turn order")
        rset, tset, gset = set(ratings), set(tracks), set(tags)

        def need(where: str, kind: str, key: str | None, known: set[str]) -> None:
            if key is not None and key not in known:
                problems.append(f"{where}: no {kind} {key!r}")

        def formula(where: str, value: object) -> None:
            if isinstance(value, Formula):
                need(where, "rating", value.per.rating, rset)

        def adjust(where: str, eff: Adjust) -> None:
            need(where, "track", eff.adjust, tset)

        def modifier(where: str, m: Modifier) -> None:
            if isinstance(m.applies_to, list):
                names = rset | {a.key for a in self.actions}
                for k in m.applies_to:
                    if k not in names:
                        problems.append(f"{where}: applies_to {k!r} is no rating or action")
            for k in [*(m.when.has if m.when else []), *(m.when.lacks if m.when else [])]:
                need(where, "tag", k, gset)

        for r in self.ratings:
            if r.default is not None and not self.scale.min <= r.default <= self.scale.max:
                problems.append(f"rating {r.key}: default is on the scale")
        for t in self.tracks:
            formula(f"track {t.key}.max", t.max)
            formula(f"track {t.key}.start", t.start)
            need(f"track {t.key}.at_min", "tag", t.at_min, gset)
            need(f"track {t.key}.at_max", "tag", t.at_max, gset)
        for role in ("harm", "advance"):
            if sum(1 for t in self.tracks if t.role == role) > 1:
                problems.append(f"one track may have role {role!r}")
        for i, m in enumerate(self.modifiers):
            modifier(f"modifiers[{i}]", m)
        for g in self.gear:
            modifier(f"gear {g.tag}", g.modifier)
        seen: set[str] = set()
        for a in self.actions:
            if a.key == "hold":  # a fight's own always-there move: an action can't take its name
                problems.append("action 'hold': that name is a fight's own move (use another)")
            for aid, rating in a.ids():
                need(f"action {a.key}.uses", "rating", rating, rset)
                if aid in seen:
                    problems.append(f"action {a.key}: affordance {aid!r} is declared twice")
                seen.add(aid)
            if a.cost is not None:
                need(f"action {a.key}.cost", "track", a.cost.track, tset)
            for band, effects in a.effects.items():
                for eff in effects:
                    where = f"action {a.key}.effects.{band}"
                    if isinstance(eff, Adjust):
                        adjust(where, eff)
                    elif isinstance(eff, AddTag):
                        need(where, "tag", eff.tag, gset)
                    elif isinstance(eff, RemoveTag):
                        need(where, "tag", eff.untag, gset)
        for name, options in self.lists.items():
            keys = [o.key for o in options]
            for dup in sorted(k for k, n in Counter(keys).items() if n > 1):
                problems.append(f"list {name}: option {dup!r} is declared twice")
            for o in options:
                where = f"list {name}.{o.key}"
                for k in o.tags:
                    need(where, "tag", k, gset)
                for k in o.ratings:
                    need(where, "rating", k, rset)
                for k in o.tracks:
                    need(where, "track", k, tset)
        self._check_choices(problems, rset, tset)
        if self.encounter is not None:
            if self.harm_track is None:
                problems.append('encounter: needs a track with role = "harm"')
            if not any(a.harm for a in self.actions):
                problems.append("encounter: needs an action with harm = true")
            if self.encounter.by not in ORDERS:
                need("encounter.by", "rating", self.encounter.by, rset)
            if self.encounter.award is not None:
                adjust("encounter.award", self.encounter.award)
                if self.encounter.award.who != "self":
                    problems.append("encounter.award: who is self")
                if isinstance(self.encounter.award.by, str):
                    problems.append("encounter.award: by is a number, not dice")
        tiers = [f.key for f in self.foes]
        for dup in sorted(k for k, n in Counter(tiers).items() if n > 1):
            problems.append(f"foe tier {dup!r} is declared twice")
        claimed: set[str] = set()
        for f in self.foes:
            if not self.scale.min <= f.steps <= self.scale.max:
                problems.append(f"foe tier {f.key}: steps is on the scale")
            for k in f.tracks:
                need(f"foe tier {f.key}", "track", k, tset)
            if not f.threat:
                problems.append(f"foe tier {f.key}: name the threat words that pick it")
            for word in f.threat:
                if word not in THREAT_LEVELS:
                    problems.append(f"foe tier {f.key}: threat is of {', '.join(THREAT_LEVELS)}")
                elif word in claimed:
                    problems.append(f"foe tier {f.key}: threat {word!r} already picks a tier")
                claimed.add(word)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _check_choices(self, problems: list[str], rset: set[str], tset: set[str]) -> None:
        scale = self.scale
        span = scale.max - scale.min

        def budget(where: str, c: BudgetChoice) -> None:
            if isinstance(c.cost, list) and len(c.cost) != span:
                problems.append(f"{where}: cost lists a price for each of the {span} steps up")
            costs = c.cost if isinstance(c.cost, list) else [c.cost]
            if any(not 0 <= x <= 1000 for x in costs):
                problems.append(f"{where}: a cost is from 0 to 1000")
            lo = c.caps.min if c.caps.min is not None else scale.min
            hi = c.caps.max if c.caps.max is not None else scale.max
            if not scale.min <= lo <= hi <= scale.max:
                problems.append(f"{where}: caps are on the scale, min at most max")

        def pick(where: str, c: PickChoice) -> None:
            options = self.lists.get(c.source)
            if options is None:
                problems.append(f"{where}: no list {c.source!r}")
            elif c.count > len(options):
                problems.append(f"{where}: picks {c.count} of {len(options)}")

        setters = 0
        names: list[str] = []
        for i, c in enumerate(self.creation):
            where = f"creation[{i}]"
            if isinstance(c, PickChoice):
                pick(where, c)
                names.append(c.name)
                if c.then is not None:
                    problems.append(f"{where}: then is for advancement")
                continue
            setters += 1
            if not self.ratings:
                problems.append(f"{where}: there are no ratings to set")
            if isinstance(c, ArrayChoice):
                if len(c.steps) > len(self.ratings):
                    problems.append(f"{where}: more steps than ratings")
                if any(not scale.min <= s <= scale.max for s in c.steps):
                    problems.append(f"{where}: every step is on the scale")
            elif isinstance(c, BudgetChoice):
                budget(where, c)
                if c.then is not None:
                    problems.append(f"{where}: then is for advancement")
            elif any(not scale.min <= b.step <= scale.max for b in c.steps):
                problems.append(f"{where}: every step is on the scale")
        if setters > 1:
            problems.append("creation: one of array, budget, roll sets the ratings")
        for dup in sorted(k for k, n in Counter(names).items() if n > 1):
            problems.append(f"creation: two picks are named {dup!r} (give one a key)")
        adv = self.advancement
        if adv is None:
            if self.advance_track is not None:
                problems.append(f"track {self.advance_track.key}: role advance needs advancement")
            return
        if self.advance_track is None:
            problems.append('advancement: needs a track with role = "advance"')
        if isinstance(adv, BudgetChoice):
            budget("advancement", adv)
            if not self.ratings:
                problems.append("advancement: there are no ratings to raise")
        else:
            pick("advancement", adv)
        if adv.then is not None:
            if adv.then.adjust not in tset:
                problems.append(f"advancement.then: no track {adv.then.adjust!r}")
            if adv.then.who != "self":
                problems.append("advancement.then: who is self")
            if isinstance(adv.then.by, str):
                problems.append("advancement.then: by is a number, not dice")
