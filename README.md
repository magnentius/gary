# Gary

<img src="gary.jpg" alt="Gary" width="600">

An AI Game Master for D&D 2024 over Discord. Gemini writes the narrative, Avrae rolls the
dice and enforces the maths, and a local state server owns the persistent world.

Design: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Status

The **deterministic core and all three adapters** are implemented, and the Avrae parser is
now confirmed against a live bot — most fixtures are real captures rather than
derivations. The MCP binding now imports and serves — see
[Before it can run](#before-it-can-run).

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
.venv/bin/python -m pytest -q          # 383 passed
```

`discord.py` and `google-genai` are needed only by `adapters/`; the core imports neither,
and a test asserts it stays that way.

The editable install needs Python 3.11+ and a pip new enough for PEP 660; without it the
package is never importable by name, and `python -m gary.…` fails with `No module named
'gary'`. `PYTHONPATH=src` works with no install at all — it is what `pyproject.toml`'s
`pythonpath = ["src"]` already does for pytest. Both `scripts/` entry points insert `src`
on `sys.path` themselves, so only the `-m` invocations need it.

The fixtures are live captures now. Five capture rounds against a real Avrae confirmed the
harder derivations — struck-through dice under advantage, To Hit rather than Damage on an
attack, the first roll winning under `-rr`, and the initiative tracker's whole format —
and corrected several things the derivation had wrong. Three fixtures remain derived:
`check_success` (its reply attribution has never been observed), `error_no_character`, and
`unreadable_embed`, which cannot be captured because it is by definition a format Avrae
does not produce.

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
2. **Capture more Avrae fixtures** (largely done). Patterns in `parser/avrae.py` started
   derived from Avrae's source at `avrae@cb75be2` and from `d20` run locally, and are now
   backed by live captures. Re-run this after any Avrae update, or to close the three
   fixtures still derived:
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
3. **Firestore is verified against the emulator** (2026-08-20) if you want the
   disposable-VM setup. The shared backend suite passes against real Firestore semantics,
   not just the fake client. To re-run it:
   ```bash
   gcloud emulators firestore start --host-port=localhost:8080   # needs a JDK
   ```
   Then point the suite's Firestore client at it instead of `FakeFirestoreClient`.
   **Know the ceiling before committing a long campaign to it:** the whole state lives in
   one document field, Firestore caps that at 1048487 bytes, and once you cross it every
   mutation raises and the campaign is write-blocked until the state is shrunk by hand.
   See `ARCHITECTURE.md` §7.4.
4. **Run it with no model attached.** `EchoNarrator` reports outcomes in plain text, so
   the entire Discord path — intents, routing, parsing, correlation, resolution, sending —
   can be proven against a live Avrae before spending a token:
   ```bash
   export DISCORD_TOKEN=... GARY_GAME_CHANNELS=<channel id>
   export GARY_PLAYERS='<discord user id>=<Avrae character name>'   # ';' between players
   PYTHONPATH=src .venv/bin/python -m gary.adapters.discord_bot
   ```
   Easier: skip the env var and have each player run `?dm playing <character name>` in
   the channel once. It persists to campaign state, survives restarts, and takes the
   Discord user id from the message author so it cannot be misattributed.
   `GARY_PLAYERS` is what stops the model calling `open_check` with a Discord display
   name. Avrae's embed says `Caspian Varr makes a Sleight Of Hand check!`, correlation is
   name-based in practice (`ARCHITECTURE.md` §14 (7)), and a check opened for "John K"
   matches nothing — it waits out its TTL and is then narrated as hesitation, with no sign
   anything went wrong. Leave it unset and validation is skipped, as before.
   In the channel: `?dm scene <location> | <situation>` and `?dm time 18:30` to author an
   opening, `?dm playing <name>`, `?dm checks`, `?dm state`, `?dm stats`, `?dm undo`, `?dm cancel`, `?dm reset` (two-step; wipes everything but the journal, so re-run `?dm playing` afterwards).
   Set a scene and a time before the first turn. On a fresh campaign layer 2 of the
   context (`ARCHITECTURE.md` §8) is empty, so the model invents the world from one
   sentence of player input — and an under-constrained model is exactly when §14 (8),
   narrating past the check, is most likely. A fresh campaign also opens at **Day 1,
   00:00**, so without `?dm time` the model narrates an evening while the clock the
   same prompt calls authoritative says midnight.
5. **Point Claude Desktop at the MCP server** (verified 2026-08-20). It imports, registers
   7 tools and 5 resources, executes them against a real campaign, and degrades to
   read-only under §7.6's lock with a JSON explanation rather than a stack trace.
   **It needs its own interpreter:** the SDK requires Python 3.10+ and this repo's `.venv`
   is 3.9, so point `command` at a 3.12 venv with `mcp>=2.0` installed, not at `.venv`.
   ```json
   {"mcpServers": {"gary": {
     "command": "/path/to/py312-venv/bin/python",
     "args": ["-m", "gary.adapters.mcp_server"],
     "env": {"GARY_CAMPAIGN_ROOT": "/path/to/campaign",
             "PYTHONPATH": "/path/to/gary/src"}}}}
   ```
   SDK 2.0 renamed `FastMCP` to `MCPServer` and moved it to `mcp.server.mcpserver`; the
   decorator shapes are unchanged. Pinning `mcp>=1.2` would install an SDK whose import
   path no longer exists, so `pyproject.toml` now requires `mcp>=2.0`.
6. **Add the model.** Set `GEMINI_API_KEY` and Gary switches from `EchoNarrator` to
   `GeminiNarrator` automatically. Defaults are `gemini-3.7-flash` for narration
   (verified 2026-08-18) and `gemini-3.5-flash-lite` for summarisation (verified
   2026-08-09), both pinned by a test.
   Model ids move — recheck rather than assume.
   Two knobs exist because `gemini-3.7-flash` spent 2026-08-21 answering 503
   "experiencing high demand": `GARY_GEMINI_TIMEOUT` (default 90s) bounds every
   call, and `GARY_GEMINI_FALLBACK_MODEL` (default `gemini-3.6-flash`) is tried
   once when the primary exhausts its retries on a transient failure. Set the
   fallback to an empty string to disable it.

## Verified facts

- Avrae application ID is `261302296103747584`.
- D&D Beyond's character endpoint is `/character/v5/` — undocumented, unsupported,
  public-characters-only. Treated as optional enrichment that may vanish.
- Discord's HTTP Interactions Endpoint cannot receive arbitrary channel messages. Since
  ingest depends on observing Avrae's embeds, Gary needs a persistent Gateway connection
  on an always-on process. See `ARCHITECTURE.md` §11.
