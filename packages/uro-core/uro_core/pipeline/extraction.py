"""Extractor + validation gauntlet (docs/05, 12, 13).

The extractor turns generated prose into *proposed* world state; the gauntlet turns
proposals into committed events — or drops them.

Enforced by construction (structural — cannot be bypassed):
- Whitelist: the schema permits only actors, places, factions, threads, claims — and, via
  affiliations, `member_of`/`located_in` edges between them (D-50). The extractor is structurally
  incapable of proposing damage/death/loss events; every emergent entity is a benign CREATION.
  It may propose exactly two bounded mutations: resolving a live EMERGENT thread the narration
  settles (D-52) and recording an alias on a known actor (D-56) — never an authored thread, never
  damage/death/loss or any other state change.
- Tier ceiling: the gauntlet always creates actors at T1.
- Role-noun guard (D-51): an actor is never MINTED from a description ("the guard" — all
  lowercase once its article is stripped); under D-50 it would become a name key every later
  guard merges into. An existing actor known by that alias still links.
- Player text isolation: the extractor is fed generated prose only, never the
  player's intent text (see engine._extract) — so a player cannot directly assert
  a claim into the extractor.

Enforced by policy (correct as far as the extractor classifies honestly):
- Provenance: narrator-asserted → `truth=true`; a character's speech → `truth=unknown`
  plus a belief for the speaker (an NPC can lie without corrupting world truth).
- Contradiction: a proposed `truth=true` claim the extractor flags as contradicting
  an existing `truth=true` claim is downgraded to `unknown`.
- Flavor filter (docs/05 promotion rules): the prompt tells the extractor to leave
  sensory/atmospheric description out entirely; a claim it marks `durable=false` (or
  any that slips through) is dropped by the gauntlet — flavor never becomes canon.
  Real models over-extract atmosphere as fact without this (found in the first live run).
- Restatement (D-51): the extractor sees the KNOWN threads/claims and is told not to repeat
  them; a claim or thread that restates a known one EXACTLY (after normalizing case, spacing,
  and trailing punctuation) is dropped structurally. A reworded restatement still commits —
  only the prompt guards that (a similarity merge would risk fusing two distinct facts/plots).

Parsing salvages per ITEM (D-51): a null optional field takes its default and one malformed
item is dropped alone — it never discards the rest of the beat's extracted state.

NOT yet implemented (docs/13, deferred): evidence-span/consequence gating and the
"a truth=true claim must not merely restate a character's assertion" guard. So
`truth=true` currently rests on the extractor's self-declared provenance label; a
narrator that echoes a player's words is a residual surface, mitigated only by the
narrator being the trusted tier. Do not read this as a hard security boundary.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    DomainEvent,
    Truth,
    actor_created,
    belief_changed,
    claim_recorded,
    edge_added,
    entity_alias_added,
    faction_created,
    place_created,
    thread_created,
    thread_state_changed,
)
from uro_core.domain.extraction_policy import ExtractionPolicy
from uro_core.domain.ids import new_id
from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RecallBundle
from uro_core.ports.projections import ProjectionQueries
from uro_core.providers.base import Message

logger = logging.getLogger(__name__)

_DEFAULT_BELIEF_CONFIDENCE = 0.8  # placeholder until belief-strength modeling (docs/11)


class ProposedActor(BaseModel):
    name: str
    role: str = ""
    member_of: str = ""  # a faction this actor belongs to (D-50; cascade-created if it's new)
    located_in: str = ""  # a place this actor is at (D-50; cascade-created if it's new)


class ProposedPlace(BaseModel):
    name: str
    description: str = ""
    parent: str = ""  # a larger place this sits inside (D-50; cascade-created if it's new)


class ProposedFaction(BaseModel):
    name: str
    description: str = ""


class ProposedThread(BaseModel):
    # the plot/conflict at stake — SOFT: never merged on similarity (two similar plots may
    # coexist); only an EXACT restatement of a known thread is dropped (D-51)
    stakes: str


class ProposedClaim(BaseModel):
    statement: str
    about: list[str] = Field(default_factory=list)
    provenance: Literal["narrator", "dialogue"] = "narrator"  # 'player' is not permitted
    speaker: str | None = None
    contradicts: list[str] = Field(default_factory=list)
    confidence: float | None = None
    durable: bool = True  # false = flavor/atmosphere; the gauntlet drops it (kept only as a
    #                       structural safety net — the prompt should keep flavor out entirely)


class Extraction(BaseModel):
    actors: list[ProposedActor] = Field(default_factory=list)
    places: list[ProposedPlace] = Field(default_factory=list)  # emergent locations (D-49)
    factions: list[ProposedFaction] = Field(default_factory=list)  # emergent factions (D-50)
    threads: list[ProposedThread] = Field(default_factory=list)  # emergent plots (D-50; soft)
    claims: list[ProposedClaim] = Field(default_factory=list)
    # ids of KNOWN threads the narration SETTLES (D-52) — the gauntlet resolves only an active/
    # offered EMERGENT one; authored plots stay under author/rule control.
    resolves: list[str] = Field(default_factory=list)


# Entity-name canonicalization: fold case/whitespace and strip a leading article, so an actor
# extracted once as "the woman" and later mentioned as "woman" (or "The Duke"/"the Duke") resolves
# to ONE entity instead of splitting (found live). The `\s+` guards partial words ("another",
# "theater" are untouched). Mirrors the SQL in find_actor_by_name; kept safe/conservative —
# semantic matches ("hooded stranger" ≈ "stranger") are the embedding entity_index's job (OQ-3).
_ARTICLE_RE = re.compile(r"^(the|a|an)\s+")


def canonical_name(name: str) -> str:
    return _ARTICLE_RE.sub("", " ".join(name.strip().lower().split()))


def _name_token(name: str) -> str:
    return f"name:{canonical_name(name)}"


def _statement_key(text: str) -> str:
    """Exact-restatement key (D-51): case, inner whitespace, and trailing punctuation folded —
    "Tavi has a bracelet." == "tavi  has a bracelet" — but any change of WORDS is a new key."""
    return " ".join(text.lower().split()).rstrip(" .!?;:")


def _is_role_noun(name: str) -> bool:
    """A description, not a name (D-51): all-lowercase once a leading article is stripped —
    "the guard", "tallest hooded stranger". A capitalized title ("the Duke") reads as a name, and a
    script without letter case can't be judged, so both pass (`islower` needs a cased letter)."""
    return re.sub(r"^(the|a|an)\s+", "", name.strip(), flags=re.IGNORECASE).islower()


# Partial-name resolution (D-56, tightened by D-58): articles carry no identity anywhere; a personal
# honorific only does as a LEADING title ("Brother Aldo" is a title, "Aldo's brother" a relation).
# Organisation words ("House", "Order") and "Old"/"Young" are KEPT — they ARE identity ("Iron Order"
# vs "Iron Fist"; "Old Weck" vs "Young Weck").
_ARTICLES = frozenset({"the", "a", "an", "of"})
_HONORIFICS = frozenset(
    {
        "ser",
        "sir",
        "lady",
        "lord",
        "dame",
        "brother",
        "sister",
        "father",
        "mother",
        "master",
        "mistress",
        "captain",
        "doc",
        "doctor",
    }
)
# a possessive ("Mira's", "Thomas'") — straight or curly apostrophe (models emit both)
_POSSESSIVE = re.compile("[^\\W\\d_]['\u2019]s\\b|s['\u2019](?=\\s|$)", re.IGNORECASE)


class _Name(NamedTuple):
    words: frozenset[str]  # the distinctive words
    titles: frozenset[str]  # the leading honorifics


def _name_parts(name: str) -> _Name:
    """A name's distinctive words and leading titles: "Ser Aldren Voss" → ({aldren, voss}, {ser}),
    "the Veyrs" → ({veyr}, {}) — articles dropped, a trailing plural folded."""
    words: set[str] = set()
    titles: set[str] = set()
    leading = True
    for w in re.findall(r"[^\W\d_]+", name.lower()):  # letter runs, any script
        if len(w) < 2 or w in _ARTICLES:
            continue
        if leading and w in _HONORIFICS:
            titles.add(w)
            continue
        leading = False
        words.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return _Name(frozenset(words), frozenset(titles))


def _partial_match(mention: _Name, index: dict[str, list[_Name]]) -> str | None:
    """The ONE known entity whose name (or alias) words CONTAIN the mention's — "Aldren" →
    "Ser Aldren Voss" — and whose title, if both have one, agrees; None if there is none OR several.
    Subset direction only (D-58): a mention that ADDS words ("Vel Market", "Tobin's wife") names
    something else, so it never links to the shorter name."""
    if not mention.words:
        return None

    def fits(known: _Name) -> bool:
        if not known.words or not mention.words <= known.words:
            return False
        # different titles name different people ("Lady Crowe" is not "Lord Haldane Crowe")
        return not (mention.titles and known.titles and not mention.titles & known.titles)

    hits = {eid for eid, names in index.items() if any(fits(n) for n in names)}
    return next(iter(hits)) if len(hits) == 1 else None


def _claims_fail_closed(items: Any) -> Any:
    """A null on a claim's TRUST fields never takes the trusting default (D-58): a null `provenance`
    means the extractor didn't say whose words these are — dialogue if a speaker is named, else the
    claim is dropped — and a null `durable` means not durable. Defaults stay for keys left out."""
    if not isinstance(items, list):
        return items
    out = []
    for item in items:
        if isinstance(item, dict):
            if "provenance" in item and item["provenance"] is None:
                if not item.get("speaker"):
                    logger.warning("extractor: dropped a claim with no provenance: %.200r", item)
                    continue
                item = {**item, "provenance": "dialogue"}
            if "durable" in item and item["durable"] is None:
                item = {**item, "durable": False}
        out.append(item)
    return out


def build_extractor_messages(
    recall: RecallBundle, narration: str, *, env: PromptEnv | None = None
) -> list[Message]:
    known_actors = "\n".join(f"- {a.name} [{a.actor_id}]" for a in recall.actors) or "(none known)"
    known_places = "\n".join(f"- {p.name} [{p.place_id}]" for p in recall.places) or "(none)"
    known_factions = "\n".join(f"- {f.name} [{f.faction_id}]" for f in recall.factions) or "(none)"
    # Live play showed the extractor re-proposing the SAME plot every beat ("something beneath
    # the canoe", 8 times) because it never saw which threads already existed (D-51).
    known_threads = (
        "\n".join(f"- [{t.thread_id}] {t.stakes}" for t in recall.active_threads) or "(none)"
    )
    # What the characters on stage already BELIEVE is known too: the narrator is shown it (the war
    # story's rumor channel), so a retelling must read as old news here, or it becomes a fresh claim
    # its teller "believes" outright (live run, 2026-10-31).
    shown = {c.claim_id: c for c in [*recall.claims, *recall.belief_claims]}
    known_claims = (
        "\n".join(f"- [{c.claim_id}] ({c.truth}) {c.statement}" for c in shown.values()) or "(none)"
    )
    user = (
        f"KNOWN ACTORS:\n{known_actors}\n\nKNOWN PLACES:\n{known_places}\n\n"
        f"KNOWN FACTIONS:\n{known_factions}\n\nKNOWN THREADS:\n{known_threads}\n\n"
        f"KNOWN CLAIMS:\n{known_claims}\n\nNARRATION:\n{narration}\n\n"
        "Return JSON (omit any key you have no value for — never null): {"
        '"actors": [{"name", "role", "member_of": faction name (optional), '
        '"located_in": place name (optional)}], '
        '"places": [{"name", "description", "parent": larger place name (optional)}], '
        '"factions": [{"name", "description"}], "threads": [{"stakes"}], '
        '"resolves": [ids of KNOWN THREADS this narration settles], '
        '"claims": [{"statement", "about": [names], "provenance": "narrator"|"dialogue", '
        '"speaker": name (dialogue only), "contradicts": [known claim ids], '
        '"durable": false for any passing moment, gesture, mood, or sensation / true for a '
        "lasting fact, or news or a plan a character reports, "
        '"confidence": 0..1, how sure the SPEAKER sounds}]}'
    )
    system = (env or DEFAULT_ENV).render("extractor.system.j2")
    return [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]


def parse_extraction(raw: str) -> Extraction | None:
    """Parse an extractor response into a validated Extraction, or None if it holds no JSON
    object (the engine re-asks on None).

    Salvages per ITEM (D-51): validating the whole object at once let ONE bad field — a
    `"member_of": null` a model writes for an "(optional)" key — silently discard every actor,
    place, and claim in the beat. Now a null takes the field's default and a malformed item is
    dropped on its own (logged), so the rest of the beat's state survives.
    """
    text = raw.strip()
    for candidate in (text, _slice_json_object(text)):
        if candidate is None:
            continue
        # Best-effort parse: a bad extraction must never crash the beat and lose the
        # narration (JSONDecodeError, RecursionError on deep nesting, …).
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return Extraction(
                actors=_salvage(data, "actors", ProposedActor),
                places=_salvage(data, "places", ProposedPlace),
                factions=_salvage(data, "factions", ProposedFaction),
                threads=_salvage(data, "threads", ProposedThread),
                claims=_salvage(
                    {"claims": _claims_fail_closed(data.get("claims"))}, "claims", ProposedClaim
                ),
                resolves=_salvage_ids(data, "resolves"),
            )
    return None


def _salvage[M: BaseModel](data: dict[str, Any], key: str, model: type[M]) -> list[M]:
    """Validate `data[key]` item by item: nulls fall back to defaults, a bad item is dropped."""
    raw_items = data.get(key)
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        logger.warning("extractor: '%s' is not a list; ignored", key)
        return []
    kept: list[M] = []
    for item in raw_items:
        if isinstance(item, dict):  # a null means "no value" — let the field default apply
            item = {
                k: [x for x in v if x is not None] if isinstance(v, list) else v
                for k, v in item.items()
                if v is not None
            }
        try:
            kept.append(model.model_validate(item))
        except Exception:  # ValidationError, or anything odd — a bad item never crashes the beat
            # what was dropped, not just how many — so a lost fact can be diagnosed later
            logger.warning("extractor: dropped a malformed '%s' item: %.200r", key, item)
    return kept


def _salvage_ids(data: dict[str, Any], key: str) -> list[str]:
    """`data[key]` as a list of id strings — anything else in it is dropped (D-52)."""
    raw = data.get(key)
    return [r for r in raw if isinstance(r, str)] if isinstance(raw, list) else []


def _slice_json_object(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else None


async def run_gauntlet(
    store: ProjectionQueries,
    branch_id: str,
    extraction: Extraction,
    *,
    policy: ExtractionPolicy | None = None,
) -> list[DomainEvent]:
    """Validate proposals into committable events (docs/13). Downgrade-or-drop only.

    `policy` gates which EMERGENT categories may be created (D-49) — a disabled category is silently
    skipped (structural: the extractor may propose it, but nothing commits).

    Emergent world is RELATIONAL (D-50): an actor carries its affiliations (a faction it's
    `member_of`, a place it's `located_in`). Resolving an actor CASCADE-creates the faction/place if
    they don't exist and wires the `member_of`/`located_in` edges; a place carries a `parent`
    (`located_in`) the same way. Identity is (name + affiliation), so two same-named actors of
    DIFFERENT houses stay DISTINCT — a soft, relationship-aware dedup, not a blunt name-merge (an
    affiliation the proposal states but an existing same-name actor lacks just ENRICHES that actor).
    Factions dedup by name; threads never merge on similarity (two similar plots may legitimately
    coexist) — only an EXACT restatement of an existing thread is dropped (D-51).

    Claims (D-51): one whose wording (`_statement_key`), subjects, AND truth all match an existing
    claim is a restatement and commits nothing new — a dialogue restatement still records the
    speaker's belief, in the EXISTING claim. A narrator confirming a rumor differs in truth, so it
    still commits.
    """
    policy = policy or ExtractionPolicy()
    events: list[DomainEvent] = []

    # Pre-load existing entities once (branches are small) for cross-beat resolution + dedup.
    places = await store.list_places(branch_id)
    factions = await store.list_factions(branch_id)
    places_by_key = {canonical_name(p.name): p.place_id for p in places}
    factions_by_key = {canonical_name(f.name): f.faction_id for f in factions}
    # D-56 partial-name indices: entity id → the word sets of its name (+ aliases)
    place_words = {p.place_id: [_name_parts(p.name)] for p in places}
    faction_words = {f.faction_id: [_name_parts(f.name)] for f in factions}
    actor_words: dict[str, list[_Name]] = {}
    actors_by_key: dict[str, list[str]] = {}  # canonical name OR alias → actor ids (may be several)
    for a in await store.list_actors(branch_id):
        # Index by name AND aliases — a pack-authored "Mera" [alias "the barkeep"] must resolve when
        # later mentioned only by the alias, or she'd split into a duplicate (review HIGH; the
        # relational rewrite dropped find_actor_by_name, which matched aliases).
        for k in {canonical_name(a.name), *(canonical_name(al) for al in a.aliases)}:
            if k:
                actors_by_key.setdefault(k, []).append(a.actor_id)
        actor_words[a.actor_id] = [_name_parts(n) for n in (a.name, *a.aliases)]
    aff: dict[str, dict[str, str]] = {}  # actor_id → {"member_of"|"located_in": dst id}
    for e in await store.list_edges(branch_id):
        if e.rel_type in ("member_of", "located_in"):
            aff.setdefault(e.src, {})[e.rel_type] = e.dst
    name_to_ref: dict[str, str] = {}  # canonical actor name (this beat) → id, for claim subjects

    def resolve_faction(name: str, *, description: str = "") -> str | None:
        key = canonical_name(name)
        if not key:
            return None
        if key in factions_by_key:
            return factions_by_key[key]
        known = _partial_match(_name_parts(name), faction_words)  # D-56: "the Veyrs" → House Veyr
        if known is not None:
            factions_by_key[key] = known
            return known
        if not policy.extract_factions:
            return None
        fid = f"f:{new_id()}"
        events.append(
            faction_created(faction_id=fid, name=name.strip(), description=description.strip())
        )
        factions_by_key[key] = fid
        faction_words[fid] = [_name_parts(name)]
        return fid

    def resolve_place(name: str, *, description: str = "", parent: str = "") -> str | None:
        key = canonical_name(name)
        if not key:
            return None
        if key in places_by_key:
            return places_by_key[key]
        known = _partial_match(_name_parts(name), place_words)  # "the Lantern" → Gilded Lantern
        if known is not None:
            places_by_key[key] = known
            return known
        if not policy.extract_places:
            return None
        pid = f"p:{new_id()}"
        events.append(
            place_created(place_id=pid, name=name.strip(), description=description.strip())
        )
        places_by_key[key] = pid
        place_words[pid] = [_name_parts(name)]
        if parent.strip():  # cascade the containing place + a located_in edge
            parent_id = resolve_place(parent)
            if parent_id is not None and parent_id != pid:
                events.append(edge_added(src=pid, rel_type="located_in", dst=parent_id))
        return pid

    def _enrich(actor_id: str, rel: str, dst: str | None) -> None:
        # Attach an affiliation the proposal states but the (linked) actor lacks — graph enrichment.
        if dst is not None and aff.get(actor_id, {}).get(rel) is None:
            events.append(edge_added(src=actor_id, rel_type=rel, dst=dst))
            aff.setdefault(actor_id, {})[rel] = dst

    def resolve_actor(
        name: str, *, role: str = "", member_of: str = "", located_in: str = ""
    ) -> str | None:
        key = canonical_name(name)
        if not key:
            return None
        # Gate affiliation resolution by the target policy AT THE CALL SITE, so a disabled
        # category wires NO edge even to a PRE-EXISTING faction/place (resolve_* returns an existing
        # id before its own gate; without this, factions-off still member_of'd an authored — C1).
        fid = (
            resolve_faction(member_of) if (member_of.strip() and policy.extract_factions) else None
        )
        pid = resolve_place(located_in) if (located_in.strip() and policy.extract_places) else None
        # Relationship-aware dedup: link to a same-name actor whose affiliations DON'T CONFLICT
        # (None = unknown = compatible); a same-name actor of a different house/place is DISTINCT.
        for cand in actors_by_key.get(key, []):
            ca = aff.get(cand, {})
            if (fid is None or ca.get("member_of") in (None, fid)) and (
                pid is None or ca.get("located_in") in (None, pid)
            ):
                _enrich(cand, "member_of", fid)
                _enrich(cand, "located_in", pid)
                name_to_ref[key] = cand
                return cand
        # D-56: no actor has this exact name — a PARTIAL name ("Aldren", "Captain Tobin") links to
        # the ONE known actor whose name words contain it, if affiliations agree, and records the
        # mention as an alias. Never for a role noun, and never when an exact name exists (a
        # same-name actor of a different house is distinct — D-50 — not a partial match).
        partial = (
            None
            if key in actors_by_key or _is_role_noun(name) or _POSSESSIVE.search(name)
            else _partial_match(_name_parts(name), actor_words)
        )
        if partial is not None:
            ca = aff.get(partial, {})
            if (fid is None or ca.get("member_of") in (None, fid)) and (
                pid is None or ca.get("located_in") in (None, pid)
            ):
                events.append(entity_alias_added(entity_id=partial, alias=name.strip()))
                actors_by_key[key] = [partial]
                actor_words[partial].append(_name_parts(name))
                _enrich(partial, "member_of", fid)
                _enrich(partial, "located_in", pid)
                name_to_ref[key] = partial
                return partial
        if not policy.extract_actors:
            return None
        # D-51: never MINT an identity from a role ("the guard") — under D-50 it becomes a name key
        # every later guard merges into, collecting their affiliations. Linking above is unaffected,
        # so an existing actor known by that alias still resolves; facts about the figure still
        # record as claims (subject = a name token), but an unnamed speaker gets no belief.
        if _is_role_noun(name):
            return None
        aid = f"a:{new_id()}"
        events.append(actor_created(actor_id=aid, name=name.strip(), tier=1, role=role.strip()))
        actors_by_key.setdefault(key, []).append(aid)
        actor_words[aid] = [_name_parts(name)]
        name_to_ref[key] = aid
        _enrich(aid, "member_of", fid)
        _enrich(aid, "located_in", pid)
        return aid

    def link_actor(name: str) -> str | None:
        # Resolve a claim SUBJECT to an existing actor (this beat or prior) — never creates.
        key = canonical_name(name)
        if not key:
            return None
        if key in name_to_ref:
            return name_to_ref[key]
        ids = actors_by_key.get(key)
        if ids:
            return ids[0]
        if _is_role_noun(name) or _POSSESSIVE.search(name):  # "Mira's mother" is not Mira (D-58)
            return None
        return _partial_match(_name_parts(name), actor_words)

    for pa in extraction.actors:
        resolve_actor(pa.name, role=pa.role, member_of=pa.member_of, located_in=pa.located_in)
    for pp in extraction.places:
        resolve_place(pp.name, description=pp.description, parent=pp.parent)
    for pf in extraction.factions:
        resolve_faction(pf.name, description=pf.description)
    if policy.extract_threads:
        threads = await store.list_threads(branch_id)
        # Only LIVE threads (D-58): a plot that ended may legitimately recur in the same words.
        known_stakes = {
            _statement_key(t.stakes) for t in threads if t.state not in ("resolved", "dead")
        }
        for pt in extraction.threads:
            stakes = pt.stakes.strip()
            if stakes and _statement_key(stakes) not in known_stakes:
                known_stakes.add(_statement_key(stakes))  # a same-beat repeat collapses too
                events.append(
                    thread_created(
                        thread_id=f"t:{new_id()}",
                        stakes=stakes,
                        state="active",
                        provenance="emergent",
                    )
                )
        # Resolution from play (D-52): the one MUTATION the extractor may propose — a KNOWN thread
        # the narration settles goes to `resolved`. Only an EMERGENT thread (play made it, play may
        # end it) that is still live; an authored plot can drive pack rules (`thread_state`
        # conditions), so its lifecycle stays with the author and the Reaction Layer.
        live_emergent = {
            t.thread_id: t.state
            for t in threads
            if t.provenance == "emergent" and t.state in ("active", "offered")
        }
        for tid in dict.fromkeys(extraction.resolves):  # dedup, keep order
            if tid in live_emergent:
                events.append(
                    thread_state_changed(
                        thread_id=tid, to_state="resolved", from_state=live_emergent[tid]
                    )
                )

    if not policy.extract_claims:
        return (
            events  # claims/beliefs disabled → the entity graph only (the engine disclaims recall)
        )

    # Pre-mint dialogue speakers so a claim *about* a speaker links to the same actor_id the belief
    # uses, not a divergent name-token.
    for pc in extraction.claims:
        if pc.durable and pc.provenance == "dialogue" and pc.speaker and pc.speaker.strip():
            resolve_actor(pc.speaker)

    # Restatement guard (D-51): live play re-proposed a KNOWN claim verbatim, beat after beat
    # ("Tavi has a bracelet.", 4 times) — it was in KNOWN CLAIMS and repeated anyway.
    # Module rumors (origin=module) are left out (D-58): an in-world claim must never be absorbed
    # into one — `expire_claims` can retract a module rumor, and would take the claim with it.
    claim_ids: dict[tuple[str, frozenset[str], str], str] = {
        (_statement_key(c.statement), frozenset(c.subject_refs), c.truth): c.claim_id
        for c in await store.list_claims(branch_id)
        if c.origin != "module"
    }
    believed: dict[str, set[str]] = {}  # actor → claim ids they already hold a belief in

    async def holds(actor_id: str, claim_id: str) -> bool:
        if actor_id not in believed:
            believed[actor_id] = {b.claim_id for b in await store.beliefs_of(branch_id, actor_id)}
        return claim_id in believed[actor_id]

    for pc in extraction.claims:
        if not pc.durable:
            continue  # flavor / atmosphere — not canon (docs/05 promotion rules)
        statement = pc.statement.strip()
        if not statement:
            continue
        subject_refs = [(link_actor(s) or _name_token(s)) for s in pc.about if s.strip()]
        truth: Truth = "true" if pc.provenance == "narrator" else "unknown"
        if truth == "true":
            for cid in pc.contradicts:
                existing = await store.get_claim(branch_id, cid)
                if existing is not None and existing.truth == "true":
                    truth = "unknown"  # can't hold two contradictory truths
                    break

        key = (_statement_key(statement), frozenset(subject_refs), truth)
        existing_id = claim_ids.get(key)
        if existing_id is None and truth == "unknown":
            # A character repeating an established FACT (D-58): their belief attaches to the fact,
            # rather than a duplicate unknown-truth claim of the same words.
            existing_id = claim_ids.get((key[0], key[1], "true"))
        if existing_id is not None:
            claim_id = existing_id  # a restatement: nothing new to record
        else:
            claim_id = f"c:{new_id()}"
            claim_ids[key] = claim_id  # a same-beat repeat collapses too
            events.append(
                claim_recorded(
                    claim_id=claim_id,
                    statement=statement,
                    subject_refs=subject_refs,
                    truth=truth,
                    origin=pc.provenance,
                )
            )

        if pc.provenance == "dialogue" and pc.speaker and pc.speaker.strip():
            speaker_ref = resolve_actor(pc.speaker)
            # A restatement never overwrites a belief the speaker ALREADY holds (D-58): that row may
            # carry a propagated rumor's decayed confidence and its `learned_from` chain (P5).
            if speaker_ref is not None and not (
                existing_id is not None and await holds(speaker_ref, claim_id)
            ):
                believed.setdefault(speaker_ref, set()).add(claim_id)
                confidence = (
                    pc.confidence
                    if (pc.confidence is not None and 0.0 <= pc.confidence <= 1.0)
                    else _DEFAULT_BELIEF_CONFIDENCE
                )
                events.append(
                    belief_changed(actor_id=speaker_ref, claim_id=claim_id, confidence=confidence)
                )

    return events
