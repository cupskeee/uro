# examples/games — builder briefs (Uro as a forcing function)

These are **not** finished games. Each folder is a **brief** — a `system_prompt.md` + `TASK.md`
pair written to be handed to a powerful LLM ("the builder"), which then builds the game as a real,
running consumer of the Uro engine.

Their real purpose is **scientific, not recreational**: each game is engineered to slam into a
different cluster of Uro's *deferred / by-policy / unproven* work, so its required **`GAP_REPORT.md`**
becomes an evidence-backed engine backlog item. We stop guessing which leftover work matters and let
real consumers show us where the engine bends or breaks.

Read [`URO_INTEGRATION.md`](URO_INTEGRATION.md) first — the verified engine surface every brief is
written against (and whose "what Uro does NOT have yet" section is the stress-target list). The
runnable reference consumer is [`../hello_uro/hello_uro.py`](../hello_uro/hello_uro.py).

| Game | Posture | Forcing function → the leftover work it stresses |
|---|---|---|
| [`ironwake/`](ironwake/) | Chronicler | The full Chronicler ingestion contract (OQ-12): self-attested scope / no parked-encounter registry, game↔world time mapping, the protection ceiling *as a contract to learn*, rumor propagation + decay, the missing REST read surface |
| [`sable-court/`](sable-court/) | GM + Reaction Layer | The **declarative computation ceiling** → a **refusal log** that is the evidence gate for the reserved WASM scripting tier (D-33 Stage B); OQ-8 off-screen sim; place-state recall; entity resolution at scale |
| [`seventh-vault/`](seventh-vault/) | Server (WS), 2–4p | PartyArbiter beyond round-robin (proposal/consensus/PvP, OQ-7), party co-combat, PC-anchored recall, session lifecycle, the missing REST management surface |
| [`hollowloop/`](hollowloop/) | GM, embed | Branching/materialization **at scale** (snapshot cadence, fork-from-past, memory-carry-across-forks, marker/branch ergonomics → store-swap pressure) — the meteor test as a whole game |

Each brief demands the same three deliverables from its builder: the **game** (runnable with the
deterministic `stub` provider, no API key), a **`GAP_REPORT.md`** in the standardized format, and a
short **`README.md`**. The builder must **consume `uro_core` / `uro-server`, never modify them** —
and must **log, not invent**, any capability Uro lacks.
