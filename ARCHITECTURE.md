# Gary — AI Game Master

**System architecture & technical specification, v3 (Dungeon World)**
Google Gemini (narrative and GM judgement) + Gary (rules, dice and state), over Discord.

Gary runs **Dungeon World**. Gemini writes the fiction, names the moves the fiction
triggers and makes the GM's moves. Gary owns everything with a number in it: it rolls
the dice when a player clicks, resolves each move by its band, keeps every sheet and
creature, and tells the model what happened as fact. The book's text is extracted from
the authors' InDesign XML ([Sagelt/Dungeon-World](https://github.com/Sagelt/Dungeon-World),
CC-BY 3.0); the rules structure built on it is hand-written.

How to run it and the slash commands are in [`README.md`](README.md); how each rule plays at
the table, and what stays with the GM, in [`RULES.md`](RULES.md). This document is how it is
built and why.

> **History.** v1–v2 ran D&D 5e (2024) with [Avrae](https://avrae.io) rolling dice and
> holding sheets, and Gary parsing Avrae's embeds to resolve checks. Avrae was removed on
> the `dungeon-world` branch: it caused most of the complexity and the open risks
> (parsing a format that was not an API, correlating rolls to requests by character name,
> a copy-paste round trip on every check). That design is in git history before commit
> `bdbb72f`. Old journals replay: Avrae-era entries are skipped or replay as no-ops.

---

## 1. Platform constraints

**Discord**

- **Message Content is a privileged intent.** Gary reads player prose. Enable
  `MESSAGE CONTENT INTENT` in the Developer Portal *and* set `intents.message_content =
  True` in code; with only one of the two, `message.content` arrives empty and Gary looks
  alive while answering nobody. `GaryClient.on_ready` checks and complains. Free below 100
  guilds; Gary is a single-table tool, so the ceiling is acceptable.
- **Components are how players act.** Rolls, choices, damage, spell picks and character
  creation are buttons, selects and modals. A click carries the clicking user exactly,
  which is the attribution the whole design rests on (§5). A typed command cannot be
  answered privately; a click can, which is why every private form opens from a button.
- **Never use a user token.** Self-botting violates Discord's ToS. Non-negotiable.
- **Rate limits.** 5 messages per 5 seconds per channel; 2000 characters per message.
  Gemini prose routinely exceeds the latter. Components: 5 rows, 5 buttons a row, 25
  options in a select, 80-character button labels, 100-character option labels.
- **Interactions must be answered within three seconds.** Narration takes longer, so a
  click is acknowledged first (the button disabled or the options cleared) and the dice
  and narration follow as ordinary messages.

**Gemini**

- API key auth; persona and tools are passed per request in `GenerateContentConfig`.
- Subject to quota and transient 5xx: every call has a timeout, a retry policy, a fallback
  model and a player-visible failure mode (§10).

---

## 2. Architecture

```
                        ┌────────────────────────┐
                        │      Discord Chat      │
                        │ players: prose + clicks│
                        └──────────┬─────────────┘
                                   │
                        Gateway WebSocket (persistent)
                        + Message Content intent
                                   │
                                   ▼
              ┌──────────────────────────────────────┐
              │                GARY                  │
              │  ┌────────────────────────────────┐  │
              │  │ Routing     → who is addressed │  │
              │  │ Director    → moves, dice, HP  │  │
              │  │ Registry    → pending moves    │  │
              │  │ Rules       → moves, sheets,   │  │
              │  │               spells, monsters │  │
              │  │ Context     → prompt assembly  │  │
              │  │ Forms       → private choices  │  │
              │  └────────────────────────────────┘  │
              └────────┬───────────────────┬─────────┘
                       │                   │ in-process
              Google GenAI API             │
                       │                   ▼
                       ▼          ┌──────────────────────┐
              ┌──────────────┐    │  Campaign Store      │
              │  Gemini API  │    │  journal + snapshot  │
              │   (cloud)    │    │  one writer, locked  │
              └──────────────┘    └──────────────────────┘
```

**Components**

- **Players** — write what their characters do, and click: Roll, an option, Roll damage,
  the private forms for their sheet.
- **Gary** — the only component that decides outcomes. Owns the Director, the pending-move
  registry, the dice, every sheet and creature, prompt assembly and all writes to state.
- **Gemini** — writes prose, voices NPCs, names the move a player's action triggers, and
  makes GM moves (a creature attacks, something approaches the camp, Death's bargain). **It
  narrates outcomes; it does not determine them.**
- **Campaign store** — in-process, journalled source of truth for sheets, creatures, world
  time, scene, NPCs, notes and effects.

### 2.1 Why there is no second client

Gary uses `CampaignStore` in-process. An MCP server on the other side of the store was
built and removed (2026-08-22): it could not be tested on the interpreter the repo then
used, needed a second one, and was not really beside Gary anyway, since the campaign lock
(§7.6) makes the bot and a prep client exclusive. Between-session prep is still worth
having; the next attempt should be a CLI over `CampaignStore`, testable like everything
else.

---

## 3. Authority model — who owns which truth

Every fact has one owner. There are no shared-write facts.

| Fact | Owner | Notes |
|---|---|---|
| Dice | **Gary** | The `Dice` port (`ports.py`): `RandomDice` in production, `ScriptedDice` in tests. The only source of randomness. Every roll is posted face by face. |
| Which move the fiction triggered | **Gemini** | `make_move`. For Defy Danger, which stat the approach uses. Whether an action triggers a move at all is GM judgement. |
| Stat, modifier, target, spell — fixed before the dice | **Gary** | From the sheet, at `make_move` time. Nothing the model writes afterwards changes them. §5. |
| What each result means | **Gary** | The band (10+/7–9/6−) and its text, written in `moves.py` before anything is rolled. |
| The player's choices after a roll | **The player** | Clicked. Never the model's. |
| Damage and HP — characters and creatures | **Gary** | Rolled from the sheet, the weapon, the spell or the bestiary; armor and piercing applied; journalled. |
| *Whether* a creature attacks on a miss | **Gemini** | A GM move: `monster_attacks`. *How much* is Gary's. |
| Character sheets, gear, coin, spells, XP, bonds | **Gary** | Created and changed through private forms and checked rules (§6). |
| Creature stats | **Gary** (bestiary) | 142 SRD creatures looked up; homebrew from the model, checked against the monster-making rules. |
| XP from End of Session | **The table** | `/end`: toggles anyone may flip; alignment and bond resolution are each player's own. |
| World date/time, scene, NPCs, notes, effects | **Store** | Written through the model's tools; reversible. |
| Position and distance | **Gemini** (nobody tracks it) | No map. The model keeps who-is-where in the scene summary and stays consistent with it. |
| Narrative prose, NPC voice, GM moves | **Gemini** | |

**Rest and time are split.** A night's rest is per character (rations, healing) and the
clock is per world. `make_camp` is one call for the whole party that moves the clock once.

---

## 4. The gameplay loop

### Mode A — Narrative

Freeform conversation, description, exploration that triggers no move. Player writes →
Gary assembles context (§8) → Gemini answers in prose, calling state tools as it goes
(record a clue, give an item, advance time).

### Mode B — A move

```
       player describes an action
                  │
                  ▼
   Gemini calls make_move(actor, move, stat?, target?, spell?, companion?)
                  │                                  ← model judgement ends here
                  ▼
   Director checks it and FIXES stat, modifier,     ← authority begins here
   target and spell; registry persists it
                  │
                  ▼
   "Kethra — Volley at Goblin 1, roll +Dex +2."  [Roll]
                  │
        ┌─────────┴──────────┐
        │                    │
  that player clicks     TTL expires (default 180s)
        │                    │
        ▼                    ▼
  Gary rolls 2d6+mod     move lapses; Gemini narrates
  and posts the dice     hesitation (rules-triggered
        │                moves are rolled for them)
        ▼
  band → choice? → [options]   damage? → [Roll damage]
        │
        ▼
  consequences applied (HP, ammo, spells, XP…), journalled
        │
        ▼
  Gemini narrates a FACT it was handed
```

### 4.1 Channel state is derived, not stored

There are two states, and the Director keeps neither in memory: `AWAITING_ROLL` if the
registry has an open move in the channel, else `IDLE`. An earlier four-state machine held
per channel diverged from the registry: a player typing "rolling now!" moved the channel
out of `AWAITING_ROLL` and the roll that followed was silently dropped. Deriving the state
removes the possibility, and it is what lets any process serve any channel (§11).

### 4.2 Multiplayer arbitration

- Gary answers a player message only in a registered game channel **and** when the message
  mentions Gary (as the user or its managed role), the player has a move open, or the
  channel is in freeform mode. A message in an unregistered channel is silence, not an
  error — logged, so a misconfigured channel can be told from a broken bot. Gary ignores
  its own messages and other bots'. The decision is pure (`adapters/routing.py`).
- One pending move per player. A new one supersedes the old, and the table is told.
- Several players may hold moves at once; each is keyed by Discord user, so this is safe.
- The spotlight is the model's: it resolves the first move-triggering action and addresses
  who acts next. Out-of-fiction turn order is a social problem, not a software one.
- **Turns serialise per channel.** discord.py dispatches concurrently; a per-channel lock
  in the narrator stops two turns loading the same history and the second save discarding
  the first. The second player's turn sees the first player's message and the reply.

---

## 5. The move protocol

The rule that makes the system correct:

> **A move is resolved only by the roll of the player it was asked of.**

Gary rolls the dice itself, when that player clicks. Nothing in the channel — another
player's text, a pasted number, a name — can stand in for the click, so there is nothing
to parse and nothing to correlate.

### 5.1 Gemini names the move; Gary fixes everything else

`make_move` is the only path from fiction to dice. The Director (`fsm.Director.open_move`)
refuses, back to the model as a `system_note`, anything the rules can see is wrong:

- a move that does not exist, or one the rules trigger themselves (Last Breath, Take
  Watch — `callable=False`);
- a character nobody at the table plays (it could never be rolled);
- a class move for the wrong class ("Backstab is the Thief's move");
- a character with no sheet, or one who is down or dead;
- an attack with no standing target in the scene, or no weapon whose range reaches;
- a spell the caster does not hold, has lost, or cannot cast while another lasts;
- Aid or Interfere with no other character named; Shapeshifter with no form;
- `companion=true` for a companion not trained for that move.

Otherwise the stat, its modifier (with Bless, casting penalties, the companion's cunning,
+Bond), the target and the spell are written to the registry **before** the player sees a
Roll button, and survive restarts, model changes of mind and context truncation.

```python
@dataclass(frozen=True)
class PendingCheck:
    check_id: str          # time-sortable id
    channel_id: int
    user_id: int           # the only Discord user who may roll it
    actor: str             # character name
    move: str              # key into moves.MOVES
    stat: str
    modifier: int          # fixed now, never revisited
    stakes: str
    dice: str              # "2d6"
    created_at: float
    expires_at: float
    faces: tuple = ()      # filled by the roll
    picks: tuple = ()      # filled by the player's choice
    target: str = ""       # creature(s), character, or form
    spell: str = ""        # Cast a Spell
    companion: str = ""    # Command
```

### 5.2 Resolution is a band

```python
def resolve(check, roll, picks=()) -> MoveOutcome:
    band = "strong" if roll.total >= 10 else "weak" if roll.total >= 7 else "miss"
    rules = MOVES[check.move].band(band)
    ...
```

`resolver.py` is deliberately boring: no model is consulted, so no model can rationalise a
total into the outcome it would rather narrate. The outcome reaches Gemini as a structured
**function response** — the band, its text, what the player chose, damage dealt and taken,
healing, the spell's effect — phrased as settled fact, with an instruction not to reverse
or soften it, to make a GM move on a miss, and to describe each wound at its severity (a
graze, a solid hit, a serious wound…, measured against max HP so 2 damage is not narrated
as a mortal blow to a 25-HP fighter).

### 5.3 The player acts by clicking

Each prompt is a `discord.ui.DynamicItem` whose custom id carries what it acts on —
`gary:roll:<check_id>`, `gary:pick:<check_id>:<n>`, `gary:choose:<check_id>:<move>:<band>`,
`gary:damage:<check_id>`, `gary:spells:<user_id>` — so discord.py rebuilds it from the id
on any click, and a button posted before a restart still works after one. Whether the
clicker may act is decided by the Director from who Discord says clicked. A click by
anyone else, or on a button left over from a superseded move, is answered privately and
does nothing.

### 5.4 A move can wait on its player more than once

After the dice, the move may need the player again: **a choice** (Volley's 7–9, Discern
Realities' questions, Backstab's two, Cast a Spell's 7–9) and then **damage** (a separate
Roll damage click, as the table chose: one click to hit, one to see how hard). The registry
`hold`s the move with its faces and picks, durably and with a fresh TTL, until each is in.
Pick-one choices are buttons; pick-several is a select taking exactly that many.

If the time runs out after the dice, the dice stand: the first options are taken, damage
is rolled for them, and the table is told. A move nobody rolled simply lapses — the moment
passed — and is narrated as hesitation, never as success.

### 5.5 The rules trigger some moves themselves

**Last Breath** (when a character hits 0 HP) and **Take Watch** (when something approaches
the camp) are opened by the Director for the character's player. They are rolled *for*
the player if nobody clicks, and resolve as `rules_event` prompts, which never answer a
pending `make_move` call — someone else's, perhaps. Last Breath's 7–9 has the model name
Death's specific bargain first; then the player gets Take/Refuse buttons.

### 5.6 A move is a deferred function call

When the model calls `make_move`, **the call is deliberately left unanswered.** The turn
ends with it open. Minutes later the player clicks, and the outcome is delivered as the
`function_response` to that original call. Three things fall out:

- The conversation stays well-formed: every call is paired with its response, in order.
- The outcome arrives over the **tool transport**, so its trust comes from the channel it
  travelled on, not from any string inside it (§9.1).
- The model cannot confuse a result with a player's *claim* about one.

Expiry answers the call with `attempted: false`; an invalid call answers it with an error
and the model retries. If history has been trimmed past the call, the adapter falls back
to a text part rather than an unpaired `function_response`, which Gemini rejects. A
`make_move` issued beside other calls ends the turn there; the rest are skipped, which the
persona warns about.

---

## 6. The rules engine

### 6.1 Text extracted, structure hand-written

`scripts/extract_dw_*.py` read the authors' XML into `src/gary/data/*.json`, each with its
attribution and source commit: classes (8), class moves (35 starting, 18 race, 157
advanced, bond lines and alignments), monsters (154; 142 with stats), equipment (45
items), spells (33 Cleric, 30 Wizard), magic items (39), and the Instant NPC tables (100
instincts, 100 knacks, names). The compendium classes (8) come from the backers' Player
Compendium PDF (CC-BY 3.0) via `pdftotext`, plus the core book's Divine Bond. The Barbarian, a
playbook released on its own (CC-BY 3.0), is transcribed by hand into `data/playbooks.json` and
merged in by `playbooks.py` wherever the book's classes load, so re-running the extractors
cannot wipe it. Every extractor fails loudly on anything it cannot
read, and the loaders raise at import if a file is missing, because an empty bestiary is
indistinguishable from "everything is homebrew".

What a move *does* — which option exposes you to an attack, which deals damage, how many
questions a hit buys — is hand-written in `moves.py`, `gear.py`, `spells.py`,
`companion.py`. There are few enough, and those are exactly the parts a parser gets wrong.
The XML's own quirks (the Druid's gear section has lost its group headings; Ranger's bond
div is `id="bond"`) are handled where they occur.

### 6.2 Moves

`moves.py`: the eight basic moves, Last Breath, Take Watch, and the rolled class moves
(Turn Undead, Lay on Hands, I Am the Law, Hunt and Track, Called Shot, Trap Expert, Tricks
of the Trade, Backstab, Bend Bars, Arcane Art, Shapeshifter, Cast a Spell). A `Move` has a
stat (or a sentinel: `ANY` for Defy Danger, `BOND`, `NOTHING`, `CASTER`), three `Band`s
and flags: `classes`, `ranged`, `heals_target`, `helps_target`, `names_form`. Bands and
options carry what they do: `deals_damage`, `enemy_attacks`, `damage_mod` (±1d6),
`spends_ammo`, `heals` (dice), `heal_transfers` (Lay on Hands' 7–9), `casts`,
`casting_penalty`, `loses_spell`. Called Shot and Arcane Art pick their effect after the
roll rather than before: the effects are the same at every result, so seeing the dice
first buys nothing.

Narrative class moves (no roll, or nothing Gary tracks) and advanced moves reach the model
every turn in the book's words, in a "class moves" block per character (`class_moves.py`).
A few touch a number Gary already runs and are wired where that number is: precise weapons
hack with Dex, a Halfling Thief deals +2 at range, Druids eat no rations, a Halfling Druid
adds 1d6 to camp healing. The other race moves with a number are wired behind a flag the
model sets when the fiction fits: `make_move(race_move=)` (`_RACE_MOVES`: Dwarf Fighter
parleys with Con; Halfling Fighter's Defy Danger and Human Thief's Spout Lore and Discern
Realities take +1), `perilous_journey(wilderness=)` (an Elf Ranger's job is a 10+ unrolled)
and `make_camp(dungeon_or_city=)` (a Human Ranger eats nothing). The Human Fighter's
once-per-battle reroll is a `reroll` reaction on a creature's hit; `reroll_used` holds the
creatures standing when it was spent, and it returns once none of them stands. The advanced moves with a number Gary runs live in `advanced.py`
(armor, damage dice, a shaped die, healing dice, spell-penalty relief, rotes) and are
called where that number is computed; `advanced.APPLIED` says, in the class-moves block,
what Gary applies of each so the model does not apply it twice. The eleven advanced moves
with a roll of their own are generated in `moves.py` (`ADVANCED_ROLLS`) from the book's text
-- trigger, stat, the three results, "choose two / choose one" as a `Choice` -- each with
`requires` (only someone who has the move, `advanced.has`, may make it). Their numbers are
wired by key: Metal Hurlant's `damage_die` (rolled in place of the class die, no weapon
needed), Communion of Whispers' -1 forward on a miss, Counterspell's staked spell (the check's
`spell`, which `_spell_of` ignores for anything but Cast a Spell) lost on a 7-9.

### 6.3 Sheets and creation

`sheets.py`: class, race, stats and modifiers, HP (class base + Constitution score), damage
die, armor, level, XP, alignment, gear, coin, rations, ammo — and, by class, spells,
companion, shape; bonds. `/new` opens a private form (`adapters/creator.py`) driving a
`Draft` that refuses at every step what the rules forbid: class, race, the
16/15/13/12/9/8 array one score at a time, starting gear from the class's lists (the
Fighter's signature weapon in full), alignment, the class's own choices (`choices.py`:
a Cleric's god, domain and precept; a Druid's land and tell; a Bard's lore; a Thief's
poison; a Fighter's weapon look and an Elf's precise weapon type, read from the move
texts), name. Creating a character also claims it
for the creator, in the same apply. (The commands are slash commands, `adapters/slash.py`:
each runs the internal command dispatcher (`GaryClient._on_dm_command`) through a stand-in
message built from the interaction -- author, channel, text, attachments -- deferring at once
and removing the placeholder after; `/new`, `/sheet`, `/fronts` and `/help` answer ephemerally
instead. They are synced per guild of the game channels on connect. A typed `?dm` message is
routed to a one-line pointer, `SLASH_ONLY`.) Everything else about a character opens from `/sheet`
(`GaryClient._sheet_hub`): `_sheet_actions` returns, per action, a button (its form built on
click, from the sheet as it is then) or why it does not apply -- spells, companion, bonds,
alignment, class choices, gear, level up -- and the sheet shows every button that applies.
Older sheets get their missing parts the same way, without a remake. The typed names
(`spells`, `levelup`...) remain dispatcher commands that post one button each, as do
`accept`, `cancel` and `stats`, reached through buttons and `/state` rather than typed.

Every form is **private** (ephemeral), opened from a public button only its owner can use,
and held in memory: a half-made choice is cheap to start again. Each save writes only what
the form changed onto the *current* sheet, so HP lost in a fight while a form was open is
not put back.

### 6.4 Gear, items, coin, healing and towns

`gear.py`: armor is the best worn plus shields; attack moves need a weapon whose range tags
reach (hand/close/reach for Hack and Slash, near/far/thrown for Volley, ammo for a bow);
its +damage and piercing join the damage roll. Found and bought items are looked up in the
equipment list (`gear.item_for` forgives articles, plurals on any word, curly quotes) and
carry their book stats; anything else is carried with no rules. `trade.py`: buy at book
price (refused if unaffordable), sell at half or less (the model may offer less, never
more), coin bounded per call. `healing.py`: a potion heals 10 and is gone; bandages heal
someone else 4 and poultices anyone 7, spending counted uses, and both are refused while
creatures stand; a potion's `cure` removes a debility instead. `sheets.clumsy` names a
clumsy item worn by anyone but a Fighter or Paladin (Armored), and every roll takes -1 for it.

**Treasure** (`treasure.py`, the `roll_treasure` tool, handled in `gemini.py` beside the
trade tools). The creature's damage die -- a scene instance, or a bestiary kind for a lair --
modified by its bestiary tags (hoarder, magical, divine, planar) and the model's flags (far
from home, lord, ancient), reads the book's 1-18 table through `Director.dice`; every amount
is rolled, "roll again" chains up to `MAX_REROLLS`. Coins and valuables (gear entries with a
`value`, which `trade.sell` pays in full) go onto the finder's sheet; the rest comes back
as `you_decide`, with three of the book's magic items (`data/magic_items.json`) as ideas
when something magical turned up. `MONSTER_LOOTED` marks an instance searched, journalled
before the sheet so the first undo returns the coins.

#### Towns

**Steadings** (`steadings.py`). `create_steading` builds one from `KINDS` -- the book's
defaults per kind, an option and a problem by number, every "your choice" filled in order
from `details` (`blank_names` says which) -- into `state["steadings"]` (`STEADING`,
journalled); `adjust_steading` steps the ranked tags and adds or removes the rest.
`set_scene(steading=)` names where the party is, and `steadings.here` reads it: the
Director's `steading` callable applies Market to Supply and `recruit_bonus` to Recruit
(its `target` names who is sought), and buying checks `for_sale` (prosperity, Market,
Exotic, Craft, Lawless for poison) and `price_factor` (Resource half, Need double, on sale
too). The live state lists every steading and marks the party's.

**Supply and Carouse.** Supply is a +Cha move; its result tells the model what to charge,
and `buy(price=)` bounds it (book price to twice it; up to 500 for something unpriced).
Carouse takes `make_move(coins=)`: whole hundreds, +1 per hundred past the first, refused
if unaffordable; its options are the player's (three, one, and one on a miss), and the coin
is taken when it resolves.

**Hirelings.** `hirelings.py` makes them by the book (skills and ranks, loyalty, a cost, points
by where they were found) and they live on the employer's sheet (`hirelings`), so every
change journals as a `SHEET`. Recruit rolls `OFFERS` (+1 per offer made known, from
`make_move(offers=)`); Order Hirelings rolls `LOYALTY` (the named hireling's, -1 while
`unpaid`). `hire` and `update_hireling` (paid, loyalty ±1, leaves) are the model's tools;
the rules of every skill at the table reach the model with the class moves block. Five are
wired: Priest (`ministry` at camp; `hireling_first_aid`), Warrior and Protector as
`make_move(aide=)` (on the `PendingCheck` as `aide`: +skill damage and the counter-attack
taken; or +1 with the roll held to 9 by `_intervened`), Protector's Sentry as
`monster_attacks(protector=)` (+skill armor, then `strain` until camp or Recover), Tracker
in `perilous_journey` (every job a 10+), Minstrel as `trade.buy(discount=)`.

**Outstanding Warrants and Bolster.** Outstanding Warrants is a +Cha move whose 7–9
complication is the model's to choose. `bolster` (downtime, guarded like camp) adds 1
preparation for a week or two of study and 3 for a month or more (`sheet["preparation"]`),
and moves the clock a day at a time; `make_move(preparation=true)` spends one for +1, one
per roll, on the moves that read a stat.

### 6.5 Monsters and damage

`add_monsters` puts creatures in the scene as numbered instances ("Goblin 2") with their own
HP, never reusing a name already used at this location (`monster_names_used`, reset when
the scene's location changes), so a second wave is "Goblin 4", not another "Goblin 1"; SRD creatures are looked up; homebrew comes from `monsters.build`, the book's Making Monsters
questions as a table -- organization, size, defense, known for, attack, traits, each answer
adding its HP, damage, die steps, armor, piercing and tags, floored at 1 -- with an instinct and
a move required (hand-given hp/armor/damage remain a checked fallback). Nothing balances an
encounter: the book says not to. Each
living kind's nature -- instinct, moves, qualities, tags, attack -- reaches the model once
per kind in the live state and in `add_monsters`' result (`monsters.natures`); a homebrew
creature keeps the instinct and moves the model gave it on its record. An
attack targets an instance by name; at 0 HP it is defeated, told to the model as fact.
`damage.py` rolls `d`, `b[2d]` and `w[2d]` expressions with the option's ±1d6, and applies
armor, piercing and ignores-armor; the dice line states the amount outright ("hits Thorin
for **4 damage** (d6: 5, armor 1) — HP 18 → 14"). A weak Hack and Slash rolls the
creature's blow back in the same breath — the trade is simultaneous, so it lands even if
the creature died. `monster_attacks` is the GM-move version; with `also`, several
creatures strike one character at once as one blow, by the book's "Damage From Multiple
Creatures": the highest damage among them, rolled once, +1 for each attacker beyond the
first, with the leader's piercing and ignores-armor.
`deal_damage` lands a character's damage with no move at all, when the fiction gives
them the blow (a helpless or surprised enemy): weapon reach checked, reason shown.
Every HP change is a journalled `ChangeHp`.

### 6.6 Spells

`spells.py` holds the caster's state on the sheet: a Wizard's spellbook, a Human's spell
borrowed from the other list, prepared/granted `spells` (total levels ≤ level + 1, none
above level; rotes and cantrips free), `lost` spells, `cast_penalty` (the 7–9 "-1 ongoing
to cast") and `ongoing` spells. Whether a spell costs -1 while it lasts, forbids casting,
or ends when its caster casts again is read from its text; its healing and damage dice are
hand-written. `cast_a_spell` rolls +Wis or +Int by class with the spell fixed on the check;
a hit casts it (damage through the Roll damage button, Fireball hitting every creature
named, Harm burning the caster too), a 7–9 costs what the player picks, a miss casts
nothing. Bless gives its target +1 while creatures stand; Magic Weapon adds 1d4 to the
caster's weapon. `prepare_spells` (Commune / Prepare Spells) is guarded like camp, clears
the old spells and posts a durable Choose spells button for a private picker
(`spells.Preparation`). `end_spell` ends an ongoing one.

### 6.7 Companion and Shapeshifter

The Ranger's companion (`companion.py`) is made in a private form — species, base,
strengths/trainings/weaknesses counted by its numbers, name. `make_move(companion=true)`
applies Command when its trainings cover the move: +cunning to tracking, Discern Realities
and Parley; +ferocity to attack damage and its armor against the blow back.

The Druid's Shapeshifter rolls into a named species and holds 3/2/1 (even a miss holds 1).
The model records the form's moves with `form_moves`; `spend_hold` spends one per use with
no roll, or all to turn back, and the Druid returns to their natural form at 0.

### 6.8 Debilities, Defend and encumbrance

`sheet["debilities"]` holds any of weak, shaky, sick, stunned, confused, scarred;
`sheets.stat_modifier` takes 1 off the stat each names, and every roll that reads a stat
goes through it, noting the cost on the roll line. The model gives and cures them with the
`debility` tool, naming a cause shown to the table; they do not stack. `recover` (days of
rest, guarded like camp) heals to full and clears days // 3 debilities (// 2 with a
healer); when that is fewer than a character has, `cures_owed` goes on the sheet and a
durable `CureButton` per debility lets their player pick. The clock moves a day at a time.

`perilous_journey` (guarded like camp) takes the destination, the expected days and up to
three jobs, one per character. It spends a ration per traveller per day at once, and opens
each job as a non-callable +Wis move (`journey_trailblazer`/`_scout`/`_quartermaster`)
through `_trigger`, so each resolves alone as a rules event; a quartermaster's 10+ refunds
one ration. Unfilled jobs count as a 6. The model moves the clock, since the trailblazer's
10+ shortens the trip "by how much" the GM says.

Defend (`defends`: the target is the charge, a character's name if it is one) puts
`defending = {charge, hold 3/1}` on the defender's sheet. `monster_attacks` against the
defender or their charge, while hold remains and the defender can act, rolls the damage
and **holds** it as a non-callable move of its own (`defend_charge`, with redirect, or
`defend_self`), its dice the creature's damage faces, so it survives a restart. The
defender's click lands it — redirected, halved, with the defender striking back for their
level, or as rolled — spends one hold (clearing the defense at 0) and tells the model as a
rules event. Expiry spends nothing. Make Camp ends every defense.

**Encumbrance.** `sheets.load` is the class base (`LOAD_BASE`) plus Str; `carried` sums gear
weights and coin at 100 to the weight. One or two over load, `open_move` and casting take -1
and say so. Past load+2 the check is opened with the -1 but posted with an `OverloadPrompt`
instead of a Roll button: a durable `DropButton` per item (dropping it through
`trade.remove_items`, then rolling) and a `FailButton` (a miss with no dice, so no XP).

#### Damage beyond an attack

**Hazards.** `hazard_damage` is damage from no creature: the model names the book's
severity (`_HAZARDS`: bruises d4, blood d6, bones d8, lethal d10), whether it ignores armor,
and cover (1-2); each character caught goes through `_attack(hazard=True)`, so armor,
advanced-move armor, `last_hit`, the Reduce damage button and Last Breath all work as for a
creature's blow.

**Answering a blow** (`reactions.py`). `_attack` records each hit on the victim's sheet as
`last_hit` (id, attacker, dealt, HP lost, down) and, when `reactions.options` finds anyone
at the table with an answer, posts it with a `ReactPrompt`: a durable `ReactButton`
(`gary:react:<hit id>`) anyone may press. `Director.reactions` lists what the presser can do
about that hit -- their own Spell Defense, Armor Mastery, Bloody Aegis, Shed, Man's Best
Friend or Penitent, or Divine Intervention for anyone -- shown as an ephemeral picker;
`Director.react` applies one, refunding at most `lost`, marking the hit `reduced` (the pain
is a separate `pain` flag), paying the cost on the sheet, cancelling an open Last Breath,
and telling the model as a `rules_event`. Commune sets `intervention` hold; a spent
companion (`companion.spent`) adds no ferocity until camp.

**Poison, stun, cover and the rest.** `use_poison` spends a carried dose of any equipment-list
poison (`POISONS`) or the Thief's mastered one; on a creature Bloodweed and Serpent's Tears go
on its record as `afflictions` (`AfflictMonster` -> `MONSTER_AFFLICT`, journalled), read by
`_attack` (-1d4 to its damage) and `_serpents_tears` (damage against it rolled twice, better
kept). `stun` sets `stunned`: no HP lost, and `open_move` refuses anything but Defy Danger until
`end=true`. `monster_attacks(cover=)` adds 1-2 armor. `_hurt` adds Cloudkill's separate 1d6
while anyone's Cloudkill runs (to every creature: Gary cannot see the fog's edge); Consume
Unlife destroys its target and heals the caster by the HP it had left. `trade._rations_for`
gives "Requires Dwarf" food only to Dwarves; antitoxin is handled in `use_item`.

#### Bonuses and situations

**Setting up the next blow.** `_forwards_from` also runs `_after_hack` (any Hack and Slash:
Duelist's Parry/Block `armor_forward`, Scent/Taste of Blood and Setup/Tandem Strike `marks`
via `make_move(ally=)`) and `_invigorate`. `armor_forward` is added in `_attack` and spent on
the next blow; `marks` ({target, damage, reason}) are spent by `_use_marks` at both character
damage sites. `lead_the_charge` grants Charge!'s forward and Ever Onward's armor. Superior
Warrior drops the counter-attack on a 12+; damage forwards may be flat ("2", Invigorate).

**Situational flags.** A move that depends on the situation is told so on `make_move`
(`weak_point`, `two_weapons`, `extra_targets`, `form_boon`, `form_plus`, `form_minus`),
checked by `_situation` against the advanced move that grants it, and carried as the check's
`flags`: Smaug's Belly's 2 piercing in `_damage_die`, Viper's off-hand dice and Blot Out the
Sun's `spread` (one roll on every target, extra ammo spent up front) in `_land_damage`,
Formshaper and Formcrafter onto the `shape` record (armor and dice in `advanced.py`, the
stat +-1 by `_form_mod`). Balance counts up on damage dealt; `channel_balance` spends it.
`_race_move_of` gives Half-Elven Rangers both race moves.

**Ongoing.** `ongoing` puts `{amount, reason, until, move}` on `sheet["ongoing_mods"]` (not
`ongoing`, which is running spells): +-1 or 2, to every move roll or one move. `until="battle"`
records the creatures standing (`battle`) and is live while any of them still stands -- the
same trick as the Human Fighter's reroll; `until="ended"` lasts until `end=true` with its
reason. `_ongoing` adds it in `open_move`, casting and `_trigger` (not Last Breath: "no stat
is added"); camp drops the finished battle ones. Through Death's Eyes' miss puts one on.

**Notes for the model.** What an advanced move adds that Gary does not track goes on the
result as `MoveOutcome.notes` (the model sees it as `also`): Cautious and Extremely Cautious
on Trap Expert, the Empower family's offer on a 10+ cast (free on a 12+ with Greater).
Impervious Defender's 12+ replaces Defend's hold in `_guard`. `_after_strike` records
`last_struck` for Spell Augmentation, whose tool shunts an ongoing spell's level onto it.

### 6.9 Fronts and the GM's tools

`fronts.py` checks a front against the book's recipe: campaign or adventure; 1–3 dangers, each
of a type from the book's five families (which gives its default impulse and its family's GM
moves); grim portents, 1–3 on an adventure front and 3–5 on the campaign front; an impending
doom of one of the six kinds with a concrete form; 1–3 stakes questions; a description, cast
and custom moves (text the model honours). Fronts live in state as `fronts`, written only by
the journalled `FRONT` mutation, whose inverse is the record as it was.

The book builds fronts "outside of active play", so `create_front` is refused except while
Gary holds the front-work window open (`opening.front_work`): after the first `/end`
for the first campaign and adventure fronts, and after every later one for a between-session
review (advance what was neglected, end what was dealt with, revise, move a danger between
fronts, write a new adventure front when one resolves). In play the model notes threats
(`record_note` kind `threat`), and may `advance_portent` (descriptively, offscreen, or as a
hard move; the last one brings the doom and resolves the danger), `revise_front` (what is
still to come; what has come to pass stays) and `move_danger` (with its progress; applied
target-first, so half an `/undo` leaves it in both fronts, never neither). None of it posts to
the table; the live state shows each live danger's type, impulse, progress and next portent,
the custom moves, and once per family in play its GM moves. With Gemini as the GM, "only the
GM sees fronts" means whoever runs the server: `/fronts` and `/state` (which renders the live
state, fronts and all) carry `default_permissions(manage_guild=True)`, so Discord hides them
from everyone else, and check the permission again before answering, ephemerally. When a
front's last danger ends, `fronts.reveal` is posted to the table -- the book's look at the
aftermath -- while the model gets only a note that it happened.

**Instant NPCs** (`npcs.py`, `data/npcs.json` from the authors' NPCs appendix): `instant_npc`
rolls a d100 instinct and knack through `Director.dice` and a name not used by an NPC or sheet.
**Session questions:** `/questions` (journalled `SESSION_QUESTIONS`) replaces the book's
three end-of-session questions with the adventure's own; `EndOfSession(questions=)` asks them.

### 6.9a Forward

`sheet["forwards"]` holds `{amount, reason}` (or `{damage, reason}`: Arcane Art's +1d4). Moves
that grant them do it in `_forwards_from` (Discern Realities, Take Watch, I Am the Law,
Backstab's advantage, Arcane Art) and Defend's "open the attacker up" in `_spend_defence`;
the model adds its own with the `forward` tool. A roll forward is spent only when the model
calls a move with `forward=true` -- the book's conditions are fiction, so the model judges
them -- and is summed into the committed modifier; a damage forward goes on the next damage
roll. Make Camp, Recover and setting out on a journey clear them.

### 6.10 Camp, XP and advancement

- **Make Camp** — one call for the party: a counted ration each; on a quiet night everyone
  who ate heals half max HP and the clock moves eight hours once. If something approaches,
  the watcher rolls Take Watch and nobody heals. Refused while creatures stand or anyone is
  mid-move, and the model must give a reason it is safe, shown to the table. The same guard
  (`Director.unsettled`) gates Commune and Level Up.
- **XP** — every miss marks 1 XP (`Director._mark_xp`, on every resolution path).
  `/end`: the book's three questions as toggles anyone may flip, a per-character
  "acted on alignment" toggle only that player may flip, and Resolve a bond (picked
  privately; the companion's player must press Agree). Award XP applies it all in one apply.
- **Level Up** — the sheet's Level up button, in downtime: a private form (`levelling.Advance`) for a stat
  (18 at most; Con raises HP), an advanced move (the 6–10 list from level 6; Requires and
  Replaces honoured) and a Wizard's spellbook spell; level + 7 XP is spent. A move in
  `levelling.SPELL_MOVES` adds a `move_spell` step: Chosen One / Anointed / Prodigy / Master
  append to `lowered_spells` (`spells.lowered` takes a level off wherever a spell is found,
  so a first-level one is `free`), Expanded Spellbook appends any class's spell to the
  spellbook (`spells._named_any`). Spell dropdowns run to four of 25. Multiclass Dabbler /
  Initiate / Master add `multi_class` and `multi_move` steps (`multiclass.options`: another
  class's starting moves and the advanced tiers its level less one reaches, minus the
  spellcasting core and the companion) and append `{class, name}` to `multiclass_moves`,
  which `advanced.has`, `class_moves.render` and the class gate in `open_move`
  (`multiclass.grants_key`) all read. Divine Favor and God Amidst the Wastes set
  `favor_from` to the level taken: `spells.favored` makes `caster_class` "cleric" and
  `caster_level` (level - favor_from + 1) drives what can be prepared and the budget; the
  cast gate lets them Cast a Spell, and the sheet's Class choices button asks for their deity.
  Compendium classes (`compendium.py`, `data/compendium.json` from the CC-BY Player
  Compendium and the core book's Divine Bond): `qualify_compendium` adds the class to
  `compendium_qualified`; `levelling.move_options` adds `compendium.options` (the starting move
  of a qualified class; the follow-on moves of a started one), and a level-up empties
  `compendium_qualified` -- "the next time you level up". Taken moves go on `advanced_moves`, so
  `advanced.has`, `class_moves.render` and `moves.ADVANCED_ROLLS` (Bind, Diplomat) read them.
  Wraithlander's mark is a `move_choice` step into `wraith_marks`. `bonds.partners` adds
  "my weapon" (Whosoever Holdeth) and the deity (Divine Bond) to whom a bond may name;
  `compendium_power` runs what the fiction calls on (weapon boons, deity favor, dragon blood,
  heart-blood, Dragon's Breath, Faraday's Law; `found_stronghold` makes `steadings.make_stronghold`
  and sets the sheet's `stronghold`), and `make_move` takes `weapon_leverage`, `dragon_nature` and
  `hurl`. Hunger and Faraday's Law are `advanced.damage_dice`; Fury is a one-attack +1 piercing;
  Call the Banners lifts a Recruit miss at the stronghold to 7 (`_banners`). Blob (`slimes.py`):
  `minions` on the owner's sheet; `make_move(minion=)` rolls the slime's own stat (a copy, the
  owner's) and a `minion:` flag makes its damage d4 and sends any counter-attack to it
  (`_hit_minion`, as does `monster_attacks(minion=)`); `slimes.penalty` takes ooze form and each
  copy off Int, Wis and Cha in `stat_modifier`.
- **Bonds** — the sheet's Bonds button: the class's lines, each filled once with a companion at the
  table. Aid or Interfere rolls +Bond, the bonds held with the character helped.

---

**The Paladin's Quest** (`quests.py`). `start_quest` records `sheet["quest"]`: the mission
(slay / defend / discover and its blank), up to two boons the player chose (two take a
blank) and the vows the model names; `end_quest` clears it as fulfilled, broken or
abandoned. Gary reads it in `advanced.py` (Smite, Holy Smite, Holy Protection, the Paladin's
Divine Protection count only `on_quest`) and in camp and journeys (`sustenance` eats nothing).
The other boons reach the model on the sheet line. Exterminatus: `swear_enemy` sets
`sworn_enemy` (a creature's listed name, or a kind or NPC); `_sworn_damage` adds 2d4 against
a target matching it by name or kind (articles ignored) and -4 against anyone else, at both
character damage sites, and `_oath_kept` clears it when that target is defeated. A failure
is released with `released=true` and a reason.

## 7. Campaign store

### 7.1 What it holds

Sheets, the player roster (Discord user → character), creatures in the scene, world time
and calendar, scene, setting, NPCs, notes, effects, quests, and — for characters without a
sheet — loose inventory. Reads are plain calls on `CampaignStore`.

### 7.2 Writes

Every write is a `Mutation` applied by `CampaignStore.apply(mutations, reason)`, batched,
journalled and reversible, returning a `StateDiff` so the model narrates what actually
happened. The model never sees that API: it gets narrow, typed tools (§12.6), and the
Director's effects (`ChangeHp`, `SpendAmmo`, `UpdateSheet`) become `SHEET`/`MONSTER_HP`
mutations in the runner.

### 7.3 Persistence

- **Journal + snapshot.** Every mutation appends to the journal, durably. **Resolved moves
  are journalled too**, as `move_outcome` entries carrying the move, stat, modifier, faces,
  total, band, choices, damage, healing, spell and shape. They are not mutations and replay
  as no-ops; they are evidence. A modifier fixed before the dice is the guarantee the
  design rests on, and without a record nothing shows afterwards that a given move was made
  that way. A snapshot is written every N mutations; recovery is the latest snapshot plus a
  replay from its offset.
- **Monotonic `seq` on every entry**, assigned by the store. A batch writes several entries
  in one millisecond, so timestamps cannot order them; a document backend **must refuse an
  entry without one** rather than invent it.
- **Atomic snapshot writes.** Temp-then-rename, with an fsync of the file and its directory.
- **The calendar is the real one, a millennium back.** `world_minutes` counts from
  `calendar.epoch_date`, set once at creation (today minus 1000 years) and journalled.
  Months are named for their full moons (Wolf, Snow, Worm, Pink, Flower, Strawberry, Buck,
  Sturgeon, Harvest, Hunter's, Beaver, Cold), so `4 Harvest Moon 1026` dates the scene and
  places it in autumn at once; seasons, daylight and leap years come free.
- **Undo.** `/undo` reverses the last journalled mutation. Inverses are computed by the
  store from observed before/after values, never supplied by the caller, so the model
  cannot describe its own undo. Reversed entries are marked by a later entry carrying
  `undoes`, so undo is not repeatable across a restart.
- **Bounds.** Mutations past configured limits are rejected, not applied, and the whole
  batch with them; the model is told why. The trade tools carry their own caps.
- **Reset is the one destructive operation.** `/reset` (two-step, with a token)
  truncates the journal *then* writes an empty snapshot — in that order, so a crash between
  leaves the old campaign intact rather than resurrected — and clears every channel's
  conversation history, pending moves and the roster.
- **Schema version** in every snapshot; a mismatch refuses to load.
- **Single writer**, enforced by §7.6.
- **Backups.** A campaign is months of a group's time: snapshot + journal to Cloud Storage
  nightly on the file backend; Firestore makes it redundant.

### 7.4 Storage backends

Durability sits behind two protocols — `StateBackend` (snapshot + journal) and
`CheckBackend` (pending moves) — so the running process owns no campaign data:

| Backend | Use |
|---|---|
| `InMemory*` | Tests; a dry run that leaves no trace. |
| `File*` | Local disk. Default for one table. |
| `Firestore*` | Managed. Makes the VM disposable. |

**Pending moves are durable, and that is not optional.** A restart between "roll" and the
click would otherwise lose a modifier committed before the dice. The registry keeps an
in-memory index and writes through on every change. A saved check that can no longer be
resolved (a d20 check from before moves) is skipped on load rather than refusing to boot.

One test suite runs against all three backends. The Firestore module is verified against
the emulator. **Its 1 MiB document ceiling is a hard limit on campaign length**: the whole
state is one field of one document, and state grows monotonically (notes, NPCs, sheets).
Past it, `InvalidArgument` propagates out of `snapshot()` mid-turn and every later mutation
raises too, because the threshold stays tripped. If a campaign approaches it, split the
snapshot or move notes out of the state blob.

### 7.5 The campaign lock

The writer takes an advisory exclusive `flock` on `.gary.lock` in the campaign root at
boot and releases it after its closing snapshot. A lock it cannot take is a loud warning,
never a refusal to start. `flock` is per-machine: it protects the file backend and does
nothing for two machines on one Firestore campaign, which would need a lease in Firestore.

---

## 8. Context assembly

A campaign runs for thousands of turns; naive appending guarantees runaway cost and the
truncation of exactly the facts the system depends on. Every call is assembled from four
layers, in order:

1. **System instruction** (static) — the persona: a Dungeon World GM who names moves and
   never rolls or invents a number, adds creatures before anyone fights them, keeps
   position in the scene summary and the books with tools. A "running the game" section,
   drawn from the fan guide, covers the conversation (threat, response, roll), clear
   threats before damage, soft and hard moves, 7–9 as success with a cost, no-roll actions,
   and spreading the spotlight, and the book's principles (never name your move, be a fan of
   the characters, think offscreen). Held under 7.5% of the token budget by a test.
2. **Live state** (rebuilt per call, never carried in history) — the setting (≤600
   characters, the premise from `/begin`), date and time, scene, the party with each sheet line
   (stats, HP, armor, weapons, XP, shape), what they carry, their spells, companion and
   bonds, the creatures in the scene with HP, and effects. Then the **class moves block**:
   each character's alignment, rolled moves by key, narrative and advanced moves in the
   book's words, and a caster's castable spells with their text. Rebuilt from the store
   every turn, which is what makes the store the source of truth in practice.
   With two or more characters seated, a **spotlight tally** follows: `context.spotlight` counts
   each character's moments in the recent turns (a player message attributed to them, a
   `make_move` with them as actor; the two together are one moment) and names the quiet ones
   once the gap is three or more -- the book's "spread the spotlight" as a fact, not a hope.
   Then, only when the recent run is lopsided (five of the last six one way), a **beats** line
   (`beats.py`): up and down read from the journal -- strong hits, creatures to 0 HP, looting
   and HP regained up; misses, Last Breath, HP lost and debilities down -- asking the model to
   lean the next thing it introduces the other way, never a roll.
3. **Retrieved memory** — NPCs and notes relevant to the scene: a note surfaces when its
   subject is a party member, the current location, or is named in what was just said
   (whole subject or any distinctive word, so "Toby Cole" answers to "Toby"), plus a
   rolling summary of everything older than the window.
4. **Recent turns** — the last `GARY_HISTORY_WINDOW` (default 30) verbatim.

**Budget.** Total input is capped (default 40k tokens). Layers 1 and 2 are never trimmed;
layer 4 is trimmed first, then layer 3. The window never begins on an orphaned
`function_response`.

**Summarisation.** Turns falling out of the window are compressed into a scene summary on
the cheaper model, off the player-facing path, written through a separate method so a
background summary cannot be clobbered by a turn that loaded the old one.

### 8.1 Seeding a campaign

Layers 2 and 3 are empty on a fresh campaign, the worst moment for it. Dungeon World's
answer is to prepare little and build the world at the table, and `/begin` (`opening.py`)
runs that first session as a sequence, following the recipe in "My Recipe for Starting
Adventures" (Spouting Lore, 2020): a premise (a fantastic place and a grabby activity), the
characters, introductions, hook questions to each character, bonds, a tight opening frame
that folds in the bonds and asks a question at once -- and, after the first `/end`, a
campaign front and one or two adventure fronts. Until then `create_front` is refused and
threats are noted (`record_note` kind `threat`), as the book has it: fronts are "built
outside of active play", and not before the first session. The stage lives in state (`OPENING`, journalled and undoable), so the durable
**Next** buttons survive a restart and only whoever ran `/begin` can press them.

The premise offer is the one model call that decides nothing: `EffectRunner.ask` returns
the text and drops any effects, and the places and activities are parsed rather than trusted
(bullets and labels stripped), shown in two dropdowns beside **Write my own** and **Other
options**, which names the ones passed over so the model offers new ground. The hook
questions and the opening scene are rules events: ordinary turns with tools, where answers
become NPCs and notes. Earlier versions seeded the world with `/setting`, `/scene`, `/time`
and `/import` of a setting document; they front-loaded a world the book says to leave blank,
and were removed.

---

## 9. Security & trust

### 9.1 Prompt injection

**Trust never derives from a string.**

- Move outcomes, rules events and tool results reach the model as **structured parts**
  over the tool transport (§5.6), never as prose in a user turn. `RULES ENGINE:` text
  parts are built by Gary, not relayed.
- Player text is attributed and delimited — `Player {display_name} (playing {actor}) says:
  <text>` — with impersonation markers (`[SYSTEM…]`, `[tool_result]`, …) stripped.
- The model holds tools that change state, so each is narrow and checked: the Director
  refuses what the rules forbid, trade and coin are capped, the store rejects out-of-bounds
  mutations, and every change is journalled and undoable.
- **The model cannot touch what decides outcomes**: the roster (who plays whom) is set only
  by `/new`/`/playing`; sheets change only through rules-checked forms and effects;
  dice are Gary's; XP from End of Session is the table's.

### 9.2 Attribution

Every action that resolves something is a click, and a click carries its user. Buttons
check that user against the move's owner (the Director), the character's player (forms,
spells, alignment toggles), or the bond's companion (Agree). There is no name matching
anywhere on that path; `normalize_actor` folds names only to check the model's spelling of
a character against the roster.

### 9.3 Secrets

All configuration is environment variables, read once at boot by `Settings.from_env` and
validated there. The two that matter are `DISCORD_TOKEN` and `GEMINI_API_KEY`.

**Deployed, they live in GCP Secret Manager**, fetched via the VM's service account and
injected into the process or container, which only ever sees environment variables.

**Locally, an env file outside the checkout** — `$XDG_CONFIG_HOME/gary/env`, falling back to
`~/.config/gary/env`, which `docker-compose.yml` reads — `chmod 600`, for a developer's own
bot. Outside the repo because `rm -rf gary && git clone …` must take neither the campaign
nor the token; `.env` stays in `.gitignore` and `.dockerignore`. A table other people play at
belongs in Secret Manager.

**Never in the image.** The build stage copies only `pyproject.toml`, `README.md` and
`src/`. Rotate on any suspicion of exposure.

---

## 10. Reliability

| Failure | Handling |
|---|---|
| Gemini timeout / 5xx | Retry with jittered backoff, then the fallback model once, then an in-character stall line. Never a silent channel. |
| Model returns nothing usable | Stall line; history not advanced. |
| Response > 2000 chars | Split on paragraph boundaries. |
| Discord rate limit | Per-channel send queue (default 1 msg/s). |
| Player never rolls | TTL expiry; the move lapses and is narrated as hesitation (§5.4). |
| Player rolled but never chose / rolled damage | The dice stand; first options taken, damage rolled for them, table told. |
| Rules-triggered move never rolled | Rolled for the player on expiry. |
| Model loops on its own output | `EffectRunner` recursion bound (`GARY_MAX_EFFECT_DEPTH`, default 4), logged. |
| Model calls tools round after round without speaking | Six rounds a turn (`MAX_TOOL_ITERATIONS`); if nothing has been said by then, one more call with the tools removed narrates what was done. |
| Gary restart mid-move | Pending moves and their buttons survive (§5.3, §7.4). In-memory forms (creation, level up, bonds, End of Session) are lost and reopened by the command. |
| Interaction not answered in 3 s | Clicks are acknowledged before narration starts. |

**Observability.** Structured logs; the last line of `/state` shows messages sent, turns
narrated, model failures and moves journalled since start.

---

## 11. Deployment

**Compute Engine VM, single always-on process.** The Gateway connection must be continuous
— Gary must see player messages addressed to nobody in particular — and pending moves must
outlive individual turns. `e2-micro` in `us-west1`, `us-central1` or `us-east1` (the free
tier), a `systemd` unit with `Restart=always` or a container with
`restart: unless-stopped`, secrets from Secret Manager.

Running today: `e2-micro` in `us-east1-b` on Container-Optimized OS, no public IP (Cloud NAT
out, SSH through IAP), the env file and a read-only GitHub token in Secret Manager, the file
backend on the VM's disk with nightly snapshots. Every boot pulls `:latest`, so a reset is an
update. [`deploy/gcp/README.md`](deploy/gcp/README.md) has the commands.

### 11.1 The VM is disposable

- **State and pending moves go to Firestore** (§7.4): delete the VM, recreate it, the
  campaign is intact — including a move whose modifier was committed seconds before.
- **The Director holds no per-channel state** (§4.1).
- **The campaign lives outside the checkout.** `GARY_CAMPAIGN_ROOT` defaults to
  `$XDG_DATA_HOME/gary/campaign` (falling back to `~/.local/share/gary/campaign`). A
  campaign found at the old `./campaign` default is reported at boot and left untouched.

The file backend is a good choice for one table, but then the VM is a pet and needs the
nightly sync to Cloud Storage.

### 11.2 The container

`Dockerfile` builds a wheel into `python:3.12-slim`, running as uid 10001, with a
`HEALTHCHECK` that imports the adapters and every data-loading module.
`.github/workflows/docker.yml` tests, builds, smoke-tests and publishes to `ghcr.io` on
`main` and `v*` tags; a pull request builds and smoke-tests but publishes nothing. The smoke
test asserts the installed image carries all the rules data (9 classes, 154 monsters, 45
items, class moves, 63 spells) and rolls a die: `pip install .` once shipped no
`data/*.json`, and a suite run from source cannot catch that.

Three constraints the image does not relax:

- **It is a long-running process, not a request handler.** Anything that scales to zero or
  recycles between turns is out; Cloud Run only with `min-instances=1` and CPU always on.
- **State is on the volume or in Firestore, never in the container.** The image sets
  `GARY_CAMPAIGN_ROOT=/campaign`; `docker-compose.yml` bind-mounts the host default.
- **Do not scale the service.** The lock is per-machine (§7.5).

### 11.3 If the always-on process ever needs to shrink

Receiving needs the Gateway; sending does not (posting is REST). A minimal always-on
publisher with stateless workers behind Pub/Sub would work, keyed on `channel_id` for
ordering, idempotent on the Discord message id before the Gemini call, with a dead-letter
topic. Out of scope; recorded because the naive version — Gary itself scaled to zero —
does not work.

---

## 12. Implementation notes

### 12.1 Stack

Python 3.12, `discord.py`, `google-genai`, pytest. Run locally:

```bash
cd ~/github/gary && set -a && . ~/.config/gary/env && set +a
PYTHONPATH=src .venv/bin/python -m gary.adapters.discord_bot
```

### 12.2 The adapter boundary

The core decides and returns `Effect` objects (`SendMessage` with an optional prompt,
`PromptModel`, `ChangeHp`, `SpendAmmo`, `UpdateSheet`, `RecordOutcome`); it performs
nothing and imports neither discord.py nor google-genai. Adapters perform. `ports.py` names
the seams: `Narrator`, `MessageTransport`, `Dice`. That is what lets the whole rules engine
be tested with no Discord server, no API key and scripted dice, and the Discord path be
run with `EchoNarrator` and no model at all.

**Narrating can produce more effects**, because the model may call tools. `EffectRunner`
recurses with a depth bound. **A narrator failure never silences the channel.**

### 12.3 Gemini configuration

Tools are declared explicitly (`adapters/gemini_tools.py`) and dispatched by the adapter;
automatic function calling is disabled, because the SDK would answer `make_move` at once
and §5.6 depends on not doing that. Temperature is left at the Gemini 3 default (lowering
it risks looping, which the recursion bound exists to catch).

### 12.4 Model choice

`gemini-3.8-flash` for narration, `gemini-3.5-flash-lite` for summarisation,
`gemini-3.7-flash` (the previous release) as fallback, each overridable by environment and pinned by a test so a
stale default fails in CI rather than at the table. The 3.x family's stable flagship is
Flash; a turn is two model calls around a human click, so latency and cost matter.

### 12.5 Configuration

`DISCORD_TOKEN`, `GEMINI_API_KEY`; `GARY_GAME_CHANNELS`, `GARY_FREEFORM_CHANNELS`,
`GARY_PLAYERS` (Discord user → character, for a table that predates `/new`),
`GARY_CHECK_TTL` (180 s), `GARY_SWEEP_INTERVAL` (15 s),
`GARY_SEND_INTERVAL` (1 s), `GARY_HISTORY_WINDOW` (30), `GARY_MAX_EFFECT_DEPTH` (4),
`GARY_GEMINI_MODEL` / `_SUMMARY_MODEL` / `_FALLBACK_MODEL` / `_TIMEOUT`, `GARY_BACKEND`
(`file` or `firestore`), `GARY_CAMPAIGN_ROOT`, `GARY_FIRESTORE_CAMPAIGN_ID`.

### 12.6 Tool surface

The model sees narrow, typed tools, each with its own checks and failure message, each
mapping to store mutations or Director calls:

- **Moves and GM moves:** `make_move`, `monster_attacks`, `hazard_damage`, `deal_damage`, `make_camp`,
  `forward`, `ongoing`, `stun`.
- **Class and compendium moves:** `start_quest`, `end_quest`, `swear_enemy`, `lead_the_charge`,
  `channel_balance`, `spell_augmentation`, `qualify_compendium`, `compendium_power`.
- **Creatures:** `add_monsters`, `remove_monsters`.
- **Harm, rest and travel:** `debility`, `recover`, `perilous_journey`.
- **Hirelings and downtime:** `hire`, `update_hireling`, `hireling_first_aid`, `bolster`.
- **Fronts:** `create_front`, `advance_portent`, `end_danger`.
- **Magic, forms and poison:** `prepare_spells`, `end_spell`, `form_moves`, `spend_hold`,
  `use_poison`, `brew_poison`.
- **Items and coin:** `give_items`, `take_items`, `use_item`, `give_coins`, `buy`, `sell`,
  `roll_treasure`.
- **World:** `advance_time`, `set_scene`, `upsert_npc`, `instant_npc`, `record_note`, `create_steading`,
  `adjust_steading`.

`open_check` survives only as a name, so histories from before moves still read. Rejections are reported back (`ok: false` with the reason), so the model narrates what
actually happened — a refused sale, a potion nobody has.

### 12.7 Source map

```
src/gary/
  models.py        frozen domain types: PendingCheck, Roll, MoveOutcome, Mutation   §5, §7
  moves.py         every move Gary rolls: stats, bands, options, flags              §6.2
  resolver.py      band resolution + the model's payload -- deliberately boring     §5.2
  registry.py      pending moves, keyed by (channel, user); hold; sweep             §5
  fsm.py           the Director: moves, dice, damage, healing, spells, camp, XP     §4, §5
  sheets.py        sheets, the creation Draft, XP lines, alignment                  §6.3
  gear.py          class gear, signature weapon, equipment lookup                   §6.4
  trade.py         give/take/buy/sell on the sheet                                  §6.4
  treasure.py      the book's treasure table; magic items as ideas                  §6.4
  steadings.py     villages, towns, keeps, cities: templates and tags               §6.4
  npcs.py          the Instant NPC tables                                           §6.9
  healing.py       potions, bandages, poultices                                     §6.4
  monsters.py      bestiary lookup, homebrew checks, instances                      §6.5
  damage.py        damage expressions, armor, severity                              §6.5
  class_moves.py   class move texts, kinds, the model's class moves block           §6.2
  spells.py        spell lists, caster state, Preparation                           §6.6
  companion.py     animal companion and Command                                     §6.7
  levelling.py     Level Up                                                         §6.10
  advanced.py      advanced moves Gary applies; what the model is told it applies   §6.3
  multiclass.py    Multiclass Dabbler, Initiate, Master                             §6.10
  compendium.py    compendium classes: requirements, starting and follow-on moves   §6.10
  playbooks.py     classes from standalone playbooks (the Barbarian), merged in     §6.1
  beyond.py        Beyond 10th Level: retire, take an apprentice, change class      §6.10
  magic.py         magic items as gear with their text; the book's looked up        §6.4
  sky.py           daylight from hour and season; the day's weather; Weather Weaver §8
  dungeons.py      dungeon maps: areas, exits, blanks; monster settings; /map        §8
  pictures.py      /picture's subjects (only what the table knows) and prompts      §10
                   (adapters/pictures.py draws them: a style reference image sent
                   with every request, 1K, the first kept per subject, a daily cap)
  opening.py       /begin and /end: the first session, each session, the finale     §8.1
  slimes.py        Blob's slime minions, ooze form, copies                          §6.10
  quests.py        the Paladin's Quest                                              §6.10
  reactions.py     answering a blow: Spell Defense, Armor Mastery, Bloody Aegis...  §6.8
  dicestats.py     /dice: the table's rolls against fair 2d6                        §10
  choices.py       creation choices: deity, land, lore, poison, precise weapon      §6.3
  hirelings.py     hirelings: making, loyalty, skill rules                          §6.4
  fronts.py        fronts, dangers, grim portents                                   §6.9
  bonds.py         bonds and +Bond                                                  §6.10
  context.py       persona, live state, retrieval, budget, injection defence        §8, §9.1
  ports.py         Narrator / transport / Dice seams, EchoNarrator, ScriptedDice    §12.2
  config.py        boot-time settings, fail-fast validation                         §12.5
  data/*.json      extracted book text (CC-BY 3.0)                                  §6.1
  state/
    store.py       journal + snapshot + undo + bounds                               §7.3
    backend.py     storage protocols: in-memory, file                               §7.4
    firestore.py   managed backend                                                  §7.4
    history.py     conversation history + rolling summary                           §8
    lock.py        single-writer enforcement                                        §7.5
    schema.py      calendar and world time                                          §7.3
  adapters/
    routing.py     pure ingest decision                                             §4.2
    sender.py      2000-char splitting + per-channel pacing                         §10
    runner.py      performs Effects; bounds model recursion                         §12.2
    discord_bot.py discord.py glue, buttons, the command dispatcher, sweep loop    §5.3
    creator.py     /new, and the sheet's gear / alignment / choices forms           §6.3
    spellbook.py   Choose spells                                                    §6.6
    companion_form.py  the sheet's Choose companion                                 §6.7
    levelup.py     the sheet's Level up                                             §6.10
    bonds_form.py  the sheet's Bonds                                                §6.10
    session.py     /end                                                      §6.10
    slash.py       the slash commands: each through the dispatcher, four private    §6.3
    gemini.py      Narrator: context, tool dispatch, retries                        §5.6, §8
    gemini_tools.py  function declarations                                          §12.6
scripts/extract_dw_*.py   XML → data/*.json                                         §6.1
```

---

## 13. Open risks

1. **The model may narrate past a move.** Gemini resolving an action in prose instead of
   calling `make_move` is the likeliest behavioural failure, and the persona is the only
   guard. Watch for it in play; a validation pass that rejects outcome language without a
   move is the next step if it recurs.
2. **GM judgement is the model's, and unchecked.** Whether a miss deals damage, whether
   something approaches the camp, which species a Druid knows, whether an action triggers a
   move at all. Gary bounds *how much* but not *whether*.
3. **Nothing tracks position.** Reach and range are only as consistent as the scene summary
   the model keeps.
4. **Conditional moves are the model's.** Advanced and compendium moves that change a
   number Gary runs are wired (`advanced.py`, `advanced.APPLIED` tells the model which);
   those with a judgement-call trigger ("when acting on the answers", Fount of Knowledge,
   Heist) the model grants as a forward or ongoing bonus, or does not. Story effects --
   a deity's petition, a Bard's lore, Awakened Mind -- are the model's to honour.
5. **Gaps in the rules engine.** Defend's hold is spent only by `monster_attacks`, one per
   attack. Not tracked: Burglar and Adept hireling skills; encumbrance on rules-triggered
   moves; the companion's instinct on interference, and its armor against `monster_attacks`;
   poisons on characters (antitoxin is spent, what it cures is narrated); magic items' powers;
   which goods are Dwarven or Elven. Cloudkill counts every creature in the scene. A Human's
   borrowed spell is offered from levels 1–5 (Discord's 25-option limit). Holds Gary does
   not count (Trap Expert, Evil Eye) reach the model as notes.
6. **In-memory forms die with the process.** Harmless (reopen with the command), but a
   restart mid End of Session loses the toggles.
7. **Latency and cost.** A move is two model calls around a human click. Measure before
   tuning; instrument token spend.
8. **Firestore's 1 MiB ceiling** limits campaign length (§7.4), and the lock is per-machine
   (§7.5).
9. **`normalize_actor` folds hard** (`Kel'thas`/`Kelthas` collide) when matching the
   model's spelling of a character against the roster. Acceptable at one table.
