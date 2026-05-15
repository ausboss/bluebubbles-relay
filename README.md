# bluebubbles-relay

A thin Python CLI over the [BlueBubbles](https://bluebubbles.app) REST API that lets an LLM agent (or anything else) **read, search, send, react to, and attach images/stickers** in iMessage. Designed to be dropped into an agent harness (Claude Code, Claude Agent SDK, Aider, etc.) as a tool surface — every command returns a JSON envelope, sends are gated behind `--confirm`, and the BlueBubbles password is read live from the local config DB so nothing sensitive sits in the repo.

If you're an LLM reading this through a harness: see [`SKILL.md`](./SKILL.md) for the full operator's manual (command tables, resolution flow, safety rules, example flows).

## What you need

- A Mac running [BlueBubbles Server](https://bluebubbles.app/install/) with a password set in its Settings.
- Python 3.10+ on the same Mac (or anywhere that can reach `http://localhost:1234`).
- Optional but recommended: a keep-awake (`caffeinate -dimsu` via a LaunchAgent) so Messages.app stays alive.

## Install

```bash
git clone https://github.com/ausboss/bluebubbles-relay.git
cd bluebubbles-relay
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

That's the whole install. No config file, no env var. The CLI reads the BlueBubbles API password at call-time from BlueBubbles' own SQLite config at `~/Library/Application Support/bluebubbles-server/config.db`.

## CLI quickstart

```bash
# Sanity check the server
./venv/bin/python messages_cli.py whoami

# What's new in the last 4 hours
./venv/bin/python messages_cli.py messages list --since 4h --limit 30

# Fuzzy-find a chat by name
./venv/bin/python messages_cli.py chats find mom

# Search across messages
./venv/bin/python messages_cli.py messages search "install steps" --since 30d

# Preview a send (does NOT send)
./venv/bin/python messages_cli.py messages draft "iMessage;-;+15551234567" "on my way"

# Send it
./venv/bin/python messages_cli.py messages send "iMessage;-;+15551234567" "on my way" --confirm

# Send a sticker from the stickers/ folder
./venv/bin/python messages_cli.py messages send-sticker "iMessage;-;+15551234567" peepoHappy --confirm

# Send an image from a URL
./venv/bin/python messages_cli.py messages send-image "iMessage;-;+15551234567" "https://example.com/img.png" --text "look at this" --confirm
```

Every command prints one JSON object and exits `0` (success) or `1` (failure):

```json
{
  "success": true,
  "command": "messages list",
  "timestamp": "2026-05-15T15:37:41-05:00",
  "result": "10 message(s)",
  "details": { "count": 10, "messages": [ ... ] }
}
```

## Using it with an LLM / agent harness

The CLI is built to be invoked by an LLM as a tool. There are two common patterns:

### 1. Claude Code (or any harness that loads `SKILL.md`)

If your harness supports skills with a `SKILL.md`, point it at this repo:

```bash
# Example for Claude Code: drop the skill into the agent's skills dir
ln -s ~/path/to/bluebubbles-relay ~/.claude/skills/bluebubbles-relay
```

The agent will then auto-load [`SKILL.md`](./SKILL.md) when the user says things like "any new texts?", "text Mom back saying...", "react to that". The skill explains the full command surface, the draft-then-confirm safety pattern, GUID resolution, etc.

### 2. Any agent SDK that can shell out

Treat the CLI as a subprocess that returns JSON:

```python
import json, subprocess

def relay(*args):
    """Invoke the bluebubbles-relay CLI and parse its JSON envelope."""
    r = subprocess.run(
        ["./venv/bin/python", "messages_cli.py", *args],
        capture_output=True, text=True, check=False,
    )
    return json.loads(r.stdout)

# Recent messages
new = relay("messages", "list", "--since", "1h")
for m in new["details"]["messages"]:
    if not m["fromMe"]:
        print(f"{m['from']}: {m['text']}")

# Resolve a name then send
matches = relay("chats", "find", "mom")["details"]["matches"]
if matches:
    chat_guid = matches[0]["guid"]
    relay("messages", "send", chat_guid, "running late, be there in 10", "--confirm")
```

If your agent runs on a different machine than the BlueBubbles host, wrap the call in `ssh <mac-host> "cd /path/to/bluebubbles-relay && ..."`.

### Example natural-language interaction

> **User:** any new texts from Mom?
>
> **Agent** (silently):
> 1. `chats find mom` → resolves to a chat GUID.
> 2. `messages list --chat <guid> --since 4h` → returns 2 incoming messages.
>
> **Agent:** "Mom sent two — at 1:14pm: 'are we still on for dinner Sunday?' and at 2:02pm: 'forgot to ask if you can pick up the cake'. Want me to draft a reply?"

> **User:** yeah, tell her yes to both
>
> **Agent** (silently): `messages draft <guid> "Yep — Sunday's still on and I'll grab the cake."`
>
> **Agent:** "Draft: 'Yep — Sunday's still on and I'll grab the cake.' → Mom. Send?"
>
> **User:** send
>
> **Agent:** `messages send <guid> "Yep — Sunday's still on and I'll grab the cake." --confirm` → "Sent at 2:14pm."

## Command surface (short version)

| Group | Commands |
|---|---|
| Health | `whoami` |
| Chats | `chats list`, `chats info`, `chats find` |
| Messages (read) | `messages list`, `messages search`, `messages get` |
| Messages (write) | `messages draft`, `messages send`, `messages react`, `messages send-image`, `messages send-sticker` |
| Attachments | `attachments download` |
| Stickers | `stickers list` |

`--help` works at every level (`messages send-image --help`, etc.). Full behavior, response shapes, and safety patterns are in [`SKILL.md`](./SKILL.md).

## Stickers

The `stickers/` directory ships with a starter pack of static PNG peepo/pepe emotes — drop your own `.png` / `.webp` files in to extend. `messages send-sticker <chat> <name>` resolves `<name>` against the directory by file stem (e.g. `peepoHappy` → `stickers/peepoHappy.png`). Animated `.gif` stickers are not included; iMessage's AppleScript send path handles them inconsistently.

## Caveats

- **Reactions require BlueBubbles Private API enabled** (`enable_private_api` in the BlueBubbles app + the macOS Messages helper bundle). Without it, `messages react` returns HTTP 500. Reads of incoming reactions work either way.
- **The CLI talks to `localhost:1234`** by default. If you've configured BlueBubbles on a different port, edit `BASE_URL` in `messages_cli.py`.
- **AppleScript send path is best-effort.** Most sends work; rarely one will silently fail server-side. Check Messages.app for any red-banner failures.
