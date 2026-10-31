# Gary

<img src="images/gary.jpg" alt="Gary" width="600">

An AI Game Master over Discord. Gemini writes the narrative, Gary rolls the dice and
enforces the maths, and a local state server owns the persistent world.

> **This branch (`dungeon-world`) runs Dungeon World.** Avrae and D&D 5e are gone: Gary
> rolls its own dice when a player clicks **Roll**, and runs the basic moves, every
> class's starting moves (spells, the animal companion and Shapeshifter included), Last
> Breath, Make Camp, End of Session and Level Up, with sheets, gear, coin, monsters, XP,
> bonds and alignment. Some rules are still missing — see
> [what is missing](#what-is-not-built-yet).

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
.venv/bin/python -m pytest -q          # 413 passed
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

A published image saves the venv entirely. It pins Python 3.12 and carries the adapters,
so the only thing you supply is config and somewhere to keep the campaign.

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

Prove the whole Discord path — intents, routing, state, sending — before spending a
single token. Leave `GEMINI_API_KEY` unset and Gary uses `EchoNarrator`, which reports
what it heard in flat text:

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

In the channel, @-mention Gary. If he answers `[echo] heard <your name>.` the plumbing is
sound and the only thing left is the model.

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
# Who plays whom. Without this the model only ever hears Discord display names, and a
# check goes to whoever spoke. ';' between players.
export GARY_PLAYERS='111111111111111111=Caspian Varr;222222222222222222=Liriel'

# Channels where Gary answers every message instead of only when @-mentioned.
# Must be a subset of GARY_GAME_CHANNELS.
export GARY_FREEFORM_CHANNELS='123456789012345678'
```

`GARY_PLAYERS` is usually unnecessary — see [step 6](#6-sit-down-and-play), where
`?dm new` does the same job from the table.

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

### Make your characters

Everyone runs:

```
?dm new
```

Gary posts a **Create character** button for you. Clicking it opens a form only you can
see: pick a class and a race, place the Dungeon World stats (16, 15, 13, 12, 9, 8) one at
a time, choose your class's starting gear (a Fighter builds their signature weapon), and
name your character. Gary works out your hit points and damage die, saves the
sheet, and tells the table who has joined. `?dm sheet` shows yours again.

Making a character also claims it: its checks are yours to roll, and nobody else's. Why it
matters: without a claim the model only knows Discord display names, and a check goes to
whoever happened to be speaking. `?dm playing <character name>` claims a character without
a sheet, for a table that keeps its sheets elsewhere.

The sheet tracks what rolls need — class, race, stats, HP, damage, armor, level — and your
gear. Gear does real work: your best armor plus a shield is your armor; a weapon's +damage
and piercing go into your damage roll; Hack and Slash needs a hand, close or reach weapon
and Volley a near, far or thrown one (and ammo for a bow); taking several shots on a Volley
spends ammo. A character made before gear existed runs `?dm gear` once to pick theirs.
**Alignment.** `?dm new` asks for one from your class's list, each with what fulfils it
("Good: endanger yourself to heal another"); a character made before that runs
`?dm alignment`. The GM sees it.

**Debilities.** When the fiction weakens you — a numbing claw, a sickening poison, a blow to
the head — the GM gives a debility: Weak, Shaky, Sick, Stunned, Confused or Scarred, each
-1 to one stat's rolls until cured, with the cause shown to the table. Your sheet lists
them and every roll shows the cost ("+Str +1 (-1 Weak)"). The GM removes one when magic or
real treatment cures it.

**Town.** Buying something readily available is the book's price. Something special, rare
or magical is the **Supply** move first (+Cha): a 10+ finds it at a fair price, a 7–9 costs
more (at most twice the book's) or something similar is offered, and for an item the book
doesn't price the GM names it, up to 500. **Carouse**: return triumphant and throw a party
for 100 coins or more, +1 for each extra 100; pick what comes of it (a useful friend,
rumours, information, not being tricked) — three on a 10+, one otherwise, and a miss gets
out of hand. The coin goes when the party does.

**Fronts.** The GM writes the threats that move on their own as fronts — dangers with an
impulse, a few grim portents and an impending doom — and ticks portents off as the story
goes, including while the party is elsewhere. Gemini is shown each danger's next portent
every turn, so it can foreshadow it. Players never see them; `?dm fronts` offers a button
that shows them privately to whoever clicks it. Every change undoes with `?dm undo`.

**Outstanding Warrants and Bolster.** Returning somewhere you've caused trouble, the GM
calls Outstanding Warrants (+Cha): on a hit everyone recognises you, and on a 7–9 the GM adds
a complication — a warrant, a price on your head, or someone you care about in a bad spot.
Spending weeks in study, meditation or hard practice is Bolster: a week or two gives 1
preparation, a month or more 3, shown on your sheet; each is +1 to one roll when the GM says
it pays off ("+1 preparation"). The clock moves the weeks.

**Hirelings.** Put out word that you're hiring and the GM calls Recruit: +1 each for
generous pay, saying what you're setting out to do, offering a share, and a useful
reputation. Whoever joins is made by the book — skills (Adept, Burglar, Minstrel, Priest,
Protector, Tracker, Warrior) with ranks, loyalty and a cost, their points set by where you
found them (village 5 up to city 10) — and kept on your sheet. Ordering one into something
dangerous or degrading is Order Hirelings, +their loyalty (-1 while their pay lapses). The
GM sees each skill's rule, and Gary applies the ones with numbers: a hired Priest adds their
skill to camp healing and can give first aid (2×skill HP, -1 forward); a Warrior aiding your
attack adds their skill to damage and takes the counter-attack; a Protector helping you Defy
Danger gives +1 (but a 10+ counts as 7–9), or standing sentry adds their skill to your armor
and then tires until they rest; a Tracker guides any journey shorter than their skill to
success; a Minstrel takes their skill off prices in town. Burglars and Adepts are the GM's.

**Encumbrance.** Your load is your class's base plus Str (a Fighter 12+Str, a Wizard
7+Str); everything you carry has a weight, and 100 coins weigh 1. The sheet shows "weight
11/13". One or two over and every move takes -1; more than two over and the roll waits on
your choice — drop something (a button per item) and roll at -1, or keep it all and fail.

**Perilous Journey.** Travelling through hostile territory, the GM gives the jobs to
different characters — trailblazer, scout, quartermaster — or asks who takes which. Rations
for the expected days are packed at once, and each job's holder rolls +Wis with their own
button: a trailblazer's 10+ shortens the trip, a scout's lets you get the drop on trouble,
a quartermaster's saves a ration. A job nobody holds counts as a 6, so a lone traveller
takes one and the GM makes moves for the rest. The GM moves the clock for the trip's real
length.

**Recover.** Days of rest somewhere comfortable and safe — an inn, a temple — are Recover,
not camp: the GM says how many days and whether a healer tends you, and the clock moves
that far. A day heals everyone resting to full; every three days clears a debility (every
two under a healer), and when there are more debilities than the rest clears, you pick
which with buttons only you can press.

**Forward.** "+1 forward" waits on your sheet with what it's for ("when acting on what
Discern Realities showed", "against the ogre") until the GM says a move acts on it — then
it's added and spent ("roll +Str +1 (+1 forward)"). Discern Realities, Take Watch, I Am the
Law, Backstab's advantage, Defend's "open the attacker up" and Arcane Art's +1d4 damage grant
them; the GM gives or takes them as the fiction earns it. They expire at camp, rest or the
start of a journey.

**Packs and free blows.** When several creatures strike one character at once, the GM
rolls them as one blow — the biggest damage die among them, once per attacker, the highest
counting — so a pack is dangerous without being certain death. And when the fiction hands
you the blow (a pinned, helpless or surprised enemy), the GM has you just deal your damage,
no roll, with the reason shown.

**Defend.** Defend names who or what you protect ("Defend Eva", "Defend the gate"): hold 3
on a 10+, 1 on a 7–9, shown on your sheet. When a creature attacks you or the character you
defend, Gary rolls the blow and holds it while you choose — redirect it to yourself, halve
it, open the attacker up (+1 forward to an ally), strike back for your level, or spend
nothing. No answer spends nothing. Hold runs out, or ends when you make camp.

**Class choices.** `?dm new` also asks what your class decides: a Cleric names their god
and picks a domain and precept, a Druid their land and tell, a Bard their lore, a Thief
their poison (three uses to start), a Fighter their weapon's look — and an Elf Fighter a
weapon type that is always precise, so it hacks and slashes with Dex. A character made
before runs `?dm choices`. The GM sees them all; a Thief's poison is spent with each use
and brewed three at a time somewhere quiet.

**Animal Companion.** A Ranger runs `?dm companion` (Gary reminds them after `?dm new`): a
private form for species, base, strengths, trainings and weaknesses — as many as its
numbers say — and a name. When you work with it ("Fang and I go for the goblin"), the GM
flags the move and Gary checks the training and applies Command: +ferocity to damage and
its armor against the blow back on an attack (fight humanoids or monsters), +cunning to
Hunt and Track (hunt, search), Discern Realities (search, scout, guard) and Parley
(perform). The roll line says so ("+1 Fang"). Its instinct on someone interfering with you
is left to the table.

**Shapeshifter.** A Druid who calls on the spirits rolls +Wis into the form named ("into a
bear"): hold 3, 2, or 1 even on a miss. The GM names the form's moves, which Gary records
and shows on your sheet ("in the shape of a bear (hold 2: maul, roar)"); each use costs 1
hold with no roll, and at 0 you turn back. You can turn back early. Which species you
know (your land, Studied Essence) is the GM's to judge — your land isn't recorded yet.

**Bonds.** Once there's another character at the table, run `?dm bonds`: a private form
offers your class's bond lines ("___ has insulted my deity; I do not trust them"), you pick
the companion who fills each blank, and save. Write at least one; remove one to rewrite it.
Aid or Interfere names who you help or hinder and rolls +Bond — the number of bonds you
have with them. `?dm sheet` shows your bonds, and the GM sees them.

**Spells.** A new Cleric or Wizard gets a **Choose spells** button after joining: a Wizard
fills their spellbook (three first-level spells), a Human picks one spell from the other
class's list, and both choose what they hold — total levels up to level + 1. Rotes and
cantrips come free. A caster made before spells existed runs `?dm spells` for the same
button; `?dm spells` and `?dm sheet` show what you hold, what's lost and what's running.
New spells come only with an undisturbed hour in the story — say your character prays or
studies, and the GM calls Commune or Prepare Spells (refused mid-fight, like camp), which
clears your spells and posts the button again. Casting is a move: +Wis for a Cleric, +Int
for a Wizard, only a spell you hold. On a 7–9 you choose attention, −1 ongoing to cast
until you next prepare, or losing the spell until then. Each running ongoing spell costs −1
to cast (Invisibility, Dominate and Mimic forbid casting) until the GM ends it. Gary rolls
what spells do to numbers: Cure Wounds heal, Magic Missile and Fireball (every creature
named, ignoring armor) and Harm (which burns you too) deal damage, Bless gives its target
+1 while creatures stand, Magic Weapon adds 1d4 to the caster's weapon. The rest the GM
narrates from the spell's text.

**XP.** Every miss marks 1 XP — Gary says so under the roll, with your total ("5/8 XP").
When you wrap up, run `?dm endsession`: anyone can flip the three questions (learned
something important, overcame a notable enemy, looted a memorable treasure), and each
player ticks whether their own character acted on their alignment (only they can). A
player can also **Resolve a bond**: pick it privately, and the companion it names gets an
Agree button only their player can press. Award XP gives everyone 1 XP per yes, plus 1 for
alignment and 1 for a resolved bond — which is cleared, so write a new one with `?dm bonds`. `?dm sheet` shows XP against what you need
(level + 7).

**Level Up.** With enough XP, run `?dm levelup` — refused while creatures stand or a move is
pending, because levelling takes downtime. A private form asks for a stat to raise (18 at
most; Constitution raises HP too), an advanced move from your class's list (the 6–10 list
opens at level 6; "Requires" and "Replaces" are honoured), and for a Wizard a spell for the
spellbook. The XP is spent and the level is announced to the table.

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

Gary decides your action triggers a move and asks you to roll:

```
Gary   **Caspian Varr** — Defy Danger, roll +Dex +2.   [🎲 Roll]
       The reeds are chest-high and the mist is with you, but the nearer warden keeps
       turning back towards the road…
```

You click **Roll**. Gary rolls 2d6 plus the stat from your sheet where everyone can see
it. The move fixed what each result means before you rolled — 10+ a strong hit, 7–9 a
weak hit, 6 or less a miss — and Gary narrates that:

```
Gary   🎲 **Caspian Varr** rolls 4 + 3 +2 = 9 — weak hit.
Gary   You make the far bank, but a stone turns under your boot. The warden's head comes
       round. Do you freeze, or run for the willows?
```

On a miss, Gary makes a GM move: something goes wrong, as hard as the fiction allows.

**Some results are yours to choose from.** A weak-hit Volley, a strong-hit Hack and
Slash, Discern Realities and Aid or Interfere all offer options. Gary posts them as
buttons, or as one dropdown when you pick several (Discern Realities' three questions),
and waits for you before narrating:

```
Gary   🎲 **Liriel** rolls 3 + 2 +2 = 7 — weak hit.
       Choose one (whichever you choose you deal your damage).
       Pick below, **Liriel**.
       [Move to get the shot…] [Take what you can get: -1d6 damage.] [Take several shots…]
```

Only you can roll your own move or make its choice, and only once: anyone else who clicks
is told privately whose it is. Buttons keep working across a Gary restart, like the moves
behind them — even between the dice and the choice.

**Fights have numbers.** When creatures appear, Gary puts them in the scene from the Dungeon
World bestiary — "Goblin 1, Goblin 2", each with its own HP. Hack and Slash and Volley are
aimed at one of them. A hit gives you a **Roll damage** button; your class's damage die
(plus or minus a d6 if your choice said so) goes through its armor, and a weak-hit Hack and
Slash means it hits you back in the same breath:

```
Gary   **Thorin**, roll your damage (d6) against Goblin 1.   [⚔️ Roll damage]
Gary   🎲 **Thorin** damage — d6: 5 → Goblin 1: 4 through armor 1, HP 3 → 0 — **defeated**.
```

**Loot, buying and selling.** Pick something up in the fiction and Gary adds it to your
sheet. If the book lists it — a long sword, plate, a bundle of arrows — it comes with its
real stats, so a found long sword hits harder and plate is armor at once; anything else (a
silver locket) is carried with no effect on the rules. Coin is on your sheet. Buying costs
the book's price, refused if you can't afford it; selling fetches half, or less from a
stingy buyer, never more. Dropping a weapon or armor changes what you can do immediately.
Every pickup, purchase and sale is posted to the table and undoes with `?dm undo`.

**Healing items** work as the book says. A healing potion heals whoever drinks it 10 —
yourself, or a friend you pour it into. Bandages heal *someone else* 4, poultices and herbs
heal anyone 7; both spend a use and are slow, so not while creatures are still standing.

A creature is defeated at 0 HP and not before, however the narration reads. When a
creature hurts you as the GM's move, Gary rolls that too and takes it off your sheet's HP.
`?dm undo` takes a blow back like any other change.

**At 0 HP you roll Last Breath** — Gary posts the button the moment you fall, and rolls it
for you if you don't click. +nothing: Death doesn't care how tough you are. On a 10+ you
cheat death and wake at 1 HP. On a 7–9 the GM describes what you glimpse beyond the Black
Gates and names Death's price; then you choose — **Take** the bargain and stabilize at 1
HP, or **Refuse** and pass on. On a miss you are marked as Death's own, and the GM will
tell you when. Anyone down or dead can't make moves.

**Make Camp heals.** When the party beds down, Gary eats a ration for each of you and, if
the night is quiet, heals everyone who ate half their max HP and moves the clock eight
hours. No ration, no healing. If something comes in the night, whoever is on watch rolls
**Take Watch** (+Wis) and nobody heals. You can't make camp with creatures still standing or while
anyone's roll is unresolved (a Last Breath above all); anywhere else unsafe — a chase, a
storm — the GM refuses the rest or lets something come for you. Every camp says where it is
and why it's safe, so a bad one is easy to spot and `?dm undo`.

If nobody rolls, the move expires after `GARY_CHECK_TTL` (default 180s) and is narrated as
hesitation or the moment passing — never as a success. If you rolled but never chose or
never rolled damage, the dice stand: Gary takes the first option(s), rolls the damage for
you, and says so. `?dm checks` shows what Gary is
waiting on; `?dm cancel` drops your own.

### What is not built yet

- **Class moves: starting moves only.** Rolled class moves work —
  Turn Undead, Lay on Hands, I Am the Law, Hunt and Track, Called Shot, Trap Expert,
  Tricks of the Trade, Backstab, Bend Bars, Arcane Art, Cast a Spell — for that class
  alone. The rest are shown to the GM to honour in the story. Called Shot and Arcane Art
  pick their effect after the roll rather than before. Of the creation choices only the
  Elf Fighter's precise weapon and the Thief's poison uses do rules work; the rest (a
  Cleric's deity and petition, a Druid's land, a Bard's lore) are the GM's to honour, and
  the poisons' effects are narrated, not tracked on creatures.
- **Spells, as far as the numbers go.** Sanctuary's +1d4 healing, Bless ending with the
  battle and spells' own conditions (Charm Person broken by damage) are the GM's to
  apply — it ends spells with `end_spell`. A Human's borrowed spell is chosen from levels
  1–5 only (Discord lists 25 options at most, and nobody reaches level 7 yet). Only
  Fireball hits several creatures.
- **Special moves:** all of the book's are in. Burglar and Adept hireling skills are the GM's
  to apply, and preparation is not spent on Cast a Spell, Recruit or Carouse rolls.
  Encumbrance applies to the moves you make, not to ones the rules trigger (Last Breath,
  Take Watch, a journey's jobs). End of Session covers the three
  group questions, alignment and bonds. A journey's length is the GM's to set on the clock.
- **About a quarter of advanced moves are wired.** Gary applies the ones that change a number
  it runs: armor (Iron Hide, Barkskin, Arcane Ward, Underdog…), extra damage (Merciless,
  Cheap Shot…), a Druid's bigger animal die, extra healing (Hospitaller, Healing Song,
  Devoted Healer), Serenity and Providence on spell penalties, First Aid's rote, Staunch
  Defender's hold, Embracing No Form's hold, and the +1s and +1 forwards with a clear
  trigger (Seeing Red, Voice of Authority, A Safe Place, Bamboozle…). The GM is told which
  it need not apply again. The rest — conditional ones like "while on a quest" or "when
  acting on the answers" — stay with the GM. A move that names another ("choose one
  spell", a move from another class) is recorded without that second choice — tell the GM
  which.
- **Defend's hold** is spent one point per attack, and only against `monster_attacks` — not
  the blow back on a weak Hack and Slash. Defending an item or a place records it, but
  nothing attacks those through the rules. Ammo is one count shared by bows and throwing daggers, and
  weight and load are not tracked. Antitoxin and poisons bought from the equipment list do
  nothing yet, and a healing potion only heals HP (the book lets it cure a debility instead). There are no magic items yet.

### When something goes wrong

| Symptom | Try |
|---|---|
| Gary says nothing at all | Is the channel in `GARY_GAME_CHANNELS`? Did you @-mention him? Is Message Content enabled? |
| Gary narrated but no dice | `?dm checks` — the check may already have expired |
| Your roll did nothing | It may not be your check — `?dm checks` shows whose it is |
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
| Setting, scene, world clock | Your environment variables and channel config |
| Party roster, character sheets, inventories, active effects | The `campaign/` directory itself — the journal is truncated and a fresh snapshot written in place |
| Quests, NPCs, notes | Any copy of `campaign/` you took first — see step 1 |
| Every channel's conversation history and rolling summary | |
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
Everything goes, including every character sheet and who is playing whom.
To confirm: `?dm reset 4821`
```

### 3. Confirm it

```
?dm reset 4821
```
```
Campaign reset. It is 21 Sturgeon Moon 1026, 00:00. Characters went with it: everyone
run `?dm new` before the first roll.
```

A fresh code is issued every time you run bare `?dm reset`, and a stale one is refused
with *"That code is not current."* Nothing is destroyed until the code comes back.

### 4. Open the new campaign

The clock re-anchors to **today's date, at midnight** — a reset on 21 August 2026 opens on
21 Sturgeon Moon 1026, 00:00 — so this is the same job as
[step 5](#5-open-the-campaign), from the top:

1. **Make the characters again.** Everyone runs `?dm new`.
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
`!`, which most other bots claim.

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
| `?dm new` | Make a character in a private form. It is yours: its checks are yours to roll. |
| `?dm sheet` | Your character's sheet. |
| `?dm gear` | Choose starting gear for a character made before gear existed. |
| `?dm spells` | A Cleric's or Wizard's spells, and the Choose spells button the first time. |
| `?dm choices` | Your class's creation choices, for a character made before them. |
| `?dm fronts` | The threats the GM is tracking, shown privately to whoever clicks (spoilers). |
| `?dm companion` | A Ranger's animal companion: a private form, or shows the one you have. |
| `?dm alignment` | Choose your alignment, for a character made before alignments existed. |
| `?dm bonds` | Write your bonds with the other characters: a private form with your class's lines. |
| `?dm levelup` | Spend XP on a level in downtime: a private form for a stat, an advanced move and (Wizards) a spell. |
| `?dm endsession` | The book's three end-of-session questions; the table toggles the answers and everyone gets 1 XP per yes. |
| `?dm playing <character name>` | Claim a character without a sheet. Its checks are yours to roll. |
| `?dm checks` | Every check Gary is waiting to see rolled, and how long each has left. |
| `?dm cancel` | Drop **your own** pending check. Each player cancels their own. |
| `?dm undo` | Reverse the last change to the world. |

### Looking in

| Command | What it does |
|---|---|
| `?dm state` | Clock, scene, party, and counts of what is stored. |
| `?dm notes` | What has been filed, by subject, plus the people. These are the words that retrieve them. |
| `?dm stats` | Messages sent, turns narrated, narrator failures, outcomes recorded. |
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
| `GARY_PLAYERS` | *(none)* | `<discord id>=<character name>`, `;` between entries. Unset skips actor validation. |
| `GARY_COMMAND_PREFIX` | `?dm ` | May not start with `!` — most other bots claim it. |

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

## Playtesting without Discord

`scripts/playtest.py` plays scripted scenes against the real rules and the real Gemini, in a
throwaway campaign: player lines go in as messages, and every button Gary posts is clicked
by the player it belongs to. It writes a Markdown transcript with each tool call and its
answer, and flags tool errors, refused moves, stalls, model loops and crashes.

```bash
set -a && . ~/.config/gary/env && set +a
PYTHONPATH=src .venv/bin/python scripts/playtest.py fight --out fight.md   # or discern, camp, journey, town, all
```

Add `--llm-players` and a second model plays the characters instead, each with a goal for the
scene, reacting to what Gary says — so a scene that escalates is played through rather than
left behind. It spends real Gemini calls, and cannot see Discord itself (how buttons and
dropdowns look).

## Design

How Gary works, and why it is built this way, is [`ARCHITECTURE.md`](ARCHITECTURE.md).
The short version, with the sections that carry the argument:

- **Outcomes are not decided by the model.** It names the move; the stat, modifier, target
  and spell are fixed before anyone is told to roll, and the result is the move's band.
  §5.1, §5.2
- **A move is a deferred function call.** The model's `make_move` is left unanswered until
  the roll arrives as its `function_response`, minutes later. §5.6
- **Trust rides on the transport, not on strings.** Roll results arrive as structured
  tool results; player text and imported documents are sanitised, fenced and labelled as
  data. §9.1
- **Only the player a move belongs to can roll it, and only once.** Gary rolls the dice
  itself when they click, so there is no roll to parse and no name to match. §5.3, §9.2
- **A proposal decides nothing.** `?dm scene` and the post-import premise run the model
  while it holds every state-mutating tool, so nothing they produce is written until you
  accept it. §8.1
- **There is one writer.** The store is used in-process and guarded by a lock; why a
  second client was built and then removed is §2.1
- **Nothing important lives on the box.** State sits behind storage protocols with
  in-memory, file and Firestore implementations. §7.4, §11.1

The rules engine is §6, the source map §12.7, and the open risks — including the
Firestore document ceiling — §13.
