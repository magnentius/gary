# Gary

<img src="gary.jpg" alt="Gary" width="600">

An AI Game Master for D&D 2024 over Discord. Gemini writes the narrative, Avrae rolls the
dice and enforces the maths, and a local state server owns the persistent world.

Design: [`ARCHITECTURE.md`](ARCHITECTURE.md). This file is how to run it.

---

## Contents

- [What you need first](#what-you-need-first)
- [1. Install](#1-install)
  - [Run it in Docker](#run-it-in-docker)
- [2. Start Gary](#2-start-gary)
- [3. Make a world](#3-make-a-world)
- [4. Import it](#4-import-it)
- [5. Open the campaign](#5-open-the-campaign)
- [6. Sit down and play](#6-sit-down-and-play)
- [7. Start over](#7-start-over)
- [`?dm` command reference](#dm-command-reference)
- [Configuration reference](#configuration-reference)
- [Design](#design)

---

## What you need first

| | |
|---|---|
| **A Discord bot token** | With the **Message Content privileged intent enabled** in the Developer Portal. |
| **Avrae in the same server** | Gary reads Avrae's embeds; it does not roll dice itself. |
| **A channel id** | Right-click the channel → Copy Channel ID (needs Developer Mode on). |
| **Python 3.11+** | `pyproject.toml` requires it. Developed on 3.12. |
| **A Gemini API key** | Optional at first. Without one Gary runs on `EchoNarrator` and spends nothing. |

> **Enable Message Content before anything else.** Without it `message.embeds` arrives
> empty, the parser sees nothing, and Gary fails *silently* — no error, no narration, no
> clue why. It is the single most common reason a correct setup does nothing at all.

---

## 1. Install

```bash
git clone <this repo> && cd gary
python -m venv .venv
.venv/bin/pip install -e '.[discord,llm,dev]'
.venv/bin/python -m pytest -q          # 576 passed
```

The editable install needs **Python 3.11+** and a pip new enough for PEP 660. On an older
interpreter it fails and `python -m gary.…` reports `No module named 'gary'`. If you are
stuck on one, `PYTHONPATH=src` does the same job without installing anything — it is what
`pyproject.toml`'s `pythonpath = ["src"]` already gives pytest, and every command below
is written that way so it works either way.

**Create the venv at its final path.** A virtualenv bakes its own absolute path into
every console script's shebang, so moving or renaming the directory afterwards leaves
`pip`, `pytest` and the rest reporting `bad interpreter`. `python -m pip` still works if
that happens, but rebuilding is the fix — a venv is disposable.

### Run it in Docker

A published image saves the venv entirely. It pins Python 3.12 and carries the adapters
and the SRD data, so the only thing you supply is config and somewhere to keep the
campaign.

```bash
mkdir -p ~/.config/gary && chmod 700 ~/.config/gary
cp .env.example ~/.config/gary/env && chmod 600 ~/.config/gary/env
$EDITOR ~/.config/gary/env    # fill in DISCORD_TOKEN and GARY_GAME_CHANNELS

docker compose up -d
docker compose logs -f
```

That pulls `ghcr.io/magnentius/gary:latest`, reads your config from
`~/.config/gary/env`, and bind-mounts `~/.local/share/gary/campaign` into the container.
For a second campaign, add a `docker-compose.override.yml` with a different volume line.
Or without compose:

```bash
docker run -d --name gary --restart unless-stopped \
  --env-file ~/.config/gary/env \
  -v ~/.local/share/gary/campaign:/campaign \
  ghcr.io/magnentius/gary:latest
```

**`~/.config/gary/env` holds your bot token**, outside the checkout so deleting a clone
cannot take it with you — and outside the build context, so it cannot be baked into an
image. It is still plaintext on disk with no expiry, so `chmod 600` it and treat a lost
laptop as exposure. Fine for your own bot in your own server; a table other people play
at wants a secret manager (`ARCHITECTURE.md` §9.3).

**Mount the volume.** The campaign — journal, snapshots, every channel's history — lives
under `/campaign` in the image. Without the mount a restart loses it, and there is no
undo. The alternative is `GARY_BACKEND=firestore`, which keeps nothing on the machine
at all.

**Both live outside the repo on purpose.** `rm -rf gary && git clone …` is an ordinary
thing to do to a checkout — and it must take neither the campaign, which exists nowhere
else, nor the token.

**Run one.** Gary is a single always-on process holding a Discord Gateway connection.
Don't scale the service: the single-writer lock is per-machine, so two containers on one
campaign will both write to it.

Images are built and published by `.github/workflows/docker.yml` on every push to `main`
and on `v*` tags. To build locally instead, uncomment `build: .` in `docker-compose.yml`.

---

## 2. Start Gary

### The dry run (no model, no spend)

Prove the whole Discord path — intents, routing, parsing, correlation, resolution,
sending — against a live Avrae before spending a single token. Leave `GEMINI_API_KEY`
unset and Gary uses `EchoNarrator`, which reports outcomes in flat text:

```bash
export DISCORD_TOKEN='...'
export GARY_GAME_CHANNELS='123456789012345678'      # ',' or ' ' between channels

PYTHONPATH=src .venv/bin/python -m gary.adapters.discord_bot
```

If you already filled in `~/.config/gary/env` for the container, source it instead of
retyping — `set -a` exports everything the file sets:

```bash
set -a; . ~/.config/gary/env; set +a
PYTHONPATH=src .venv/bin/python -m gary.adapters.discord_bot
```

In the channel, have someone run `!sheet` and then roll something. If Gary answers with
`[SUCCESS] Caspian Varr rolled 17 against DC 15.` the plumbing is sound and the only thing
left is the model.

### The real thing

```bash
export DISCORD_TOKEN='...'
export GARY_GAME_CHANNELS='123456789012345678'
export GEMINI_API_KEY='...'

PYTHONPATH=src .venv/bin/python -m gary.adapters.discord_bot
```

Setting `GEMINI_API_KEY` is the entire switch from `EchoNarrator` to `GeminiNarrator`.

Campaign state lands in `~/.local/share/gary/campaign` by default, or wherever
`GARY_CAMPAIGN_ROOT` points (`$XDG_DATA_HOME` is honoured). It is a journal plus
snapshots, so it survives restarts and can be inspected by hand — and it sits outside
the repo, so deleting a checkout cannot delete a campaign. If Gary finds an old
`./campaign` from before that default moved, it says so at boot and leaves it alone.

### Optional, but worth knowing early

```bash
# Who plays whom. Without this the model can call for a check as "John K" — a name
# Avrae never says — and the check silently waits out its TTL. ';' between players.
export GARY_PLAYERS='111111111111111111=Caspian Varr;222222222222222222=Liriel'

# Channels where Gary answers every message instead of only when @-mentioned.
# Must be a subset of GARY_GAME_CHANNELS.
export GARY_FREEFORM_CHANNELS='123456789012345678'
```

`GARY_PLAYERS` is usually unnecessary — see [step 6](#6-sit-down-and-play), where `!sheet`
does the same job by itself — but it is the one that fails *invisibly* if it is wrong, so
it is worth knowing about.

---

## 3. Make a world

Gary reads a whole setting document and files it into memory. You can write one yourself,
but the fastest path is a generator. Gary's import was written against
[Kenji's Setting Generator](https://cros.land/rpg-setting-generator-and-world-building-tool/),
whose sections — overview, sub-locations, factions, NPCs, quest hooks — land in Gary's
notes and NPC roster almost one-to-one.

1. Open the [Setting Generator](https://cros.land/rpg-setting-generator-and-world-building-tool/).
   It is free, needs no account, and has no daily limit.
2. Type a premise under **DESCRIBE YOUR SETTING** — *"A drowned fen county under a
   dyke-lord whose sea walls are failing"* — or leave it blank for a surprise. Click
   **Generate setting**. It takes about half a minute.
3. **Fill in the other tabs.** This is the step that is easy to miss. A fresh setting has
   only **OVERVIEW** and **NPCS** — **LOCATIONS**, **FACTIONS** and **QUEST HOOKS** each
   start empty behind their own *"Generate factions for &lt;name&gt;"* button, and **the
   export contains only what you have generated.** Skip this and you import a thin
   document with no factions and nothing to pull the party anywhere.
4. Scroll to the bottom of the setting, click **Export**, then **Copy as Markdown**.

   Choose Markdown, not plain text. Gary splits an imported document **on Markdown
   headings**, because a section cut in half writes an NPC twice — once from the name,
   once from the disposition that landed in the next chunk. Markdown gives it `#`, `##`
   and a `###` per NPC to cut on; plain text gives it nothing, so it falls back to a hard
   split at 6000 characters. **Copy as HTML is not importable** — Gary accepts `.md`,
   `.markdown` and `.txt` only.
5. These buttons copy to the clipboard; they do not download a file. Paste into any editor
   and save as **`siltgard.md`**.

A single generated setting runs about 5KB — comfortably one chunk, so one model call.
Filling every tab and nesting sub-locations grows it; the ceiling is 200KB and 12 chunks.

---

## 4. Import it

Drag the `.md` file into your game channel with `?dm import` as the message:

```
?dm import
      [siltgard.md attached]
```

```
Reading `siltgard.md` — 1 part. I'll file people, places, factions and hooks as I go.
Recorded Siltgard, the Eikthyrn Fens, Kjor the Dyke-Binder, Grimwulf…
Done with `siltgard.md`. ?dm notes lists what stuck — anything it missed you can add
by telling me in the channel.
```

People become NPCs; places, factions, lore and rumours become notes, filed under the
subject a player would actually *say* — "the Alder-Keeping road", not "Chapter 2". They
surface when someone names them, so they cost nothing on turns where they are irrelevant.

`?dm notes` shows what stuck. Anything it missed you can add by just telling Gary in the
channel.

**Every refusal lands before the first model call**, so a bad import costs nothing: wrong
extension, over 200KB, more than 12 chunks, an empty file, or no model attached.

---

## 5. Open the campaign

Two lines make the difference between a model that knows where it is and one that invents
a world from a player's first sentence. **Set both before the first turn.**

### Let Gary write them

When an import finishes on a campaign with no setting yet, Gary offers a premise — the one
thing an import *cannot* file, because it is not a sentence in the document, it is what
the document is about:

```
Suggested setting:
> Siltgard, a drowned fen county under a dyke-lord whose sea walls are failing.
  Salt, conscription and slow retreat; low magic, wet, feudal.
`?dm accept` to take it, `?dm setting <premise>` to write your own.
Nothing is written until you do.
```

`?dm scene` with **no arguments** does the same for a scene, at any point in the campaign,
from where the story actually is:

```
?dm scene
```
```
Suggested scene:
> **The Eel-Runs, Siltgard** — Fever-mist off the ditches, tide sounding against the
  timbered wall. Two dyke-wardens watching the road.
`?dm accept` to take it, `?dm scene <location> | <standing state>` to write your own.
```

Then:

```
?dm accept
```

Both are suggestions and neither is written until you accept. `?dm accept setting` /
`?dm accept scene` takes one when both are waiting; typing your own `?dm setting` or
`?dm scene` discards the suggestion it overrules; ignoring it does nothing.

**Don't like it? Ask again.** Running `?dm scene` a second time is itself a rejection —
Gary is told what it already offered and asked for a different place or a different
moment, not the same one reworded. It remembers the last few, so you can keep re-rolling
until one lands.

### Or write them yourself

```
?dm setting Siltgard, a drowned fen county under a failing dyke. Low magic, wet, feudal.
?dm scene The Eel-Runs | Fever-mist off the ditches. Two dyke-wardens watching the road.
```

**Setting** is ≤600 characters and rides on *every single turn*, so it holds only what
stays true — premise, tone, region. Detail belongs in notes, which surface on mention.

**Scene** is re-read every turn too, so its summary is *what is true while you are there*,
not what happens when you walk in. "Rain on the shutters, barkeep watching the door" ages
well across an hour of play; "the door bangs open" does not.

### Set the clock

A fresh campaign opens at **midnight** on its epoch date. Without this the model narrates
an evening while the clock the same prompt calls authoritative says 00:00:

```
?dm time 18:30                       # later today
?dm time 4 Harvest Moon 1026 06:00   # a date, typed back the way the screen shows it
```

The calendar is the real one with a millennium taken off and the months under their
full-moon names — Wolf, Snow, Worm, Pink, Flower, Strawberry, Buck, Sturgeon, Harvest,
Hunter's, Beaver, Cold. A campaign started on 21 August 2026 opens on **21 Sturgeon Moon
1026**, so month lengths, leap years and seasons are all correct for free, and each month
names its own season instead of needing to be looked up. **The clock only moves forward.**

---

## 6. Sit down and play

### Register the players

Have everyone run `!sheet` once. Avrae's reply names the character and its level, and Gary
registers whoever asked for it — so one command everybody runs anyway is the whole of
setup. It persists to campaign state and survives restarts.

```
!sheet
```

`?dm playing <character name>` is the explicit override if a claim lands on the wrong
person. It takes the Discord user id from the message author, so it cannot be
misattributed.

Why this matters: Avrae's embed says `Caspian Varr makes a Sleight Of Hand check!`, and
correlation is name-based in practice. A check opened for a Discord *display* name matches
nothing — it waits out its TTL and is then narrated as hesitation, with no sign anything
went wrong.

### Talk to Gary

Gary stays quiet unless addressed, so five players chatting do not cost five model calls.
He answers when:

- you **@-mention** him (either the user or his auto-created role — both work), **or**
- the channel is in `GARY_FREEFORM_CHANNELS`, **or**
- you have a **check open** — your next message is read as an answer to it.

### A turn, end to end

```
@Gary  I want a look at what the wardens are guarding, without them seeing me.
```

Gary narrates, decides a check is needed, and posts the Avrae command for you to run:

```
Gary   The reeds are chest-high and the mist is with you, but the nearer warden keeps
       turning back towards the road…
       Caspian Varr: !check stealth
```

You run it. Avrae rolls. Gary reads the embed, compares against the DC **it persisted
before you rolled**, and narrates the consequence it committed to in advance:

```
!check stealth
Avrae  Caspian Varr makes a Stealth check! 1d20 (17) + 4 = 21
Gary   You are past them before the tide sounds again…
```

If nobody rolls, the check expires after `GARY_CHECK_TTL` (default 180s) and is narrated
as hesitation or the moment passing — never as a success. `?dm checks` shows what Gary is
waiting on; `?dm cancel` drops your own.

**In combat** Avrae owns initiative and durations, and the world clock stops. Gary mirrors
the tracker rather than keeping its own.

### When the enemy has had enough

Avrae fights everything to the death. Gary watches the tracker and works out when a
creature would rather run — from its SRD creature type, how badly it is hurt, how much of
its side is down, and whether it is outnumbered:

```
Gary  **BA1** breaks.
      _BA1 (Humanoid): pressure 5 vs nerve 3 — bloodied, 1 of its side down,
       outnumbered 3 to 2_
      Run it if you agree:
      `!init remove "BA1"`

      The bandit throws down the shortsword and goes over the dyke wall…
```

**It is a suggestion, not an action.** Gary cannot run Avrae commands — it never has —
so nothing happens until you run it. Ignore it and the bandit fights on; that is a
ruling, not a bug. When the creature that breaks is the last one standing you get
`!init end` instead, because the fight is over rather than one combatant short.

Undead, constructs, oozes, plants and swarms are never offered — they have nothing to
frighten. Beasts break soonest. A named NPC's disposition counts as *conviction* rather
than friendliness: a devoted ally and a creature that hates you both hold longer than the
mercenary between them.

Morale needs to know what the tracker's `BA1` actually is. Avrae builds that name itself —
`Bandit` becomes `BA1`, `Bandit Captain` becomes `BC1` — and Gary reproduces the rule to
match it back against the `!init madd` lines it posted when combat started.

It stays quiet rather than guessing. **A fight you set up by hand gets no morale**, nor
does a combatant you renamed with `-name`, nor two creatures Avrae would abbreviate the
same way (`Bandit` and `Basilisk` are both `BA`).

### When something goes wrong

| Symptom | Try |
|---|---|
| Gary says nothing at all | Is the channel in `GARY_GAME_CHANNELS`? Did you @-mention him? Is Message Content enabled? |
| Gary narrated but no dice | `?dm checks` — the check may already have expired |
| A roll did nothing | The name Avrae printed is not one Gary knows. `?dm playing <name>` |
| Gary invented the wrong world | `?dm state` — setting or scene is probably empty |
| Gary forgot something you filed | `?dm notes` — check it is filed under the subject you actually say |
| He wrote something you hate | `?dm undo` |

---

## 7. Start over

`?dm reset` ends a campaign and begins an empty one in the same server. It is the only
operation in Gary with **no undo** — it destroys the journal, so there is no record
afterwards to recover from.

### Try the smaller tool first

| You want | Use |
|---|---|
| The last change reversed | `?dm undo` |
| A different premise or scene | `?dm setting <premise>` / `?dm scene <location> \| <state>` — both overwrite in place |
| A botched import gone | `?dm reset`. An import writes one journal entry per record, so `?dm undo` would take dozens of goes |
| A genuinely new campaign | `?dm reset` |

### What goes, and what does not

| Wiped | Untouched |
|---|---|
| Setting, scene, world clock | **Your Avrae characters.** Sheets, HP and inventory are Avrae's; Gary never held them |
| Party roster, inventories, active effects | Your environment variables and channel config |
| Quests, NPCs, notes | The `campaign/` directory itself — the journal is truncated and a fresh snapshot written in place |
| Every channel's conversation history and rolling summary | Any copy of `campaign/` you took first — see step 1 |
| All pending checks, and any unaccepted suggestion | |

It is not per-channel: one Gary process is one campaign, and reset ends all of it.

### 1. Back it up

Reset truncates the journal in place, so take a copy first if there is any chance you
want the old campaign back:

```bash
cp -r ~/.local/share/gary/campaign ~/gary-backup-$(date +%F)
```

### 2. Ask for it

```
?dm reset
```

Gary answers with what is about to be destroyed and a four-digit code:

```
**This deletes the campaign.** World state, quests, NPCs, notes, inventories and
conversation memory all go — and so does the journal, so there is no record afterwards
and no undo. Currently: 14 note(s), 2 quest(s), 9 NPC(s).
Everything goes, including who is playing whom.
To confirm: `?dm reset 4821`
```

### 3. Confirm it

```
?dm reset 4821
```
```
Campaign reset. It is 21 Sturgeon Moon 1026, 00:00. Everyone run
`?dm playing <character name>` again before the first roll.
```

A fresh code is issued every time you run bare `?dm reset`, and a stale one is refused
with *"That code is not current."* Nothing is destroyed until the code comes back.

### 4. Open the new campaign

The clock re-anchors to **today's date, at midnight** — a reset on 21 August 2026 opens on
21 Sturgeon Moon 1026, 00:00 — so this is the same job as
[step 5](#5-open-the-campaign), from the top:

1. **Re-register the players.** Everyone runs `!sheet` again, or `?dm playing <name>`.
   The roster went with everything else. *(If `GARY_PLAYERS` is set, restarting Gary
   restores it instead — that is the one piece of setup an env var can hand back.)*
2. **`?dm import`** the new setting document, and `?dm accept` the premise Gary proposes
   from it.
3. **`?dm scene`** and **`?dm time`** before the first turn, for the reasons in
   [step 5](#5-open-the-campaign) — an empty setting and a midnight clock are exactly the
   conditions under which the model invents a world of its own.

---

## `?dm` command reference

`?dm help` prints this in the channel. So does `?dm` on its own, and any command Gary does
not recognise. The prefix is configurable (`GARY_COMMAND_PREFIX`); it may not start with
`!`, which is Avrae's.

### Opening a campaign

| Command | What it does |
|---|---|
| `?dm import` *(attach `.md`/`.txt`)* | Read a whole setting document; file people as NPCs, places/factions/lore as notes. Refuses before spending anything. Needs a model. |
| `?dm setting` | Show the current premise. |
| `?dm setting <premise>` | Set it. ≤600 chars, shown to the model every turn. |
| `?dm scene` | **Suggest** a scene from the campaign so far. Nothing is written. |
| `?dm scene <location> \| <standing state>` | Set it. The summary is re-read every turn. |
| `?dm accept` | Take whatever Gary has suggested here. |
| `?dm accept setting` / `?dm accept scene` | Take one when both are waiting. |
| `?dm time` | Show the world clock. |
| `?dm time 18:30` | Later today. |
| `?dm time 4 Harvest Moon 1026 06:00` | A date. Also accepts `1026-09-04 06:00` and `day 2 06:00`. Forward only. |

### At the table

| Command | What it does |
|---|---|
| `?dm playing <character name>` | Claim a character, spelled exactly as Avrae spells it. Usually unnecessary — `!sheet` does it. |
| `?dm checks` | Every check Gary is waiting to see rolled, and how long each has left. |
| `?dm cancel` | Drop **your own** pending check. Each player cancels their own. |
| `?dm undo` | Reverse the last change to the world. |

### Looking in

| Command | What it does |
|---|---|
| `?dm state` | Clock, scene, party, and counts of what is stored. |
| `?dm notes` | What has been filed, by subject, plus the people. These are the words that retrieve them. |
| `?dm stats` | Messages sent, turns narrated, narrator failures, Avrae parse drops. A rising `unknown` drop rate is how an Avrae format change announces itself. |
| `?dm help` | This reference. |

### Starting over

| Command | What it does |
|---|---|
| `?dm reset` | **Deletes the campaign** — state, quests, NPCs, notes, inventories, the roster, the journal, **and every channel's conversation history.** Two-step: it replies with a code you type back. No undo. See [Start over](#7-start-over). |

---

## Configuration reference

Everything is environment variables; there is no config file. Only the first two are
required.

### Required

| Variable | Notes |
|---|---|
| `DISCORD_TOKEN` | Bot token. Empty is a fatal config error. |
| `GARY_GAME_CHANNELS` | Channel ids, separated by commas or spaces. Empty is fatal — Gary would join and ignore every message. |

### Discord

| Variable | Default | Notes |
|---|---|---|
| `GARY_FREEFORM_CHANNELS` | *(none)* | Channels where Gary answers without being @-mentioned. Must be a subset of `GARY_GAME_CHANNELS`. |
| `GARY_PLAYERS` | *(none)* | `<discord id>=<Avrae character>`, `;` between entries. Unset skips actor validation. |
| `GARY_COMMAND_PREFIX` | `?dm ` | May not start with `!` — that collides with Avrae. |
| `GARY_AVRAE_BOT_ID` | `261302296103747584` | Avrae's application id. |
| `GARY_COINS_COMMAND` | `game coins` | Override only to use a workshop alias; the native command needs nothing installed. |

### Storage

| Variable | Default | Notes |
|---|---|---|
| `GARY_CAMPAIGN_ROOT` | `~/.local/share/gary/campaign` | Journal, snapshots, pending checks, history. Honours `$XDG_DATA_HOME`. Outside the repo so a `git clone` cannot destroy it; `/campaign` inside the image. |
| `GARY_BACKEND` | `file` | `file` or `firestore`. |
| `GARY_FIRESTORE_CAMPAIGN_ID` | `default` | Which campaign document. |

### Model

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | *(none)* | Unset runs `EchoNarrator` — full Discord path, zero spend. |
| `GARY_GEMINI_MODEL` | `gemini-3.7-flash` | Narration. Verified 2026-08-18; model ids move, recheck. |
| `GARY_GEMINI_SUMMARY_MODEL` | `gemini-3.5-flash-lite` | Rolling summary. Verified 2026-08-09. |
| `GARY_GEMINI_TIMEOUT` | `90.0` | Seconds. Bounds every call; without one a hung request blocks the turn forever. |
| `GARY_GEMINI_FALLBACK_MODEL` | `gemini-3.6-flash` | Tried once when the primary exhausts its retries. `gemini-3.7-flash` spent 2026-08-21 answering 503 "high demand". Empty string disables it. |
| `GARY_HISTORY_WINDOW` | `30` | Turns kept verbatim; older ones fold into the rolling summary. |

### Timing

| Variable | Default | Notes |
|---|---|---|
| `GARY_CHECK_TTL` | `180.0` | Seconds a check stays rollable before it is narrated as hesitation. |
| `GARY_SWEEP_INTERVAL` | `15.0` | How often expiries are swept. |
| `GARY_SEND_INTERVAL` | `1.0` | Per-channel send pacing. |
| `GARY_MAX_EFFECT_DEPTH` | `4` | Bounds model recursion. Exceeding it is logged loudly — it means the model is opening checks in a loop. |

Settings are validated at boot and fail fast: a freeform channel that is not a game
channel, a prefix starting with `!`, and an unknown backend are all startup errors rather
than mysteries at the table.

---

## Design

How Gary works, and why it is built this way, is [`ARCHITECTURE.md`](ARCHITECTURE.md).
The short version, with the sections that carry the argument:

- **Outcomes are not decided by the model.** A DC and both consequences are persisted
  before anyone is told to roll; resolution is arithmetic. §5.1, §5.2
- **A check is a deferred function call.** The model's `open_check` is left unanswered
  until the roll arrives as its `function_response`, minutes later. §12.4
- **Trust rides on the transport, not on strings.** Roll results arrive as structured
  tool results; player text and imported documents are sanitised, fenced and labelled as
  data. §9.1
- **Only `AWAITING_ROLL` consumes Avrae output**, which is what stops `!help` and other
  players' rolls from each costing a model call. §4, §6.3
- **A proposal decides nothing.** `?dm scene` and the post-import premise run the model
  while it holds every state-mutating tool, so nothing they produce is written until you
  accept it. §8.1.1
- **There is one writer.** The store is used in-process and guarded by a lock; why a
  second client was built and then removed is §2.1
- **Nothing important lives on the box.** State sits behind storage protocols with
  in-memory, file and Firestore implementations. §7.4, §11.1

The source map is §12.7, re-capturing Avrae fixtures is §6.5, and the open risks —
including the Firestore document ceiling and Avrae version drift — are §14.
