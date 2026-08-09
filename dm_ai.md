AI Game Master: System Architecture & Technical Specification
An integration design using Google Gemini, the Model Context Protocol (MCP), and the Avrae Discord Bot.

This document outlines the architecture, data flow, implementation details, and deployment options for a hybrid AI-driven tabletop roleplaying game (TTRPG) system. In this design, Gemini handles the narrative and roleplay, Avrae enforces the rules and mathematics, and a custom MCP Server manages the persistent world state.

1. System Architecture Overview
The system consists of five main components interacting in a continuous feedback loop. Drelm serves as the central "Orchestrator," hosting the MCP Client, listening to Discord events, and managing Gemini API calls.

code
Code
┌────────────────────────┐
                      │      Discord Chat      │
                      └───────────┬────────────┘
                                  │
                          (Discord Gateway API)
                                  │
                                  ▼
                      ┌────────────────────────┐
                      │         DRELM          │
                      │  (The Central Hub /    │
                      │   Runs local process)  │
                      └────────┬───────────┬───┘
                               │           │
               (Google GenAI API)         (Stdio / Local Process)
                               │           │
                               ▼           ▼
                      ┌──────────────┐   ┌────────────────┐
                      │  Gemini API  │   │   MCP Server   │
                      │   (Cloud)    │   │ (Local Script) │
                      └──────────────┘   └────────────────┘
Components:
The Player(s): Input actions in a Discord channel and execute mechanical rolls via Avrae.

Drelm (Orchestrator): Listens to Discord, parses Avrae's rich text embeds, translates raw math into clean JSON/string data, and manages Gemini's API calls.

The Gemini API: Evaluates the narrative context, generates the story prose, roleplays NPCs, and decides when to request mechanical rolls.

The MCP Server: A background service connected to Drelm via stdio. It stores the campaign's source of truth (time, active effects, inventory, and rule guidelines).

The Avrae Bot: An independent official bot running in the same Discord server that resolves character sheets and d20 mathematics.

2. The Dual-Mode Gameplay Loop
The system dynamically switches between two modes to prevent mechanical clutter during narrative play, and to prevent narrative hallucinations during mechanical play.

Mode A: Narrative Mode (Freeform Roleplay)
Used for conversation, environmental description, and risk-free exploration.

The Flow: Player types action 
→
→
 Bot sends to Gemini 
→
→
 Gemini resolves immediately via narrative prose and NPC dialogue.

The State: No dice are rolled, but the AI can invoke MCP tools to update inventory (e.g., spending gold) or note narrative clues.

Mode B: Mechanical Mode (The "Director" Pattern)
Used when actions involve combat, spells, or risk of failure. This pattern works around Discord's restriction on bot-to-bot communication (Avrae ignoring other bots).

Initiation: The player declares a risky action (e.g., "I try to pick the lock").

Instruction: Gemini halts the narrative right before the climax, queries the MCP server for the correct syntax, and outputs a copy-paste command for the player (e.g., !check sleight of hand).

Execution: The player copies and runs the command. Avrae resolves the roll publicly in the channel.

Parsing & Resolution: Drelm intercepts Avrae's output embed, extracts the final result (e.g., Total 17), and feeds it to Gemini as a [SYSTEM] update.

Narration: Gemini compares the result against its internal DC and narrates the final outcome.

3. MCP Server Design (Resources & Tools)
Your MCP server provides the state management and syntax translation that Gemini needs to remain consistent.

Resources (Read-Only)
rpg://campaign/state: Returns a JSON representation of the current world date, time, active characters, and quest milestones.

rpg://rules/avrae_syntax: A cheat sheet dictionary mapping in-game actions to correct Avrae commands, preventing Gemini from generating broken command syntax.

rpg://rules/house_rules: Explains homebrew rules, specific rest variants, or setting modifications.

Tools (Executable Actions)
get_avrae_template(action_type, parameters) -> string: Returns the exact formatting string for an Avrae command.

advance_time(minutes) -> object: Updates the persistent clock in the campaign state, automatically checks for expired spell durations, and returns the new world time.

update_campaign_state(update_type, data) -> void: Updates character records, inventories, and NPC relationships in the JSON database.

4. Crucial State & Time Management
To maintain a cohesive game world, mechanical states and the passage of time must be tracked objectively.

Combat Status Tracking
During combat, Drelm constantly parses Avrae's public initiative tracker messages.

HP & Death Saves: If a player drops to 0 HP, the parser detects the [Dying] tag and passes [SYSTEM: Player is dying. 0/32 HP. 0 failed death saves] to Gemini.

Active Conditions: If Avrae applies a condition (e.g., [Prone], [Poisoned]), the parser extracts it so Gemini can reflect the penalty in its next narrative turn.

Out-of-Combat Timekeeping
Gemini cannot track abstract elapsed time natively. The MCP server manages this using an advance_time loop:

The Trigger: The players take a Short Rest (1 hour).

The Execution: Gemini calls advance_time(minutes=60).

The Resolve: The MCP server advances its internal clock, compares the new time against any registered active effects (like a 1-hour Protection from Poison spell), removes expired effects, and returns the new state to Gemini to narrate.

5. Technical Implementation Details

A. Language & SDK Choices
Drelm and the MCP Server can be written in either Python or TypeScript/Node.js.

Python Stack: Best for standard TTRPG math and integrations.

Libraries: discord.py, mcp (Official Python SDK), and google-genai (Official Google Gen AI SDK).

TypeScript Stack: Best for web-based event piping.

Libraries: discord.js, @modelcontextprotocol/sdk, and @google/genai.

B. Configuring the Gemini API (Persona & Tools)
You cannot use consumer-facing "Gemini Gems" for an API-driven Discord bot like Drelm. Instead, you must instantiate the Gemini client and pass your DM persona and MCP tools directly in the API configuration.

code
Python
from google import genai
from google.genai import types

client = genai.Client()

dm_system_instruction = """
You are an expert Tabletop RPG Game Master for D&D 2024. 
Enforce rules strictly. If an action requires a check, pause and give the player an Avrae command.
Do not use legacy 2014 rules. Keep narrative atmospheric and wait for [SYSTEM] roll updates.
"""

# Registering MCP tools dynamically to pass to Gemini
gemini_config = types.GenerateContentConfig(
    system_instruction=dm_system_instruction,
    # Tools fetched from MCP server list are mapped here:
    tools=[get_avrae_template, advance_time, update_campaign_state], 
    temperature=0.7
)
C. The Drelm-to-MCP Pipeline
To connect Drelm (MCP Client) to your local MCP Server without network lag, spawn the MCP Server as a background subprocess using stdio communication:

code
Python
import subprocess
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Define how to launch your local MCP Server script
server_params = StdioServerParameters(
    command="python",
    args=["path/to/mcp_server.py"]
)

async def init_mcp():
    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            # The session is now ready to query resources and call tools
            tools = await session.list_tools()
D. Intercepting and Parsing Avrae Messages
Drelm must listen to the channel and parse Avrae's rich embeds. Since Avrae uses embeds for neat layout, the parser must inspect message.embeds.

code
Python
AVRAE_BOT_ID = 265889285011603457 # Official Avrae Bot Discord ID

@bot.event
async def on_message(message):
    if message.author.id == AVRAE_BOT_ID:
        if message.embeds:
            embed = message.embeds[0]
            embed_text = f"{embed.title or ''} \n {embed.description or ''}"
            
            # Extract fields if they exist (common in initiative lists)
            for field in embed.fields:
                embed_text += f"\n {field.name}: {field.value}"
            
            # Simple keyword parsing
            is_dying = "[Dying]" in embed_text or "Death Saves:" in embed_text
            is_prone = "[Prone]" in embed_text
            
            # Format the output for Gemini
            system_payload = f"[SYSTEM: Avrae Update] Text: {embed_text} | Dying: {is_dying} | Prone: {is_prone}"
            
            # Call Gemini and post narrative outcome
            response = await call_gemini(system_payload)
            await message.channel.send(response)
E. D&D Beyond Integration Workarounds
Because Avrae has a read-only relationship with D&D Beyond's inventory system:

D&D Beyond Character JSON API (Out of Combat): Your MCP server can periodically query the public D&D Beyond character endpoint (https://character-service.dndbeyond.com/character/v2/character/{character_id}) to fetch real-time out-of-combat HP, spell slot counts, and attributes without bugging players for updates.

Inventory Sync: When players receive loot, the AI instructs them to open their browser and add the item on D&D Beyond manually. The AI then calls update_campaign_state(loot_gained=["potion"]) so that Gemini's narrative context matches their browser sheet.

6. Infrastructure & Deployment Architecture
Depending on your chosen Discord API style, you have two optimal deployment routes on Google Cloud Platform (GCP).

Option 1: Monolithic Google Compute Engine VM (Best for WebSocket Bots)
If Drelm is written using traditional WebSockets (like discord.py or standard discord.js), it requires a 24/7 running connection.

The Infrastructure: Run an e2-micro virtual machine on Google Compute Engine (eligible for GCP's Always Free tier) [1.2.4].

The Setup: Run your Drelm script on the VM. It will launch the MCP Server locally as a background subprocess via stdio.

Why: Serverless hosting (like Cloud Run) will shut down when inactive, breaking the WebSocket connection and taking your bot offline. A free-tier VM provides stable, continuous uptime.

Option 2: Serverless Google Cloud Run (Best for HTTP Webhook Bots)
If you configure Drelm to receive events via HTTP Webhooks (Discord's Interaction Endpoint instead of WebSockets), your code only runs when a player triggers a command.

The Infrastructure: Deploy Drelm inside a Docker container on Google Cloud Run.

The Setup: Pack Drelm and your MCP Server into the same Docker container (Monolithic Container). Or, deploy your MCP Server separately using SSE (Server-Sent Events) over HTTP, utilizing Google Cloud's official remote MCP deployment patterns [1.3.8].

Why: When no one is playing, Cloud Run scales to zero instances. You pay absolutely nothing for hosting during downtime, and it spins up in milliseconds when a player runs a command.

⚠️ A Warning on Self-Bots: Do not attempt to use a standard user account token (self-botting) to bypass Discord's bot-to-bot communication rules. Discord scans for automated user accounts and permanently bans them for violating their Terms of Service. Always use an official Discord Bot Application token.
7. Multi-System Portability & Configuration

Running D&D 5e (2024 Edition)
To ensure the system adheres to the 2024 rules instead of legacy 2014 rules:

System Prompt: Explicitly instruct Gemini to use the 2024 ruleset (referencing Weapon Masteries, updated Surprised mechanics, new Exhaustion rules, etc.).

MCP Grounding: Keep an up-to-date cheat sheet of major 2024 rules changes in your MCP resources.

Avrae Settings: Set the Discord server's Avrae ruleset default to 2024 using the !servsettings command, and ensure player sheets are configured via !csettings [1.1.2].
