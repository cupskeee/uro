"""Uro Composable — the fourth built-in ruleset: a game that is entirely its settings (D-114).

It carries no vocabulary of its own. A world or a campaign hands it a DEFINITION (`definition.py`:
ratings on a step scale, tracks, tags, modifiers, actions, choices, and the optional gear,
encounter and foes) and picks a RESOLVER (`resolvers.py`: d20, 2d6 or a dice pool). This module
knows only steps, bands, counters and tags; "Strength", "Anger", "hit points" and "Composure" are
strings in someone's data. With empty settings it is a game with no checks: every beat is
narration.

    the sheet        {ratings: {key: step}, tracks: {key: {value, min, max}}, tags: [...],
                      picks: {choice: [option, ...]}, pending: [...], sizes: {track: max}}
    a check          the action's rating step, moved by the modifiers that apply → the resolver's
                     dice → a band → that band's effects on the one acting
    a fight          the same, with a foe: `against` modifiers count and `target` effects land;
                     someone is out when the harm track reaches its `out_at` end
    growing          the advance track fills → a pending choice the player answers (D-113)

Everything is a pure function of its inputs and the Rng, like every ruleset."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from uro_core.rulesets.base import (
    RULESET_API_VERSION,
    Action,
    ActionSpec,
    Affordance,
    Award,
    CharSpec,
    CharSpecError,
    CheckRequest,
    CheckResult,
    Choice,
    Combatant,
    Effect,
    EncounterCtx,
    EncounterOutcome,
    EncounterState,
    Holding,
    SpecProblem,
    spec_problems,
)
from uro_core.rulesets.base import Sheet as SheetDict
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_composable import definition as D
from uro_core.rulesets.uro_composable.definition import Definition, parse_dice
from uro_core.rulesets.uro_composable.resolvers import Pool, Resolver, TwoD6, roll, shift

_STRICT = ConfigDict(extra="forbid")
_ADVANCE = "advance"  # the id of the one pending choice: the advancement


class TrackState(BaseModel):
    value: int
    min: int
    max: int


class Sheet(BaseModel):
    """The one sheet shape, whatever the game: every key in it is the definition's."""

    ratings: dict[str, int] = Field(default_factory=dict)
    tracks: dict[str, TrackState] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    picks: dict[str, list[str]] = Field(default_factory=dict)
    pending: list[str] = Field(default_factory=list)
    sizes: dict[str, int] = Field(default_factory=dict)  # a track's size, when a foe tier set it


class Spec(BaseModel):
    """What `new_character` takes. `ratings` and `picks` answer the definition's creation choices;
    `seed` is the dice of a rolled creation (the same seed, the same character). An empty spec is
    the default character. (A foe tier is picked by the engine's threat word, never by a spec.)"""

    model_config = _STRICT

    ratings: dict[str, int] | None = None
    picks: dict[str, list[str]] | None = None
    seed: int | None = None


# --- a sheet under a definition (module-level: the settings' own validation builds with them) ---


def _options(defn: Definition, sheet: Sheet) -> list[D.Option]:
    """Every option the character has picked, at creation or since."""
    sources = {c.name: c.source for c in defn.creation if isinstance(c, D.PickChoice)}
    if isinstance(defn.advancement, D.PickChoice):
        sources.setdefault(defn.advancement.name, defn.advancement.source)
    out: list[D.Option] = []
    for name, keys in sheet.picks.items():
        by_key = {o.key: o for o in defn.lists.get(sources.get(name, ""), [])}
        out += [by_key[k] for k in keys if k in by_key]
    return out


def _value(defn: Definition, sheet: Sheet, bound: int | str | D.Formula, lo: int, hi: int) -> int:
    if isinstance(bound, D.Formula):
        raw = bound.base + bound.per.times * sheet.ratings.get(bound.per.rating, 0)
    else:
        raw = lo if bound == "min" else hi if bound == "max" else int(bound)
    return max(lo, min(hi, raw))


def _size(defn: Definition, sheet: Sheet, track: D.Track) -> int:
    if track.key in sheet.sizes:
        return max(track.min + 1, sheet.sizes[track.key])
    top = (
        track.max if isinstance(track.max, int) else _value(defn, sheet, track.max, -(10**6), 10**6)
    )
    top += sum(o.tracks.get(track.key, 0) for o in _options(defn, sheet))
    return max(track.min + 1, top)


def _settle(defn: Definition, sheet: Sheet, key: str) -> None:
    """After a track moved: the tag it wears at either end, and the choice a full advance track
    opens."""
    track, state = defn.track(key), sheet.tracks[key]
    for tag, at in ((track.at_min, state.min), (track.at_max, state.max)):
        if tag is None:
            continue
        if state.value == at and tag not in sheet.tags:
            sheet.tags.append(tag)
        elif state.value != at and tag in sheet.tags:
            sheet.tags.remove(tag)
    if (
        track.role == "advance"
        and defn.advancement is not None
        and state.value >= state.max
        and _ADVANCE not in sheet.pending
    ):
        sheet.pending.append(_ADVANCE)


def _move(defn: Definition, sheet: Sheet, key: str, *, by: int = 0, to: int | None = None) -> int:
    """Set a track `to` a value or move it `by` one, held to its ends. Returns the change."""
    state = sheet.tracks.get(key)
    if state is None:
        return 0
    before = state.value
    state.value = max(state.min, min(state.max, to if to is not None else before + by))
    _settle(defn, sheet, key)
    return state.value - before


def _resize(defn: Definition, sheet: Sheet, *, fresh: bool = False) -> None:
    """Every track's ends from the definition and the sheet's ratings and picks. `fresh` also
    sets each value to its start (a new character); otherwise values are only held to the ends."""
    for track in defn.tracks:
        top = _size(defn, sheet, track)
        state = sheet.tracks.get(track.key)
        if state is None or fresh:
            start = _value(defn, sheet, track.start, track.min, top)
            sheet.tracks[track.key] = TrackState(value=start, min=track.min, max=top)
        else:
            state.min, state.max = track.min, top
            state.value = max(track.min, min(top, state.value))
        _settle(defn, sheet, track.key)
    for gone in [k for k in sheet.tracks if k not in {t.key for t in defn.tracks}]:
        del sheet.tracks[gone]


def _tier(defn: Definition, threat: str) -> D.FoeTier | None:
    """The foe tier a threat word picks, if any."""
    return next((f for f in defn.foes if threat and threat in f.threat), None)


def load(defn: Definition, sheet: SheetDict) -> Sheet:
    """A stored sheet brought up to this definition: a rating it lacks at its default, one the
    definition no longer has dropped, tracks resized. A sheet made under another definition (an
    earlier version, another campaign's game on the same branch) still reads."""
    s = Sheet.model_validate(sheet)
    s.ratings = {r.key: s.ratings.get(r.key, defn.base_step(r.key)) for r in defn.ratings}
    _resize(defn, s)
    return s


def build(
    defn: Definition, spec: Spec, threat: str = "", *, enforce: bool | None = None
) -> tuple[Sheet, list[SpecProblem], list[dict[str, Any]]]:
    """The sheet a spec makes, the problems with it, and what its creation choices still want.
    The choices are enforced when the spec answers any of them (`enforce=None`), or always
    (`True`, for guidance); an empty spec is the default character and a foe tier skips them."""
    scale = defn.scale
    sheet = Sheet(ratings={r.key: defn.base_step(r.key) for r in defn.ratings})
    problems: list[SpecProblem] = []
    left: list[dict[str, Any]] = []
    given = dict(spec.ratings or {})
    for key, step in list(given.items()):
        if key not in sheet.ratings:
            problems.append(SpecProblem(loc=["ratings", key], message="no such rating"))
            del given[key]
        elif not scale.min <= step <= scale.max:
            problems.append(
                SpecProblem(loc=["ratings", key], message=f"from {scale.min} to {scale.max}")
            )
            del given[key]
    tier = _tier(defn, threat)
    if tier is not None:
        sheet.ratings = {k: tier.steps for k in sheet.ratings} | given
        sheet.sizes = dict(tier.tracks)
        if spec.picks:
            problems.append(SpecProblem(loc=["picks"], message="a foe tier takes no picks"))
        _resize(defn, sheet, fresh=True)
        return sheet, problems, left
    answered = spec.ratings is not None or spec.picks is not None or spec.seed is not None
    if not (answered if enforce is None else enforce):
        _resize(defn, sheet, fresh=True)
        return sheet, problems, left
    picks = dict(spec.picks or {})
    setter = next((c for c in defn.creation if not isinstance(c, D.PickChoice)), None)
    if setter is None:
        sheet.ratings |= given  # no rule for ratings: any step on the scale
    for choice in defn.creation:
        if isinstance(choice, D.ArrayChoice):
            want, got = sorted(choice.steps), sorted(given.values())
            if got != want:
                rest = list(want)
                for step in got:
                    if step in rest:
                        rest.remove(step)
                problems.append(
                    SpecProblem(
                        loc=["ratings"],
                        message=f"assign the steps {', '.join(map(str, choice.steps))}, one each, "
                        f"to {len(want)} ratings",
                    )
                )
                left.append({"choice": "array", "steps": rest})
            sheet.ratings |= given
        elif isinstance(choice, D.BudgetChoice):
            lo = choice.caps.min if choice.caps.min is not None else scale.min
            hi = choice.caps.max if choice.caps.max is not None else scale.max
            spent = 0
            for key, step in given.items():
                if not lo <= step <= hi:
                    problems.append(
                        SpecProblem(loc=["ratings", key], message=f"from {lo} to {hi} here")
                    )
                    continue
                base = defn.base_step(key)
                up, down = range(base + 1, step + 1), range(step + 1, base + 1)
                spent += sum(choice.price(s, scale) for s in up)
                spent -= sum(choice.price(s, scale) for s in down)
                sheet.ratings[key] = step
            if spent > choice.points:
                problems.append(
                    SpecProblem(loc=["ratings"], message=f"{spent} points spent of {choice.points}")
                )
            if spent != choice.points:
                left.append({"choice": "budget", "points": choice.points - spent})
        elif isinstance(choice, D.RollChoice):
            if spec.ratings is not None:
                problems.append(SpecProblem(loc=["ratings"], message="the dice set the ratings"))
            if spec.seed is None:
                problems.append(SpecProblem(loc=["seed"], message="a seed for the dice"))
                left.append({"choice": "roll", "dice": choice.dice})
                continue
            dice = Rng(spec.seed)
            _, count, sides, bonus = parse_dice(choice.dice)
            for key in sheet.ratings:
                total = dice.roll(count, sides) + bonus
                band = next((b for b in choice.steps if total <= b.upto), choice.steps[-1])
                sheet.ratings[key] = band.step
        else:
            options = {o.key for o in defn.lists[choice.source]}
            chosen = picks.pop(choice.name, [])
            valid = [k for k in dict.fromkeys(chosen) if k in options]
            if len(valid) != len(chosen):
                problems.append(
                    SpecProblem(
                        loc=["picks", choice.name],
                        message=f"each one once, from: {', '.join(sorted(options))}",
                    )
                )
            if len(valid) != choice.count:
                problems.append(
                    SpecProblem(loc=["picks", choice.name], message=f"pick {choice.count}")
                )
                if len(valid) < choice.count:
                    left.append(
                        {"choice": "pick", "key": choice.name, "count": choice.count - len(valid)}
                    )
            sheet.picks[choice.name] = valid[: choice.count]
    for name in picks:
        problems.append(SpecProblem(loc=["picks", name], message="no such pick"))
    for option in _options(defn, sheet):
        for key, bonus in option.ratings.items():
            sheet.ratings[key] = scale.clamp(sheet.ratings[key] + bonus)
        sheet.tags += [t for t in option.tags if t not in sheet.tags]
    _resize(defn, sheet, fresh=True)
    return sheet, problems, left


class ComposableConfig(BaseModel):
    """Uro Composable's settings: the game's definition and the resolver it is played with. An
    unknown key anywhere is refused."""

    model_config = _STRICT

    definition: Definition = Field(
        default_factory=lambda: Definition.model_validate({}),
        description="the game: ratings, tracks, tags, modifiers, actions, choices (all optional)",
    )
    resolver: Resolver = Field(
        default_factory=lambda: TwoD6(),
        description="how a check is rolled: kind d20, 2d6 (the default) or pool, with its tuning",
    )

    @model_validator(mode="after")
    def _check(self) -> ComposableConfig:
        push = self.resolver.push if isinstance(self.resolver, Pool) else None
        if push is not None and push.track not in {t.key for t in self.definition.tracks}:
            raise ValueError(f"resolver.push: no track {push.track!r}")
        for i, ready in enumerate(self.definition.ready_made):
            try:
                _, problems, _ = build(self.definition, Spec.model_validate(ready.spec))
            except ValidationError as exc:
                problems = spec_problems(exc)
            if problems:
                why = "; ".join(f"{'.'.join(map(str, p.loc))}: {p.message}" for p in problems)
                raise ValueError(f"ready_made[{i}] ({ready.name}) doesn't build: {why}")
        return self


# --- the fight's private state ---


class _Fighter(BaseModel):
    actor_id: str
    team: str
    sheet: Sheet
    gear: list[str] = Field(default_factory=list)  # the tags on what it holds
    fled: bool = False


class _Encounter(EncounterState):
    fighters: dict[str, _Fighter]
    order: list[str]
    turn_index: int = 0
    committed: dict[str, Action] = Field(default_factory=dict)  # simultaneous rounds only
    round: int = 1


def _gear(holdings: list[Holding]) -> list[str]:
    tags = [t for h in holdings for t in h.data.get("tags", []) if isinstance(t, str)]
    return sorted(set(tags))


class _Attempt(BaseModel):
    band: D.Band
    trace: str
    view: dict[str, Any]
    effects: list[D.Effect] = Field(default_factory=list)  # the band's, to apply
    paid: list[D.Cost] = Field(default_factory=list)  # the action's cost, a pool's push


class UroComposable:
    id = "uro-composable"
    version = "0"
    api_version = RULESET_API_VERSION

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = ComposableConfig.model_validate(config or {})
        self.d = self.config.definition
        self.resolver = self.config.resolver

    # --- what a client reads ---

    def config_schema(self) -> dict[str, Any]:
        return ComposableConfig.model_json_schema()

    def sheet_schema(self) -> dict[str, Any]:
        """The sheet's shape, with this definition's ratings and tracks named in it."""
        schema = Sheet.model_json_schema()
        scale = self.d.scale
        schema["properties"]["ratings"] = {
            "type": "object",
            "properties": {
                r.key: {
                    "type": "integer",
                    "minimum": scale.min,
                    "maximum": scale.max,
                    "title": r.label or r.key,
                }
                for r in self.d.ratings
            },
        }
        schema["properties"]["tracks"] = {
            "type": "object",
            "properties": {
                t.key: {"$ref": "#/$defs/TrackState", "title": t.label or t.key}
                for t in self.d.tracks
            },
        }
        return schema

    def spec_schema(self) -> dict[str, Any]:
        """What a spec may say under this definition: its ratings, its picks, a seed when the
        creation rolls."""
        scale, props = self.d.scale, {}
        if self.d.ratings:
            props["ratings"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    r.key: {
                        "type": "integer",
                        "minimum": scale.min,
                        "maximum": scale.max,
                        "title": r.label or r.key,
                        "description": r.describes,
                    }
                    for r in self.d.ratings
                },
            }
        picks = [c for c in self.d.creation if isinstance(c, D.PickChoice)]
        if picks:
            props["picks"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    c.name: {
                        "type": "array",
                        "items": {"enum": [o.key for o in self.d.lists[c.source]]},
                        "minItems": c.count,
                        "maxItems": c.count,
                        "uniqueItems": True,
                    }
                    for c in picks
                },
            }
        if any(isinstance(c, D.RollChoice) for c in self.d.creation):
            props["seed"] = {"type": "integer", "description": "the dice of a rolled character"}
        return {"type": "object", "additionalProperties": False, "properties": props}

    # --- characters ---

    def new_character(self, spec: CharSpec, rng: Rng) -> SheetDict:
        """A rolled creation takes its dice from the spec's `seed`, never from `rng`: the
        character a preview showed is the one that begins."""
        sheet, problems, _ = build(self.d, Spec.model_validate(spec.data), spec.threat)
        if problems:
            raise CharSpecError(problems)
        return sheet.model_dump()

    def creation_guidance(self, spec: CharSpec) -> dict[str, Any]:
        """What a spec's creation choices still want (points to spend, steps to assign, picks to
        make), so a client shows the ruleset's own count. Optional on the port (D-114)."""
        try:
            sheet, problems, left = build(
                self.d, Spec.model_validate(spec.data), spec.threat, enforce=True
            )
        except ValidationError:
            return {"left": [], "complete": False, "ratings": {}}
        return {
            "left": left,
            "complete": not left and not problems,
            "ratings": self._readings(sheet, left),
        }

    def _readings(self, sheet: Sheet, left: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Each rating as a creator shows it: its step, the scale's word for it, how the resolver
        reads it (`+2`, `3d6`), and whether a step up or down is still a build the creation's
        rules allow. The counting is here so no client does it."""
        scale = self.d.scale
        setter = next((c for c in self.d.creation if not isinstance(c, D.PickChoice)), None)
        budget = setter if isinstance(setter, D.BudgetChoice) else None
        lo, hi = scale.min, scale.max
        points = None
        if budget is not None:
            lo = budget.caps.min if budget.caps.min is not None else lo
            hi = budget.caps.max if budget.caps.max is not None else hi
            points = next((x["points"] for x in left if x["choice"] == "budget"), 0)
        free = setter is None  # no rule for ratings: any step on the scale
        out = {}
        for key, step in sheet.ratings.items():
            up = (free or budget is not None) and step < hi
            if up and budget is not None and points is not None:
                up = budget.price(step + 1, scale) <= points
            out[key] = {
                "step": step,
                "word": scale.word(step),
                "reads": self.resolver.reads(step),
                "can_raise": up,
                "can_lower": (free or budget is not None) and step > lo,
            }
        return out

    def starting_holdings(self, spec: CharSpec) -> list[Holding]:
        """The things a spec's picks give its character to hold from the start. Optional on the
        port (D-114); the engine makes them items."""
        try:
            sheet, _, _ = build(self.d, Spec.model_validate(spec.data), spec.threat)
        except ValidationError:
            return []
        return [
            Holding(item_id="", name=h.name, data={"tags": list(h.tags)} if h.tags else {})
            for option in _options(self.d, sheet)
            for h in option.holdings
        ]

    def progression(self, sheet: SheetDict, award: Award) -> SheetDict:
        """`{"adjust": {track: by}}`: the tracks an award moves."""
        s = load(self.d, sheet)
        for key, by in dict(award.data.get("adjust") or {}).items():
            _move(self.d, s, str(key), by=int(by))
        return s.model_dump()

    def pending_choices(self, sheet: SheetDict) -> list[Choice]:
        s, adv = load(self.d, sheet), self.d.advancement
        if adv is None or _ADVANCE not in s.pending:
            return []
        if isinstance(adv, D.BudgetChoice):
            top = adv.caps.max if adv.caps.max is not None else self.d.scale.max
            options: list[dict[str, Any]] = [
                {
                    "rating": r.key,
                    "label": r.label or r.key,
                    "step": s.ratings[r.key],
                    "cost": adv.price(s.ratings[r.key] + 1, self.d.scale),
                }
                for r in self.d.ratings
                if s.ratings[r.key] < top
            ]
            prompt = f"Raise your ratings: {adv.points} to spend"
        else:
            taken = set(s.picks.get(adv.name, []))
            options = [
                {"option": o.key, "label": o.label or o.key, "describes": o.describes}
                for o in self.d.lists[adv.source]
                if o.key not in taken
            ]
            prompt = f"Pick {min(adv.count, len(options))}"
        return [Choice(id=_ADVANCE, prompt=prompt, options=options)] if options else []

    def answer_choice(self, sheet: SheetDict, choice_id: str, answer: dict[str, Any]) -> SheetDict:
        s, adv = load(self.d, sheet), self.d.advancement
        assert adv is not None
        if isinstance(adv, D.BudgetChoice):
            self._spend(s, adv, answer)
        else:
            self._pick(s, adv, answer)
        s.pending.remove(_ADVANCE)
        track = self.d.advance_track
        if adv.then is not None:
            by = adv.then.by if isinstance(adv.then.by, int) else 0
            _move(self.d, s, adv.then.adjust, by=by, to=adv.then.to)
        elif track is not None:
            _move(self.d, s, track.key, to=track.min)
        _resize(self.d, s)
        # One answer, one advance: a `then` that leaves the track full would open the choice
        # again at once, for ever. The track starts over instead.
        if track is not None and _ADVANCE in s.pending:
            s.pending.remove(_ADVANCE)
            _move(self.d, s, track.key, to=track.min)
            if _ADVANCE in s.pending:  # (a track whose lowest is its highest can't happen)
                s.pending.remove(_ADVANCE)
        return s.model_dump()

    def _spend(self, s: Sheet, adv: D.BudgetChoice, answer: dict[str, Any]) -> None:
        raises = answer.get("ratings")
        if set(answer) != {"ratings"} or not isinstance(raises, dict) or not raises:
            raise CharSpecError([SpecProblem(message="answer with {ratings: {<rating>: <steps>}}")])
        top = adv.caps.max if adv.caps.max is not None else self.d.scale.max
        problems, spent = [], 0
        for key, steps in raises.items():
            if key not in s.ratings:
                problems.append(SpecProblem(loc=["ratings", key], message="no such rating"))
            elif not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
                problems.append(SpecProblem(loc=["ratings", key], message="a number of steps up"))
            elif s.ratings[key] + steps > top:
                problems.append(SpecProblem(loc=["ratings", key], message=f"at most {top} here"))
            else:
                reached = range(s.ratings[key] + 1, s.ratings[key] + steps + 1)
                spent += sum(adv.price(step, self.d.scale) for step in reached)
        if not problems and spent > adv.points:
            problems.append(
                SpecProblem(loc=["ratings"], message=f"{spent} points spent of {adv.points}")
            )
        if problems:
            raise CharSpecError(problems)
        for key, steps in raises.items():
            s.ratings[key] += steps

    def _pick(self, s: Sheet, adv: D.PickChoice, answer: dict[str, Any]) -> None:
        picked = answer.get("picks")
        taken = s.picks.get(adv.name, [])
        open_ = [o.key for o in self.d.lists[adv.source] if o.key not in taken]
        want = min(adv.count, len(open_))
        if (
            set(answer) != {"picks"}
            or not isinstance(picked, list)
            or len(set(map(str, picked))) != len(picked)
            or len(picked) != want
            or any(p not in open_ for p in picked)
        ):
            raise CharSpecError(
                [SpecProblem(loc=["picks"], message=f"pick {want} of: {', '.join(open_)}")]
            )
        s.picks[adv.name] = [*taken, *picked]
        for option in self.d.lists[adv.source]:
            if option.key in picked:
                for key, bonus in option.ratings.items():
                    s.ratings[key] = self.d.scale.clamp(s.ratings[key] + bonus)
                s.tags += [t for t in option.tags if t not in s.tags]

    def downtime(self, sheet: SheetDict, days: int) -> SheetDict | None:
        """Tracks renewed by this many days go back to their start; tags that last days lapse."""
        s = load(self.d, sheet)
        before = s.model_dump()
        for track in self.d.tracks:
            renew = track.renew
            if renew is not None and renew.downtime_days and days >= renew.downtime_days:
                state = s.tracks[track.key]
                start = _value(self.d, s, track.start, state.min, state.max)
                _move(self.d, s, track.key, to=start)
        lapsed = {t.key for t in self.d.tags if isinstance(t.lasts, int) and days >= t.lasts}
        s.tags = [t for t in s.tags if t not in lapsed]
        after = s.model_dump()
        return after if after != before else None

    # --- checks ---

    def affordances(self) -> list[Affordance]:
        out: list[Affordance] = []
        for action in self.d.actions:
            for aid, rating in action.ids():
                r = self.d.rating(rating)
                words = action.describes or action.label or action.key
                if len(action.uses) > 1:
                    using = f"{r.label or r.key}: {r.describes}" if r.describes else r.label
                    words = f"{words} ({using or r.key})"
                out.append(
                    Affordance(
                        id=aid,
                        stat=aid,  # the check is handed only this: it names action and rating
                        difficulty=action.difficulty,
                        trigger_categories=list(action.triggers),
                        starts_encounter=action.harm and self.d.encounter is not None,
                        description=words,
                    )
                )
        return out

    def _modifiers(self, tags: list[str], gear: list[str], side: str) -> list[D.Modifier]:
        """The modifiers a character has standing on `side`: the definition's own and those of
        the tags on what it holds, each when its `when` reads true of the character's tags."""
        have = set(tags)
        mods = [*self.d.modifiers, *(g.modifier for g in self.d.gear if g.tag in gear)]
        return [
            m
            for m in mods
            if m.side == side
            and (
                m.when is None
                or (all(t in have for t in m.when.has) and not any(t in have for t in m.when.lacks))
            )
        ]

    @staticmethod
    def _sum(mods: list[D.Modifier], action: str, rating: str) -> tuple[int, int]:
        """(step, band) the modifiers that apply add up to; of a shared stack, the largest."""
        step = band = 0
        stacked: dict[str, D.Modifier] = {}
        for m in mods:
            if m.applies_to != "all" and action not in m.applies_to and rating not in m.applies_to:
                continue
            if m.stack is None:
                step, band = step + m.change.step, band + m.change.band
                continue
            best = stacked.get(m.stack)
            size = (abs(m.change.step), abs(m.change.band))
            if best is None or size > (abs(best.change.step), abs(best.change.band)):
                stacked[m.stack] = m
        for m in stacked.values():
            step, band = step + m.change.step, band + m.change.band
        return step, band

    def _can_pay(self, sheet: Sheet, cost: D.Cost | None) -> bool:
        if cost is None:
            return True
        state = sheet.tracks.get(cost.track)
        return state is not None and state.value - cost.by >= state.min

    def _attempt(
        self,
        sheet: Sheet,
        gear: list[str],
        action: D.Action,
        rating: str,
        rng: Rng,
        foe: tuple[Sheet, list[str]] | None = None,
    ) -> _Attempt:
        """One use of an action: the step, the dice, the band and what the band does. Nothing is
        applied here; the caller decides whose sheets the effects reach."""
        name = action.label or action.key
        r = self.d.rating(rating)
        if not self._can_pay(sheet, action.cost):
            assert action.cost is not None
            return _Attempt(
                band="fail",
                trace=f"{name}: nothing left to pay for it ({action.cost.track})",
                view={"action": action.key, "rating": rating, "band": "fail", "unpaid": True},
            )
        mods = self._modifiers(sheet.tags, gear, "self")
        if foe is not None:
            mods += self._modifiers(foe[0].tags, foe[1], "against")
        d_step, d_band = self._sum(mods, action.key, rating)
        step = max(0, sheet.ratings.get(rating, self.d.scale.default_step) + d_step)
        push = self.resolver.push if isinstance(self.resolver, Pool) else None
        after_cost = sheet
        if push is not None and action.cost is not None:  # the push is paid after the action
            after_cost = sheet.model_copy(deep=True)
            _move(self.d, after_cost, action.cost.track, by=-action.cost.by)
        rolled = roll(
            self.resolver, step, action.difficulty, rng, may_push=self._can_pay(after_cost, push)
        )
        band = shift(self.resolver, rolled.band, d_band)
        paid = [c for c in (action.cost, push if rolled.pushed else None) if c is not None]
        view = {
            "action": action.key,
            "rating": rating,
            "step": step,
            **({"word": self.d.scale.word(step)} if self.d.scale.words else {}),
            **rolled.view,
            "band": band,
        }
        return _Attempt(
            band=band,
            trace=f"{name} ({r.label or r.key} {step}): {rolled.trace} → {band}",
            view=view,
            effects=list(action.effects.get(band, [])),
            paid=paid,
        )

    def _apply(self, sheet: Sheet, effect: D.Effect, rng: Rng) -> None:
        """One effect on the sheet it reaches (a `leave` is the fight's to carry out)."""
        if isinstance(effect, D.Adjust):
            if effect.to is not None:
                _move(self.d, sheet, effect.adjust, to=effect.to)
            elif isinstance(effect.by, str):
                sign, count, sides, bonus = parse_dice(effect.by)
                _move(self.d, sheet, effect.adjust, by=sign * (rng.roll(count, sides) + bonus))
            else:
                _move(self.d, sheet, effect.adjust, by=effect.by or 0)
        elif isinstance(effect, D.AddTag):
            if effect.tag not in sheet.tags:
                sheet.tags.append(effect.tag)
        elif isinstance(effect, D.RemoveTag) and effect.untag in sheet.tags:
            sheet.tags.remove(effect.untag)

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult:
        found = self.d.affordance(req.stat)
        if found is None:
            raise ValueError(f"uro-composable: this game has no action {req.stat!r}")
        action, rating = found
        before = load(self.d, req.sheet)
        sheet = before.model_copy(deep=True)
        tried = self._attempt(sheet, _gear(req.holdings), action, rating, rng)
        for cost in tried.paid:
            _move(self.d, sheet, cost.track, by=-cost.by)
        for effect in tried.effects:  # outside a fight there is no target: only what lands on self
            if getattr(effect, "who", "self") == "self":
                self._apply(sheet, effect, rng)
        return CheckResult(
            outcome=tried.band,
            trace=tried.trace,
            detail=tried.view,
            sheet=sheet.model_dump() if sheet != before else None,
            player_view=tried.view,
        )

    # --- fights ---

    def _out(self, sheet: Sheet) -> bool:
        track = self.d.harm_track
        state = sheet.tracks.get(track.key) if track is not None else None
        if track is None or state is None:
            return False
        return state.value == (state.min if track.out_at == "min" else state.max)

    def _active(self, f: _Fighter) -> bool:
        return not f.fled and not self._out(f.sheet)

    @property
    def _mode(self) -> Literal["rating", "alternate", "simultaneous"]:
        by = self.d.encounter.by if self.d.encounter is not None else "alternate"
        return by if by in ("alternate", "simultaneous") else "rating"  # type: ignore[return-value]

    def _fighter(self, c: Combatant) -> _Fighter:
        sheet = load(self.d, c.sheet)  # a sheet from before a definition changed still plays
        return _Fighter(actor_id=c.actor_id, team=c.team, sheet=sheet, gear=_gear(c.holdings))

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState:
        fighters = {c.actor_id: self._fighter(c) for c in ctx.combatants}
        order = [c.actor_id for c in ctx.combatants]
        if self._mode == "rating":
            assert self.d.encounter is not None
            by = self.d.encounter.by
            order.sort(key=lambda a: (-fighters[a].sheet.ratings.get(by, 0), a))
        first = next((i for i, a in enumerate(order) if self._active(fighters[a])), 0)
        return _Encounter(
            encounter_id=ctx.encounter_id, fighters=fighters, order=order, turn_index=first
        )

    def join_encounter(
        self, state: EncounterState, combatant: Combatant, rng: Rng
    ) -> EncounterState:
        """A latecomer takes the last place in the order."""
        st = _as_enc(state).model_copy(deep=True)
        st.fighters[combatant.actor_id] = self._fighter(combatant)
        st.order.append(combatant.actor_id)
        return st

    def standing(self, state: EncounterState) -> list[str]:
        return sorted(a for a, f in _as_enc(state).fighters.items() if self._active(f))

    def current_actor(self, state: EncounterState) -> str | None:
        st = _as_enc(state)
        if self.is_over(st) is not None:
            return None
        if self._mode == "simultaneous":
            waiting = (a for a in st.order if self._active(st.fighters[a]))
            return next((a for a in waiting if a not in st.committed), None)
        return st.order[st.turn_index]

    def sheets(self, state: EncounterState) -> dict[str, SheetDict]:
        return {a: f.sheet.model_dump() for a, f in _as_enc(state).fighters.items()}

    def _foes(self, st: _Encounter, actor_id: str) -> list[str]:
        me = st.fighters[actor_id]
        return sorted(a for a, f in st.fighters.items() if f.team != me.team and self._active(f))

    @staticmethod
    def _aims(action: D.Action) -> bool:
        """Whether an action is done TO someone: it harms, or an effect of it lands on a target."""
        return action.harm or any(
            getattr(e, "who", "self") == "target" for effs in action.effects.values() for e in effs
        )

    @staticmethod
    def _in_fight(action: D.Action) -> bool:
        leaves = any(isinstance(e, D.Leave) for effs in action.effects.values() for e in effs)
        return action.harm or action.in_fight or leaves

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]:
        st = _as_enc(state)
        me, foes = st.fighters[actor_id], self._foes(st, actor_id)
        specs: list[ActionSpec] = []
        for action in self.d.actions:
            if not self._in_fight(action) or not self._can_pay(me.sheet, action.cost):
                continue
            aims = self._aims(action)
            if aims and not foes:
                continue
            specs += [ActionSpec(kind=aid, targets=foes if aims else []) for aid, _ in action.ids()]
        return [*specs, ActionSpec(kind="hold")]

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action:
        """The first harmful action it can pay for, with its best rating, at the foe nearest to
        going out (then by name). No dice: the same fight, the same choice."""
        st = _as_enc(state)
        me, foes = st.fighters[actor_id], self._foes(st, actor_id)
        track = self.d.harm_track
        action = next(
            (a for a in self.d.actions if a.harm and self._can_pay(me.sheet, a.cost)), None
        )
        if action is None or not foes or track is None:
            return Action(kind="hold", actor_id=actor_id)

        def room(foe: str) -> int:
            state_ = st.fighters[foe].sheet.tracks[track.key]
            return state_.value - state_.min if track.out_at == "min" else state_.max - state_.value

        aid, _ = max(action.ids(), key=lambda pair: me.sheet.ratings.get(pair[1], 0))
        return Action(kind=aid, actor_id=actor_id, target_id=min(foes, key=lambda f: (room(f), f)))

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        st = _as_enc(state).model_copy(deep=True)
        if self._mode != "simultaneous":
            done = self._act(st.fighters, st.fighters, action, rng, st.round)
            return self._advance_turn(st), done
        st.committed[action.actor_id] = action
        if any(a not in st.committed for a in st.order if self._active(st.fighters[a])):
            return st, []  # the round isn't closed yet
        # everyone acts on the fight as it stood when the round began, then it all lands at once
        was = {a: f.model_copy(deep=True) for a, f in st.fighters.items()}
        effects: list[Effect] = []
        leaving: list[str] = []
        for actor_id in st.order:
            if actor_id in st.committed:
                effects += self._act(
                    was, st.fighters, st.committed[actor_id], rng, st.round, leaving
                )
        for actor_id in leaving:  # who gets away is settled once every blow has landed
            effects += self._leave(st.fighters, actor_id)
        st.committed, st.round = {}, st.round + 1
        return st, effects

    def _act(
        self,
        read: dict[str, _Fighter],
        write: dict[str, _Fighter],
        action: Action,
        rng: Rng,
        round_: int,
        leaving: list[str] | None = None,
    ) -> list[Effect]:
        """One fighter's action: judged on the fighters in `read`, landing on those in `write`
        (the same ones, unless the round is simultaneous). One who leaves goes at once, or is
        only noted in `leaving` when the round settles that after every action."""
        actor = action.actor_id
        if action.kind == "hold":
            return [Effect(kind="hold", actor_id=actor, trace=f"{actor} holds")]
        found = self.d.affordance(action.kind)
        if found is None or not self._in_fight(found[0]):
            raise ValueError(f"uro-composable: {action.kind!r} is no action of this game's fights")
        act, rating = found
        target = action.target_id if action.target_id in read else None
        if self._aims(act) and target is None:
            raise ValueError(f"uro-composable: {action.kind!r} needs a target in the fight")
        me = read[actor]
        foe = (read[target].sheet, read[target].gear) if target is not None else None
        tried = self._attempt(me.sheet, me.gear, act, rating, rng, foe)
        was_out = self._out(write[target].sheet) if target is not None else False
        for cost in tried.paid:
            _move(self.d, write[actor].sheet, cost.track, by=-cost.by)
        left = False
        for effect in tried.effects:
            if isinstance(effect, D.Leave):
                left = True
            elif effect.who == "self":
                self._apply(write[actor].sheet, effect, rng)
            elif target is not None:
                self._apply(write[target].sheet, effect, rng)
        aim = f" at {target}" if target is not None else ""
        effects = [
            Effect(
                kind=tried.band,
                actor_id=actor,
                payload={"target": target, **tried.view},
                trace=f"round {round_}: {actor}{aim}: {tried.trace}",
            )
        ]
        if target is not None and not was_out and self._out(write[target].sheet):
            effects.append(
                Effect(kind="out", actor_id=target, trace=f"{target} is out of the fight")
            )
        if left and leaving is not None:
            leaving.append(actor)
        elif left:
            effects += self._leave(write, actor)
        return effects

    def _leave(self, fighters: dict[str, _Fighter], actor: str) -> list[Effect]:
        if self._out(fighters[actor].sheet):
            return []  # beaten in the same breath: out, not away
        fighters[actor].fled = True
        return [Effect(kind="fled", actor_id=actor, trace=f"{actor} gets away")]

    def _advance_turn(self, st: _Encounter) -> _Encounter:
        if self.is_over(st) is not None:
            return st
        n, idx = len(st.order), st.turn_index
        for _ in range(n):
            idx = (idx + 1) % n
            if idx == 0:
                st.round += 1
            if self._active(st.fighters[st.order[idx]]):
                break
        st.turn_index = idx
        return st

    def is_over(self, state: EncounterState) -> EncounterOutcome | None:
        st = _as_enc(state)
        live = {f.team for f in st.fighters.values() if self._active(f)}
        if len(live) > 1:
            return None
        return EncounterOutcome(
            winner_team=next(iter(live)) if live else None,
            survivors=sorted(a for a, f in st.fighters.items() if self._active(f)),
            out_of_fight=sorted(a for a, f in st.fighters.items() if self._out(f.sheet)),
            fled=sorted(a for a, f in st.fighters.items() if f.fled),
        )

    def conclude_encounter(
        self, state: EncounterState, outcome: EncounterOutcome, pcs: frozenset[str]
    ) -> dict[str, SheetDict]:
        """The fight's end: tags that last a fight lapse, tracks a fight renews go back to their
        start, and each player character still standing gets the encounter's award."""
        st = _as_enc(state).model_copy(deep=True)
        award = self.d.encounter.award if self.d.encounter is not None else None
        for actor_id, f in st.fighters.items():
            self._after_fight(f.sheet)
            if award is not None and actor_id in pcs and actor_id in outcome.survivors:
                self._apply(f.sheet, award, Rng(0))  # an award is a number, never dice
        return {a: f.sheet.model_dump() for a, f in st.fighters.items()}

    def _after_fight(self, sheet: Sheet) -> None:
        """What only lasts a fight ends with it: such tags lapse, tracks a fight renews restart."""
        brief = {t.key for t in self.d.tags if t.lasts == "encounter"}
        sheet.tags = [t for t in sheet.tags if t not in brief]
        for track in self.d.tracks:
            state = sheet.tracks.get(track.key)
            if track.renew is not None and track.renew.encounter and state is not None:
                start = _value(self.d, sheet, track.start, state.min, state.max)
                _move(self.d, sheet, track.key, to=start)

    def leave_encounter(self, state: EncounterState, actor_id: str) -> SheetDict:
        """The sheet of one who leaves an undecided fight (it goes on, or the game closed it):
        tidied like a fight's end, with no award. Optional on the port (D-114)."""
        sheet = _as_enc(state).fighters[actor_id].sheet.model_copy(deep=True)
        self._after_fight(sheet)
        return sheet.model_dump()


def _as_enc(state: EncounterState) -> _Encounter:
    return state if isinstance(state, _Encounter) else _Encounter.model_validate(state.model_dump())
