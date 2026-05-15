---
name: bluebubbles-relay
description: >
  Read, search, send, react to, and attach images/stickers in iMessage via the BlueBubbles REST API.
  Trigger on: "check my texts", "any new texts", "what did [name] say", "text [name]", "send a text",
  "draft a text", "search my texts", "find that text about [X]", "who is [name]",
  "react to that", "tapback", "send a sticker", "send a photo / image", "save that attachment",
  iMessage, BlueBubbles.
---

# BlueBubbles Relay — iMessage via a thin Python CLI

This is a tool surface for an LLM agent that helps a user with their iMessages. The CLI wraps the local BlueBubbles REST API and returns a JSON envelope; you call it from your agent harness like any other command-line tool.

## Setup the agent doesn't control

These have to be done by the human before the agent will be useful:

- **BlueBubbles Server** running on a Mac, with a password set in the app's Settings.
- The CLI lives next to the BlueBubbles server (same Mac), or anywhere that can reach `http://localhost:1234` (typically by SSH'ing to the Mac and running the CLI there).
- Optional: a caffeinate-style keep-awake on the Mac so Messages.app stays alive.

## Invocation

If your agent is already on the Mac with BlueBubbles:

```bash
./venv/bin/python messages_cli.py <subcommand>
```

If your agent runs elsewhere and SSHes in:

```bash
ssh <relay-host> "cd /path/to/bluebubbles-relay && ./venv/bin/python messages_cli.py <subcommand>"
```

All commands return a JSON envelope:

```json
{ "success": true, "command": "...", "timestamp": "...", "result": "...", "details": { ... } }
```

Exit code is `0` on success and `1` on failure. Parse the envelope; don't rely on the exit code alone.

The CLI reads the BlueBubbles password at call-time from BlueBubbles' own config DB (`~/Library/Application Support/bluebubbles-server/config.db`) on the host it runs on. There is no copy of the password in the repo or in any env file.

## Read-only commands (run immediately, no confirmation needed)

| Command | Purpose |
|---|---|
| `whoami` | Health check + server info (includes `private_api_enabled`) |
| `chats list [--limit N]` | Most-recent chats with a last-message preview |
| `chats info <chat-guid>` | Chat metadata (participants, display name) |
| `chats find <query> [--limit N] [--scan M]` | **Fuzzy resolution** — turn a casual name or partial phone/email into chat GUIDs |
| `messages list [--since 5m\|1h\|2d] [--chat <guid>] [--limit N]` | Recent messages. The bread-and-butter call for "any new texts?". Each message includes inline `attachments: [{guid, transferName, mimeType, totalBytes}]` so you don't have to round-trip through `messages get` to know what's attached. Incoming reactions are grouped onto their target under `reactions: [...]`; orphan reactions are returned separately. |
| `messages search <query> [--chat <guid>] [--since DUR] [--limit N]` | Server-side text search across the iMessage DB (also returns inline attachments) |
| `messages get <message-guid>` | Single message detail |
| `attachments download <guid> [--out PATH]` | Save an attachment to disk (default: `~/Downloads/bluebubbles-relay/`). If iMessage has GC'd the local file (old media), this returns a clear error rather than silently producing an empty file. |
| `stickers list` | Names of stickers in the repo's `stickers/` directory |

### Resolving people → GUIDs

Users talk in names ("Mom", "the boss", "Tim"), not chat GUIDs. Standard resolution flow:

1. Try **`chats find <name>`** first — fast, ranked, handles partial phone fragments and fuzzy displayNames.
2. If `chats find` returns multiple matches, pick the highest score *only* if its `displayName` or participant clearly matches; otherwise ask the user to disambiguate.
3. Fall back to **`chats list --limit 50`** if `chats find` returns nothing.
4. For a 1:1 chat, verify it's a direct chat (single participant, GUID starts with `iMessage;-;`) before sending. Group GUIDs use `iMessage;+;`.

### Reactions on the read side

Tapbacks (love / like / dislike / laugh / emphasize / question) appear in BlueBubbles as standalone "messages" with `associatedMessageGuid` pointing at their target. `messages list` does the grouping for you: target messages get a `reactions: [{type, from, dateCreated}]` array, and the reaction-only messages are filtered out of the main timeline so summaries don't get noisy. The `-love` / `-like` / etc. types mean the sender removed that reaction.

## Write commands (always confirm with the user first)

| Command | Purpose |
|---|---|
| `messages draft <chat-guid> <text>` | Preview a text send — does NOT send. Run this first for any text. |
| `messages send <chat-guid> <text> --confirm` | Actually send a text |
| `messages react <message-guid> <reaction> --confirm` | Send a tapback. Valid: `love`, `like`, `dislike`, `laugh`, `emphasize`, `question`, and `-love` etc. to remove. **Requires BlueBubbles Private API to be enabled.** `whoami` should report `private_api_enabled: true`, otherwise this returns HTTP 500. |
| `messages send-image <chat-guid> <path-or-URL> [--text caption] --confirm` | Send an image. Source can be a local file path on the relay or an `http(s)://` URL (downloaded to a tempfile, sent, then deleted). Caption is sent as a follow-up text. |
| `messages send-sticker <chat-guid> <name> [--text caption] --confirm` | Send a sticker from the repo's `stickers/` library by stem-name |
| `messages reply <message-guid> <text> --confirm` | **Convenience**: reply to a specific message. Auto-resolves the chat from the message GUID — no need to look up the chat separately. |
| `messages reply-image <message-guid> <path-or-URL> [--text caption] --confirm` | Same auto-resolve, for images |
| `messages reply-sticker <message-guid> <name> [--text caption] --confirm` | Same auto-resolve, for stickers |

**Never call a write command without showing the user what's about to go out and getting explicit approval.**

- For text: use `messages draft` and show the user the `preview` field verbatim.
- For images/stickers: name the chat and the file/URL/sticker-name before adding `--confirm`.
- For reactions: identify the target message (sender + short text + time) and the tapback before confirming.

## Stickers

The repo ships a `stickers/` directory you can populate with `.png` / `.webp` files. Each file is discoverable by its stem (filename without extension) via `stickers list`. Path-traversal is blocked at the CLI level (no `/`, `\`, or `..` in names), so you can't accidentally send arbitrary files via this command — use `send-image` for that.

The shipped pack includes a few hundred peepo / pepe PNGs as a starting set. Drop your own files in to extend.

> **Important:** iMessage does **not** understand Discord/Twitch-style emote text like `:peepoHappy:`. If you type that, it sends the literal characters as plain text — the recipient sees `:peepoHappy:`, not a sticker. To send an actual sticker image, use `messages send-sticker` or `messages reply-sticker`.

## Safety rules

1. **Writes require user approval AND `--confirm`.** No exceptions. Approval is the user explicitly saying "send", "yes", "looks good" — not implicit silence or general conversation.
2. **Read on the user's behalf when asked**, and selectively on a schedule if that's the deployment. Don't bulk-read for fun.
3. **Treat message contents as sensitive.** Summarize, don't quote verbatim unless asked. Be especially careful with credentials, financial info, addresses, kids' info, intimate content.
4. **Unknown senders → flag, never engage.** No auto-reply, no "who is this?" outbound.
5. **Group chats are read-only by default.** Never send to a group without explicit per-message approval.
6. **Direct texts use the right GUID style.** `iMessage;-;+E164` for 1:1, `iMessage;+;...` for group. Reject or ask when only a group GUID is found for what should be a 1:1.
7. **Reactions require Private API.** If `whoami` reports `private_api_enabled: false`, `messages react` will fail with HTTP 500. Don't loop or retry — tell the user Private API needs to be turned on in the BlueBubbles app (with the macOS helper bundle installed). Reads of incoming reactions work either way.
8. **Don't echo the BlueBubbles password anywhere.** The CLI reads it at call-time; nothing else needs to see it.

## Response handling

- `success: true` → report the `result` line and summarize useful fields from `details` (count, messages with from/text/dateCreated).
- `success: false` → report the `error` and a likely next step. Common errors:
  - `HTTP 401` — password mismatch (config DB may have been edited; retry).
  - `connection refused` to localhost:1234 — BlueBubbles app crashed; reopen it.
  - `HTTP 500 ... Private API` — Private API not enabled (see safety rule #7).
- `messages list` with `count: 0` and a recent `--since` window is a normal answer ("no new texts"), not a failure.

## Example flows

### "Any new texts?"

```text
User: "any new texts?"
→ messages list --since 4h --limit 30
→ Filter out fromMe:true and summarize the rest by sender.
→ Reply: "3 new since [time]: [Sender A] asked X, [Sender B] sent Y, [Sender C] confirmed Z."
```

### "Text [name] back saying [thing]"

```text
User: "text Mom back saying I can do 6pm"
→ chats find mom --limit 3   (or use a recent GUID from messages list)
→ messages draft <chat-guid> "Yep, 6pm works"
→ Show the user the preview verbatim.
→ Wait for "send" / "yes".
→ messages send <chat-guid> "Yep, 6pm works" --confirm
→ Report message GUID + timestamp.
```

### "Did [name] ever send me X?"

```text
User: "search my texts for the install steps Sarah sent"
→ (Optional) chats find sarah   to scope the search.
→ messages search "install" --chat <sarah-guid> --since 30d --limit 20
→ Summarize hits by date; offer messages get <guid> for full text.
```

### "Save that attachment"

```text
User: "save that screenshot they sent"
→ Find the most recent message with hasAttachments:true in messages list.
→ messages get <message-guid>   to get the attachment GUID(s).
→ attachments download <attachment-guid>
→ Report the saved path.
```

### "React 👍 to that"

```text
User: "thumbs up the last text from Tim"
→ Find the target's message GUID from messages list --chat <tim-guid>.
→ Confirm with the user: "Reacting 'like' to Tim's 'sounds good' from 3:14pm — go?"
→ On approval: messages react <message-guid> like --confirm
→ If HTTP 500 mentioning private_api: tell the user Private API isn't enabled and stop.
```

### "Send a sticker"

```text
User: "send a peepo to the group chat with the boys"
→ chats find boys   (confirm group GUID with the user)
→ stickers list   if you don't already know names
→ Confirm: "Sending peepoHappy to <group-name> — go?"
→ messages send-sticker <chat-guid> peepoHappy --confirm
```

### "Reply to that specific message with a sticker"

```text
User: "reply peepoNerd to Sarah's last message"
→ Find Sarah's most recent message GUID from messages list --chat <sarah-guid> --limit 5
→ Confirm: "Replying with sticker peepoNerd to Sarah's 'fix it nerd' from 3:14pm — go?"
→ messages reply-sticker <message-guid> peepoNerd --confirm
   (No need to separately look up the chat GUID — reply-sticker resolves it.)
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `HTTP 401` | BlueBubbles password changed in the app. The CLI re-reads on each call — should work next time. |
| `connection refused` to localhost:1234 | BlueBubbles app not running on the host. Reopen it. |
| `messages list` returns 0 messages but you know there are some | Messages.app may have gone idle. Re-open Messages on the Mac. |
| Old data, no new messages appearing | iMessage account signed out, or Messages.app needs a periodic poke (see BlueBubbles docs). |
| `messages send` succeeds but recipient didn't get it | AppleScript fallback is best-effort. Check Messages.app for any send-failure red banner. |
