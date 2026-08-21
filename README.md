# Gary

<img src="gary.jpg" alt="Gary" width="600">

An AI Game Master for D&D 2024 over Discord. Gemini writes the narrative, Avrae rolls the
dice and enforces the maths, and a local state server owns the persistent world.

Design: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Status

The **deterministic core and all three adapters** are implemented. The Avrae parser is
built against fixtures derived from Avrae's own source, not yet confirmed against a live
bot, and the MCP binding has never been run — see [Before it can run](#before-it-can-run).

```
src/gary/
  models.py        frozen domain types
  templates.py     Avrae command rendering from an allowlist   §5.3
  registry.py      pending-check registry + correlation        §5
  resolver.py      DC comparison — deliberately boring         §5.2
  fsm.py           the Director: Mode A/B state machine        §4
  context.py       prompt assembly + injection defence         §8, §9.1
  parser/avrae.py  Avrae output parsing, fail-loud             §6
  state/store.py   journal + snapshot + undo + bounds          §7.3
  state/backend.py storage protocols: in-memory, file          §7.4
  state/firestore.py  managed backend, disposable VM           §7.4, §11.1
  state/history.py conversation history + rolling summary       §8
  state/lock.py    single-writer enforcement                     §7.6
  prep.py          between-session campaign prep API             §2.1
  ports.py         Narrator / transport seams + EchoNarrator
  config.py        boot-time settings, fail-fast validation     §1
  adapters/
    routing.py     pure ingest decision                         §6.3
    sender.py      2000-char splitting + per-channel pacing     §10
    runner.py      performs Effects; bounds model recursion     §4
    discord_bot.py discord.py glue, ?dm commands, sweep loop    §1, §6.3
    gemini.py      Narrator: context, tool loop, retries        §5, §8, §12.4
    gemini_tools.py  function declarations                      §12.6
    mcp_server.py  MCP binding over prep.py (unverified)         §2.1
```

The core has **no third-party dependencies**, so it runs without Discord, an API key, or
a live Avrae:

```bash
python -m venv .venv && .venv/bin/pip install -e '.[discord,llm,dev]'
.venv/bin/python -m pytest -q          # 369 passed, 1 xfailed
```

`discord.py` and `google-genai` are needed only by `adapters/`; the core imports neither,
and a test asserts it stays that way.

The editable install needs Python 3.11+ and a pip new enough for PEP 660; without it the
package is never importable by name, and `python -m gary.…` fails with `No module named
'gary'`. `PYTHONPATH=src` works with no install at all — it is what `pyproject.toml`'s
`pythonpath = ["src"]` already does for pytest. Both `scripts/` entry points insert `src`
on `sys.path` themselves, so only the `-m` invocations need it.

The one `xfail` is intentional: it fails until the Avrae fixtures are confirmed against a
live bot, so the last gap cannot be quietly forgotten.

## The three properties worth knowing

**Outcomes are not decided by the model.** `open_check` persists a DC and both
consequences before the player is told to roll; `resolver.resolve` is arithmetic. An LLM
asked after the fact whether 17 beat a DC it set earlier will rationalise, and context
truncation loses the DC entirely. If `resolver.py` ever grows an LLM call, the design has
regressed.

**Only `AWAITING_ROLL` consumes Avrae output.** In every other state an Avrae embed is
logged and dropped. That single rule is what stops `!help`, sheet lookups, and other
players' rolls from each costing a Gemini call.

**Trust rides on the transport, not on strings.** Roll results reach the model as
structured tool results. Player text is sanitised and attributed. Nothing is trusted
because it starts with `[SYSTEM:`.

**A check is a deferred function call.** When the model calls `open_check`, that call is
left unanswered; the roll outcome arrives minutes later as its `function_response`. The
result therefore reaches the model over the tool transport rather than as prose, so its
trust comes from the channel it travelled on — and the model cannot confuse a result with
a player's claim about one. See `ARCHITECTURE.md` §12.4.

**Gary is not an MCP client.** It uses the store in-process; a protocol boundary between
two objects in one program buys nothing. MCP sits *beside* Gary so you can open the
campaign from Claude Desktop between sessions — audit what the model did, write NPCs,
plant foreshadowing. Two processes on one campaign would break the single-writer rule, so
the writer holds a lock and a prep client that cannot take it runs read-only.

**Nothing important lives on the box.** Campaign state and pending checks sit behind
storage protocols with in-memory, local-file, and Firestore implementations; the Director
derives channel state from the registry rather than holding it. Point the backends at
Firestore and the VM can be deleted and recreated mid-campaign. One test suite runs
against all three backends, which is how a Firestore journal-ordering defect was caught
before it ever ran.

## Before it can run

1. **Enable the Message Content privileged intent** in the Discord Developer Portal.
   Without it, `message.embeds` arrives empty and the parser sees nothing — it fails
   silently, which is why this is first.
2. **Capture live Avrae fixtures.** Patterns in `parser/avrae.py` are derived from
   Avrae's source at `avrae@cb75be2` and from running `d20` locally — not guesses, but
   not yet checked against what Discord delivers over the wire:
   ```bash
   pip install -e '.[discord]'
   export DISCORD_TOKEN=...
   python scripts/capture_avrae_fixtures.py --channel <id> --minutes 10
   ```
   The script prints the exact commands to run. Then:
   ```bash
   python scripts/verify_avrae_fixtures.py    # exits non-zero on any gap
   ```
   It flags captures the parser cannot read, containers that disagree with the derived
   fixtures (the class that hid the initiative tracker), and commands you have not run
   yet. Captures land in `tests/fixtures/avrae/captured/`, which the test glob ignores —
   move them up a level once you have filled in each `expect`.
3. **Verify the Firestore backend** if you want the disposable-VM setup. `state/firestore.py`
   has never run against Firestore, real or emulated — the fake-client tests cover this
   repo's serialisation and ordering, not Google's semantics:
   ```bash
   gcloud emulators firestore start --host-port=localhost:8080
   ```
4. **Run it with no model attached.** `EchoNarrator` reports outcomes in plain text, so
   the entire Discord path — intents, routing, parsing, correlation, resolution, sending —
   can be proven against a live Avrae before spending a token:
   ```bash
   export DISCORD_TOKEN=... GARY_GAME_CHANNELS=<channel id>
   PYTHONPATH=src .venv/bin/python -m gary.adapters.discord_bot
   ```
   In the channel: `?dm checks`, `?dm state`, `?dm stats`, `?dm undo`, `?dm cancel`.
5. **Verify the MCP binding** if you want Claude Desktop access. `adapters/mcp_server.py`
   has never been imported: the MCP SDK needs Python 3.10+ and this repo's environment is
   3.9. Everything with behaviour worth trusting is in `prep.py` and `state/lock.py`,
   which are fully tested; what is unverified is decorator shapes and transport wiring.
   ```json
   {"mcpServers": {"gary": {
     "command": "/path/to/.venv/bin/python",
     "args": ["-m", "gary.adapters.mcp_server"],
     "env": {"GARY_CAMPAIGN_ROOT": "/path/to/campaign"}}}}
   ```
6. **Add the model.** Set `GEMINI_API_KEY` and Gary switches from `EchoNarrator` to
   `GeminiNarrator` automatically. Defaults are `gemini-3.7-flash` for narration
   (verified 2026-08-18) and `gemini-3.5-flash-lite` for summarisation (verified
   2026-08-09), both pinned by a test.
   Model ids move — recheck rather than assume.

## Verified facts

- Avrae application ID is `261302296103747584`.
- D&D Beyond's character endpoint is `/character/v5/` — undocumented, unsupported,
  public-characters-only. Treated as optional enrichment that may vanish.
- Discord's HTTP Interactions Endpoint cannot receive arbitrary channel messages. Since
  ingest depends on observing Avrae's embeds, Gary needs a persistent Gateway connection
  on an always-on process. See `ARCHITECTURE.md` §11.
