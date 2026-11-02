"""The days a player's intent passes (D-77): the clock follows play, read from what the player says
they do — never from the narration, never from an LLM.

Measured on the 146-beat long run: the fiction crossed a day on 7 beats (8 days), and every one
started in the intent ("I sleep until morning", "the journey takes a day and a night", "I rest for
two days"); the narration reached a new day on only 5 of the 7 (one beat ends asleep, one "before
dawn"). So the intent is the signal, read by a closed, conservative grammar:

- a first-person sentence ("I…", "we…", "then I…", "Tonight I…"; quoted speech stripped) that
  rests (sleep, rest, camp, recover, stay, wait, spend…) or travels (ride, march, sail, walk,
  follow, cross…) overnight ("until morning", "through the night", "spend the night", "stay the
  night", "for the next day", "wake at dawn") → 1;
- the same verbs for a stated duration ("for two days", "over the next couple of days", "takes a
  day", "a day and a night") → N. A number of days that dates or describes something ("camped two
  days before", "a day's wages", "a day old") is not a duration;
- a sentence that asks, tells, says, shouts, whispers, hears or reads, promises, swears, tries,
  pretends, plans, wishes, wonders, remembers, watches or negates passes nothing, and so does a
  question, a past tense ("slept", "yesterday") and someone else's night ("I watch the guard sleep
  until morning");
- start-time adverbs alone ("at first light") and same-day phrases ("until nightfall", "the whole
  day") pass no day — the long run's eighth day ("I leave at first light") is one, so the clock
  trails such a fiction by a day; more than 3 days is left to an explicit time-skip.

The result is the number of whole days to add, 0..3. The engine computes the new day from it.
English only (a non-English pack's players pass no day this way; an explicit skip still works).
The labelled fixture `tests/fixtures/day_cues.json` gates it in CI.
"""

from __future__ import annotations

import re

MAX_DAYS = 3  # a longer stretch is a time-skip's job, not a beat's

# Quoted speech: double, curly and guillemet quotes; curly single quotes; and an ASCII single-quoted
# span that opens at the start or after a space, colon or bracket and closes before a space or
# punctuation (so "the inn's" and "'til dawn" are not quotes). Applied before curly apostrophes
# become straight ones.
_QUOTED = re.compile(
    r'"[^"]*"?|“[^”]*”?|«[^»]*»?|‘[^’]*’?|'  # noqa: RUF001 — the quote characters themselves
    r"(?:^|(?<=[\s:(]))'[^']*'(?=[\s.,!?;:)]|$)"
)
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")
# the subject opens the sentence, after at most a conjunction or a short leading phrase
# ("Tonight I…", "Exhausted, I…", "After the fight, we…")
_FIRST_PERSON = re.compile(
    r"^(?:(?:and|but|so|then|after that|tonight|today|finally|later|instead)\s*,?\s+|"
    r"[\w' -]{1,30},\s+)?(?:i|we)\b"
)
_NOT_DOING = re.compile(
    r"\b(?:ask|asks|asking|tell|tells|telling|say|says|saying|promise|promises|swear|swears|"
    r"try|tries|trying|attempt|attempts|attempting|pretend|pretends|pretending|wonder|wonders|"
    r"plan|plans|planning|intend|intends|offer|offers|refuse|refuses|not|never|cannot|can't|"
    r"don't|won't|wouldn't|shouldn't|if|watch|watches|watching|observe|observes|"
    # wishes and maybes, not acts
    r"wish|wishes|could|would|should|might|hope|hopes|think|thinks|suppose|consider|considers|"
    # someone else's words, or the player's own spoken ones
    r"shout|shouts|yell|yells|whisper|whispers|mutter|mutters|murmur|murmurs|announce|announces|"
    r"call out|calls out|reply|replies|explain|explains|suggest|suggests|hear|hears|overhear|"
    r"overhears|listen|listens|read|reads|sing|sings|"
    # what already happened
    r"remember|remembers|recall|recalls|yesterday|last night|ago|earlier|used to)\b"
)
_REST_VERB = re.compile(  # present tense: "slept" reports the past
    r"\b(?:sleep|sleeps|rest|rests|resting|camp|camps|camping|doze|dozes|bed down|"
    r"turn in|lie down|recuperate|recuperates|recover|recovers|recovering|heal|heals|lie up|"
    r"holed up|hole up|stay|stays|remain|remains|spend|spends|wait|waits|waiting|lie low|"
    r"lies low)\b"
)
_TRAVEL_VERB = re.compile(
    r"\b(?:travel|travels|travelling|traveling|ride|rides|riding|journey|journeys|march|marches|"
    r"sail|sails|walk|walks|trek|treks|hike|hikes|row|rows|drive|drives|set out|sets out|head|"
    r"heads|press on|presses on|push on|pushes on|keep going|keeps going|continue|continues|"
    r"follow|follows|cross|crosses|urge|urges|make my way|make our way)\b"
)
_OVERNIGHT = re.compile(
    r"\b(?:until|till|til) (?:the )?(?:morning|dawn|daybreak|first light|sunrise|daylight)\b|"
    r"\b(?:through|throughout) the night\b|\bthe rest of the night\b|\bovernight\b|"
    r"\bthe night through\b|\buntil the (?:next|following) (?:morning|day)\b|\bfor the night\b|"
    r"\b(?:spend|spends|stay|stays|remain|remains) the night\b|\b(?:until|till) tomorrow\b|"
    r"\binto the next day\b|\b(?:spend|spends) the (?:next|following) day\b|"
    r"\b(?:wake|wakes|waking) (?:up )?(?:at|with|by) (?:the )?"
    r"(?:sunrise|dawn|first light|daybreak)\b|"
    r"\b(?:for|through|throughout|over) the (?:next|following) day\b|"
    r"\banother night\b"
)
_NUMBERS = {"a": 1, "one": 1, "two": 2, "a couple of": 2, "a couple": 2, "couple of": 2,
            "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "a few": 3}  # fmt: skip
# A number of days counts only as a duration ("for two days", "over the next two days", "takes a
# day", "another two days"), never as a date or an adjective ("camped two days before", "a day's
# wages", "a day old").
_N_DAYS = re.compile(
    r"\b(?:(?:for|over|after|within|take|takes|taking|took|last|lasts|spend|spends|spending)\s+"
    r"(?:(?:the )?(?:next|following) )?|(?:the )?(?:next|following) |another )"
    r"(a couple of|a couple|couple of|a few|a|one|two|three|four|five|six|seven|[1-9])\s+"
    r"(?:whole |full |long )?days?\b(?!['’]|\s+(?:ago|earlier|old))"  # noqa: RUF001
)
_DAY_AND_NIGHT = re.compile(r"\ba day and a night\b|\bday and night\b|\bnight and a day\b")


def _days_in(clause: str) -> int:
    """Whole days one first-person sentence passes (0 if it passes none, or more than MAX_DAYS).
    A whole sentence, not a clause: "I ride back; the journey takes a day and a night" puts the
    duration after a semicolon, and "I eat, then sleep until morning" drops the subject."""
    if not _FIRST_PERSON.search(clause) or _NOT_DOING.search(clause):
        return 0
    rests, travels = bool(_REST_VERB.search(clause)), bool(_TRAVEL_VERB.search(clause))
    if not (rests or travels):
        return 0
    days = 0
    m = _N_DAYS.search(clause)
    if m:
        word = m.group(1)
        days = int(word) if word.isdigit() else _NUMBERS.get(word, 0)
    elif _DAY_AND_NIGHT.search(clause):
        days = 1
    elif _OVERNIGHT.search(clause):
        days = 1  # a night or the next day passes, resting or on the road
    return days if 0 < days <= MAX_DAYS else 0


def day_cue(intent: str) -> tuple[int, str]:
    """The whole in-fiction days a player's intent passes, 0..MAX_DAYS, and the sentence that
    passes them (the evidence). One beat is one stretch of time, not a sum: the largest of its
    first-person sentences counts."""
    text = _QUOTED.sub(" ", intent).replace("\u2019", "'")
    best, evidence = 0, ""
    for match in _SENTENCE.finditer(text):
        sentence = match.group(0)
        if sentence.rstrip().endswith("?"):
            continue  # a question passes no time
        sentence = sentence.rstrip(" .!")
        days = _days_in(sentence.lower().strip(" ,"))
        if days > best:
            best, evidence = days, sentence.strip()
    return best, evidence


def parse_day_cue(intent: str) -> int:
    """The whole in-fiction days a player's intent passes, 0..MAX_DAYS."""
    return day_cue(intent)[0]
