# Gary

<img src="images/gary.jpg" alt="Gary" width="600">

An AI Game Master for **Dungeon World**, played over Discord. Gemini narrates; Gary owns
the rules, rolls the dice when a player clicks **Roll**, and keeps the world — characters,
creatures, the clock, the journal — in a local campaign store.

- How each rule plays at the table: [`RULES.md`](RULES.md)
- How Gary is built, and why: [`ARCHITECTURE.md`](ARCHITECTURE.md)

---

## What you need

- **A Discord bot** with the **Message Content** privileged intent enabled in the Developer
  Portal, invited to your server with the **`bot`** and **`applications.commands`** scopes
  (the second makes the slash commands appear). Without Message Content, Gary hears nothing
  and says nothing — the most common reason a correct setup stays silent.
- **A game channel's id** — right-click the channel → Copy Channel ID (turn on Developer
  Mode in Discord's settings first).
- **Python 3.11+**, or Docker.
- **A Gemini API key** — optional at first. Without one Gary runs a stand-in narrator that
  echoes what it heard, so you can test the Discord side for free.

## Install and run

Put your settings in a file outside the checkout, so deleting a clone can't take your token:

```bash
mkdir -p ~/.config/gary && chmod 700 ~/.config/gary
cp .env.example ~/.config/gary/env && chmod 600 ~/.config/gary/env
$EDITOR ~/.config/gary/env    # DISCORD_TOKEN, GARY_GAME_CHANNELS, and GEMINI_API_KEY when ready
```

**With Python:**

```bash
git clone <this repo> && cd gary
python -m venv .venv
.venv/bin/pip install -e '.[discord,llm,dev]'

set -a; . ~/.config/gary/env; set +a
.venv/bin/python -m gary.adapters.discord_bot
```

**With Docker:**

```bash
docker compose up -d && docker compose logs -f
```

That runs `ghcr.io/magnentius/gary:latest` with your settings file and keeps the campaign
in `~/.local/share/gary/campaign`. Run **one** Gary per campaign.

When Gary connects, its log says `registered 17 slash command(s) in <your server>`. In the
game channel, type `/` to see them, or @-mention Gary to check he's listening.

## Your first session

1. **Give the world a premise.** `/import` a setting document (a `.md` or `.txt` file —
   [Kenji's Setting Generator](https://cros.land/rpg-setting-generator-and-world-building-tool/)
   exports one with **Copy as Markdown**; generate its Locations, Factions and Quest Hooks
   tabs first). Gary files its people as NPCs and its places and lore as notes, then offers a
   premise with an **Accept** button. Or write one: `/setting premise:`.
2. **Set the scene.** `/scene` suggests one from the story so far; `/scene location: summary:`
   sets your own. The summary is what's true while you're there ("rain on the shutters").
3. **Set the clock** if it matters: a new campaign starts at midnight. `/time when:18:30`.
4. **Make characters.** Everyone runs `/new` — a private form for class, race, stats, gear
   and the rest. The character is then yours: only you roll its moves.
5. **Play.** Talk to Gary by @-mentioning him. When what you do triggers a move, he posts a
   **Roll** button; click it and Gary rolls 2d6 + your stat in front of everyone, then
   narrates the result.
6. **Wrap up** with `/endsession`: the table answers the end-of-session questions and
   everyone gets their XP. Level up from `/sheet` when you have the XP.

## Slash commands

| Command | What it does |
|---|---|
| **Setting up** | |
| `/import document:` | Read a setting document (`.md`/`.txt`) and file its people, places and lore. |
| `/setting` | Show the campaign premise; `premise:` sets it. |
| `/scene` | Suggest a scene to accept; `location:` and `summary:` set your own. |
| `/time` | Show the world clock; `when:` moves it forward (`18:30`, `4 Harvest Moon 1026 06:00`). |
| **At the table** | |
| `/new` | Make a character, in a private form. |
| `/sheet` | Your sheet, privately, with buttons for spells, bonds, companion, alignment and level-up. |
| `/playing character:` | Claim a character someone else made. |
| `/checks` | What Gary is waiting on, with a button to cancel your own move. |
| `/dice` | How the table's dice have fallen, against fair 2d6; `character:` for one character. |
| `/undo` | Reverse the last change to the world. |
| `/endsession` | The end-of-session questions, and everyone's XP. |
| `/questions` | Show the end-of-session questions; `first:` `second:` `third:` set this adventure's own, `default:True` restores the book's. |
| **For the GM** | |
| `/fronts` | The threats the GM is tracking, shown only to you (spoilers). |
| `/state` | Clock, scene, party, steadings, what's stored, and Gary's counters. |
| `/notes` | What's been filed, by subject. |
| `/help` | This list, privately. |
| `/reset` | **Delete the campaign.** Two steps (`/reset`, then `/reset code:`), and no undo. |

`/new`, `/sheet`, `/fronts` and `/help` answer only you. A message typed the old way
(`?dm …`) gets a pointer to the slash commands.

## How play works

- **Gary answers when** you @-mention him, when you have a move waiting on you, or every
  message in a channel listed in `GARY_FREEFORM_CHANNELS`.
- **Gemini names the move; Gary decides what it means.** The stat, modifier and target are
  fixed before you roll, and the result is the book's 10+ / 7–9 / miss. Choices (a weak-hit
  Volley, Discern Realities' questions) come as buttons only you can press.
- **Gary keeps the numbers:** HP, damage through armor, creatures from the book's bestiary,
  spells, gear, coin, XP, bonds, debilities, hirelings, steadings and more. Gemini narrates
  what they mean. [`RULES.md`](RULES.md) has the details, and what stays with the GM.
- **Every change is journalled**, so `/undo` takes back a blow, a purchase or a bad import
  step, and the campaign survives restarts.

## Starting over

`/reset` deletes the whole campaign — characters, world, notes and every channel's memory —
and there is no undo. Back it up first if you might want it again:

```bash
cp -r ~/.local/share/gary/campaign ~/gary-backup-$(date +%F)
```

Run `/reset`, then confirm with the code it gives you: `/reset code:`. Then start again from
[your first session](#your-first-session).

## When something goes wrong

| Symptom | Try |
|---|---|
| Gary says nothing | Is the channel in `GARY_GAME_CHANNELS`? Is Message Content enabled? Did you @-mention him? |
| No slash commands | Re-invite Gary with the `applications.commands` scope, then restart him. |
| Narration but no Roll button | `/checks` — the move may have expired (after 3 minutes, by default). |
| Your click did nothing | It may be someone else's move; `/checks` shows whose. |
| Gary invented the wrong world | `/state` — the setting or scene is probably empty. |
| He wrote something you hate | `/undo`. |

## Configuration

Everything is an environment variable. Only the first two are required.

| Variable | Default | |
|---|---|---|
| `DISCORD_TOKEN` | — | The bot token. |
| `GARY_GAME_CHANNELS` | — | Channel ids Gary plays in, separated by commas or spaces. |
| `GEMINI_API_KEY` | *(none)* | Without it, the free echo narrator. |
| `GARY_FREEFORM_CHANNELS` | *(none)* | Channels where Gary answers every message; must also be game channels. |
| `GARY_PLAYERS` | *(none)* | Pre-assign characters: `<discord id>=<name>;…`. Usually `/new` does this. |
| `GARY_CAMPAIGN_ROOT` | `~/.local/share/gary/campaign` | Where the campaign lives. |
| `GARY_BACKEND` | `file` | `file`, or `firestore` (with `GARY_FIRESTORE_CAMPAIGN_ID`). |
| `GARY_GEMINI_MODEL` | `gemini-3.7-flash` | The narrator. |
| `GARY_GEMINI_FALLBACK_MODEL` | `gemini-3.6-flash` | Tried when the narrator is overloaded; empty disables it. |
| `GARY_GEMINI_SUMMARY_MODEL` | `gemini-3.5-flash-lite` | Folds old conversation into a summary. |
| `GARY_GEMINI_TIMEOUT` | `90` | Seconds before a model call gives up. |
| `GARY_HISTORY_WINDOW` | `30` | Turns kept word for word before summarising. |
| `GARY_CHECK_TTL` | `180` | Seconds a move waits for its roll. |
| `GARY_SWEEP_INTERVAL` | `15` | How often expired moves are swept. |
| `GARY_SEND_INTERVAL` | `1` | Seconds between Gary's messages in a channel. |
| `GARY_MAX_EFFECT_DEPTH` | `4` | Bounds the model calling itself in a loop. |

Settings are checked at start-up, and a bad one stops Gary with a clear error.

## Playtesting without Discord

`scripts/playtest.py` plays scripted scenes against the real rules and the real Gemini in a
throwaway campaign, clicking every button for its player, and writes a Markdown transcript
with each tool call and anything that went wrong.

```bash
set -a; . ~/.config/gary/env; set +a
PYTHONPATH=src .venv/bin/python scripts/playtest.py fight --out fight.md   # or discern, hire, camp, journey, town, all
```

Add `--llm-players` to have a second model play the characters, each with a goal for the
scene.

---

Dungeon World is by Sage LaTorra and Adam Koebel; its text, and the Player Compendium's,
are used under [CC BY 3.0](http://creativecommons.org/licenses/by/3.0/).
