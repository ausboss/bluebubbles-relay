#!/usr/bin/env python3
"""bluebubbles-relay — read / send iMessage via the local BlueBubbles REST API.

Architectural pattern matches metro-scripts/as400-tools/asi_manage.py and
bha-helpdesk-cli/helpdesk_cli.py: invoke via SSH on the Mac mini relay,
JSON-envelope output, exit 0/1.

Password is read at call time from BlueBubbles' own config DB at
~/Library/Application Support/bluebubbles-server/config.db so there is no
copy of the secret in this repo's filesystem.
"""

import argparse
import difflib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

CONFIG_DB = Path.home() / "Library/Application Support/bluebubbles-server/config.db"
BASE_URL = "http://localhost:1234"
TIMEOUT_S = 15
DOWNLOAD_TIMEOUT_S = 120

REPO_DIR = Path(__file__).resolve().parent
STICKERS_DIR = REPO_DIR / "stickers"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "bluebubbles-relay"

# BlueBubbles reaction type ids → human names.
# 2000-range are reactions; 3000-range are their "removed" counterparts.
REACTION_NAMES = ["love", "like", "dislike", "laugh", "emphasize", "question"]
REACTION_TYPE_TO_NAME = {2000 + i: n for i, n in enumerate(REACTION_NAMES)}
REACTION_TYPE_TO_NAME.update({3000 + i: f"-{n}" for i, n in enumerate(REACTION_NAMES)})
VALID_REACTIONS = set(REACTION_NAMES) | {f"-{n}" for n in REACTION_NAMES}


def _envelope(success: bool, command: str, *, result=None, error=None, details=None):
    out = {
        "success": success,
        "command": command,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if success:
        out["result"] = result
    else:
        out["error"] = error
    if details is not None:
        out["details"] = details
    return out


def _read_password() -> str:
    if not CONFIG_DB.exists():
        raise RuntimeError(f"BlueBubbles config DB not found at {CONFIG_DB}")
    conn = sqlite3.connect(str(CONFIG_DB))
    try:
        row = conn.execute("SELECT value FROM config WHERE name='password'").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise RuntimeError("BlueBubbles password not set in config DB")
    return row[0]


def _api(method: str, path: str, *, params=None, json_body=None):
    p = dict(params or {})
    p["password"] = _read_password()
    r = requests.request(
        method,
        f"{BASE_URL}{path}",
        params=p,
        json=json_body,
        timeout=TIMEOUT_S,
    )
    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
        raise RuntimeError(f"HTTP {r.status_code}: {body.get('message') or body.get('error') or body}")
    return r.json()


def _parse_duration(s: str | None) -> timedelta | None:
    if not s:
        return None
    unit = s[-1].lower()
    try:
        n = int(s[:-1])
    except ValueError as e:
        raise ValueError(f"invalid duration {s!r}: expected like '5m', '1h', '2d'") from e
    mults = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in mults:
        raise ValueError(f"invalid duration unit {unit!r}; expected s/m/h/d")
    return timedelta(seconds=n * mults[unit])


def _short_text(s: str | None, n: int = 200) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[:n] + "…"


def _api_download(path: str, out_path: Path) -> int:
    """Stream a binary attachment to disk. Returns bytes written."""
    params = {"password": _read_password()}
    with requests.get(f"{BASE_URL}{path}", params=params, stream=True, timeout=DOWNLOAD_TIMEOUT_S) as r:
        if not r.ok:
            # iMessage GCs old media off disk while keeping the message + attachment
            # metadata, so a 404 on a known-good attachment GUID usually means the
            # file has been purged from this Mac's cache and is unrecoverable.
            if r.status_code == 404:
                raise RuntimeError(
                    "attachment metadata exists but the file has been purged from this Mac's "
                    "iMessage cache (HTTP 404). Old media is GCd locally; nothing to download."
                )
            try:
                body = r.json()
                detail = body.get("message") or body.get("error") or body
            except Exception:
                detail = r.text[:200]
            raise RuntimeError(f"HTTP {r.status_code} downloading attachment: {detail}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        if written == 0:
            # Some BlueBubbles versions return 200 + empty body when the file is missing.
            try:
                out_path.unlink()
            except OSError:
                pass
            raise RuntimeError(
                "BlueBubbles returned an empty response for this attachment — the file is "
                "likely metadata-only on this Mac (iMessage cache GC). Nothing to download."
            )
        return written


def _summarize_message(m: dict) -> dict:
    chat = (m.get("chats") or [{}])[0]
    handle = m.get("handle") or {}
    raw_attachments = m.get("attachments") or []
    attachments = [
        {
            "guid": a.get("guid"),
            "transferName": a.get("transferName"),
            "mimeType": a.get("mimeType"),
            "totalBytes": a.get("totalBytes"),
        }
        for a in raw_attachments
    ]
    # BlueBubbles' top-level hasAttachments flag is unreliable in list views,
    # so derive it from the actual attachment metadata when we have it.
    has_attachments = bool(attachments) if attachments else bool(m.get("hasAttachments"))
    return {
        "guid": m.get("guid"),
        "chatGuid": chat.get("guid"),
        "chatName": chat.get("displayName"),
        "fromMe": m.get("isFromMe"),
        "from": "me" if m.get("isFromMe") else handle.get("address"),
        "text": _short_text(m.get("text"), 500),
        "dateCreated": m.get("dateCreated"),
        "dateRead": m.get("dateRead"),
        "hasAttachments": has_attachments,
        "attachments": attachments,
    }


def _is_reaction(m: dict) -> bool:
    return bool(m.get("associatedMessageGuid")) and m.get("associatedMessageType") in REACTION_TYPE_TO_NAME


def _split_reactions(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (regular_messages_with_reactions_attached, orphan_reactions).

    Reactions whose target message is in this batch get merged into the target's
    `reactions` list; the reaction message itself is dropped from output. Orphan
    reactions (target not in batch) come back as their own list for the bot to
    decide what to do with them.
    """
    by_guid: dict[str, dict] = {}
    regular: list[dict] = []
    raw_reactions: list[dict] = []
    for m in messages:
        if _is_reaction(m):
            raw_reactions.append(m)
        else:
            summary = _summarize_message(m)
            summary["reactions"] = []
            by_guid[m.get("guid")] = summary
            regular.append(summary)

    orphans: list[dict] = []
    for r in raw_reactions:
        target_guid = r.get("associatedMessageGuid") or ""
        # BlueBubbles encodes targets as "p:0/<guid>" or "bp:<guid>" — strip the prefix.
        clean = re.sub(r"^[a-z]+:\d*/?", "", target_guid)
        handle = r.get("handle") or {}
        entry = {
            "type": REACTION_TYPE_TO_NAME.get(r.get("associatedMessageType"), "?"),
            "from": "me" if r.get("isFromMe") else handle.get("address"),
            "dateCreated": r.get("dateCreated"),
        }
        target = by_guid.get(clean)
        if target is not None:
            target["reactions"].append(entry)
        else:
            orphans.append({**entry, "targetGuid": clean})
    return regular, orphans


def _resolve_image_source(src: str) -> tuple[Path, bool]:
    """Return (local_path, is_temp). Accepts a local path or http(s) URL."""
    parsed = urlparse(src)
    if parsed.scheme in ("http", "https"):
        r = requests.get(src, stream=True, timeout=DOWNLOAD_TIMEOUT_S)
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status_code} fetching {src}")
        suffix = Path(parsed.path).suffix
        if not suffix:
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            suffix = mimetypes.guess_extension(ct) or ".bin"
        fd, tmp = tempfile.mkstemp(prefix="bbrelay-", suffix=suffix)
        os.close(fd)
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return Path(tmp), True
    p = Path(src).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"image not found: {p}")
    if not p.is_file():
        raise IsADirectoryError(f"not a file: {p}")
    return p, False


def _resolve_sticker(name: str) -> Path:
    """Look up a sticker by stem-name in STICKERS_DIR. Path-traversal safe."""
    if not STICKERS_DIR.exists():
        raise FileNotFoundError(f"stickers dir does not exist: {STICKERS_DIR}")
    # Reject anything that smells like traversal before touching the FS.
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid sticker name: {name!r}")
    candidates = sorted(STICKERS_DIR.glob(f"{name}.*"))
    # Filter to actual files inside STICKERS_DIR (resolved path check).
    safe = [c for c in candidates if c.is_file() and STICKERS_DIR.resolve() in c.resolve().parents]
    if not safe:
        # try exact (with extension already in name)
        exact = STICKERS_DIR / name
        if exact.is_file() and STICKERS_DIR.resolve() in exact.resolve().parents:
            return exact
        raise FileNotFoundError(f"no sticker named {name!r} in {STICKERS_DIR}")
    return safe[0]


def _upload_attachment(chat_guid: str, file_path: Path) -> dict:
    """POST to /api/v1/message/attachment as multipart. Returns API json."""
    pw = _read_password()
    temp_guid = f"relay-{int(time.time() * 1000)}"
    with file_path.open("rb") as f:
        files = {"attachment": (file_path.name, f, mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")}
        data = {
            "chatGuid": chat_guid,
            "tempGuid": temp_guid,
            "name": file_path.name,
            "method": "apple-script",
        }
        r = requests.post(
            f"{BASE_URL}/api/v1/message/attachment",
            params={"password": pw},
            data=data,
            files=files,
            timeout=DOWNLOAD_TIMEOUT_S,
        )
    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
        raise RuntimeError(f"HTTP {r.status_code}: {body.get('message') or body.get('error') or body}")
    return r.json()


# ---------- commands ----------

def cmd_whoami(_args):
    data = _api("GET", "/api/v1/server/info")
    d = data.get("data") or {}
    return _envelope(
        True, "whoami",
        result="BlueBubbles server reachable",
        details={
            "server_version": d.get("server_version"),
            "macos_version": d.get("macos_version"),
            "imessage_version": d.get("imessage_version"),
            "detected_imessage": d.get("detected_imessage"),
            "private_api_enabled": d.get("private_api"),
            "proxy_service": d.get("proxy_service"),
        },
    )


def cmd_chats_list(args):
    body = {
        "limit": args.limit,
        "offset": 0,
        "with": ["lastMessage", "participants"],
    }
    data = _api("POST", "/api/v1/chat/query", json_body=body)
    chats = data.get("data") or []
    summarized = []
    for c in chats:
        last = c.get("lastMessage") or {}
        summarized.append({
            "guid": c.get("guid"),
            "displayName": c.get("displayName"),
            "isGroup": (c.get("style") == 43) or (len(c.get("participants") or []) > 1),
            "participants": [p.get("address") for p in (c.get("participants") or [])],
            "lastMessage": {
                "fromMe": last.get("isFromMe"),
                "text": _short_text(last.get("text")),
                "dateCreated": last.get("dateCreated"),
            } if last else None,
        })
    return _envelope(
        True, "chats list",
        result=f"{len(summarized)} chat(s)",
        details={"count": len(summarized), "chats": summarized},
    )


def cmd_chats_info(args):
    data = _api("GET", f"/api/v1/chat/{args.guid}", params={"with": "participants,lastmessage"})
    d = data.get("data") or {}
    return _envelope(
        True, "chats info",
        result=d.get("displayName") or d.get("guid"),
        details={
            "guid": d.get("guid"),
            "displayName": d.get("displayName"),
            "style": d.get("style"),
            "participants": [p.get("address") for p in (d.get("participants") or [])],
        },
    )


def cmd_messages_list(args):
    body: dict = {
        "limit": args.limit,
        "offset": 0,
        "with": ["chat", "handle", "attachment"],
        "sort": "DESC",
    }
    if args.since:
        delta = _parse_duration(args.since)
        after_ms = int((datetime.now(timezone.utc) - delta).timestamp() * 1000)
        body["after"] = after_ms
    if args.chat:
        body["chatGuid"] = args.chat
    data = _api("POST", "/api/v1/message/query", json_body=body)
    msgs = data.get("data") or []
    summarized, orphan_reactions = _split_reactions(msgs)
    return _envelope(
        True, "messages list",
        result=f"{len(summarized)} message(s)",
        details={
            "count": len(summarized),
            "filter_since": args.since,
            "filter_chat": args.chat,
            "messages": summarized,
            "orphan_reactions": orphan_reactions,
        },
    )


def cmd_messages_get(args):
    data = _api("GET", f"/api/v1/message/{args.guid}", params={"with": "chat,handle,attachment"})
    d = data.get("data") or {}
    return _envelope(True, "messages get", result="ok", details=d)


def cmd_messages_draft(args):
    """Preview what `messages send` would do, without sending."""
    payload = {
        "chatGuid": args.chat,
        "message": args.text,
        "method": "apple-script",
    }
    return _envelope(
        True, "messages draft",
        result="(draft — nothing sent; pipe to `messages send --confirm` to actually send)",
        details={
            "payload": payload,
            "preview": f"To {args.chat}: {args.text}",
        },
    )


def cmd_messages_send(args):
    if not args.confirm:
        return _envelope(
            False, "messages send",
            error="--confirm is required to actually send. Use `messages draft` to preview first.",
        )
    body = {
        "chatGuid": args.chat,
        "tempGuid": f"relay-{int(time.time() * 1000)}",
        "message": args.text,
        "method": "apple-script",
    }
    data = _api("POST", "/api/v1/message/text", json_body=body)
    d = data.get("data") or {}
    return _envelope(
        True, "messages send",
        result="message sent",
        details={
            "guid": d.get("guid"),
            "chatGuid": args.chat,
            "text": args.text,
            "dateCreated": d.get("dateCreated"),
        },
    )


def cmd_chats_find(args):
    """Fuzzy-find a chat by display name or participant address."""
    body = {"limit": args.scan, "offset": 0, "with": ["participants", "lastMessage"]}
    data = _api("POST", "/api/v1/chat/query", json_body=body)
    chats = data.get("data") or []
    query = args.query.lower().strip()

    scored: list[tuple[float, dict]] = []
    for c in chats:
        name = (c.get("displayName") or "").strip()
        parts = [(p.get("address") or "") for p in (c.get("participants") or [])]
        haystacks = [name] + parts
        haystacks_lc = [h.lower() for h in haystacks if h]
        if not haystacks_lc:
            continue
        # Substring hit > fuzzy match. Score 1.0 for substring, else SequenceMatcher ratio.
        score = 0.0
        for h in haystacks_lc:
            if query in h:
                score = max(score, 1.0 - (len(h) - len(query)) / max(len(h), 1) * 0.2)
            else:
                score = max(score, difflib.SequenceMatcher(None, query, h).ratio())
        if score < 0.5:
            continue
        last = c.get("lastMessage") or {}
        scored.append((score, {
            "guid": c.get("guid"),
            "displayName": name or None,
            "participants": parts,
            "score": round(score, 3),
            "lastMessage": {
                "fromMe": last.get("isFromMe"),
                "text": _short_text(last.get("text")),
                "dateCreated": last.get("dateCreated"),
            } if last else None,
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    matches = [m for _, m in scored[: args.limit]]
    return _envelope(
        True, "chats find",
        result=f"{len(matches)} match(es) for {args.query!r}",
        details={"query": args.query, "count": len(matches), "matches": matches},
    )


def cmd_messages_search(args):
    body: dict = {
        "limit": args.limit,
        "offset": 0,
        "with": ["chat", "handle", "attachment"],
        "sort": "DESC",
        "where": [
            {"statement": "message.text LIKE :text", "args": {"text": f"%{args.query}%"}}
        ],
    }
    if args.since:
        delta = _parse_duration(args.since)
        body["after"] = int((datetime.now(timezone.utc) - delta).timestamp() * 1000)
    if args.chat:
        body["chatGuid"] = args.chat
    data = _api("POST", "/api/v1/message/query", json_body=body)
    msgs = data.get("data") or []
    summarized, _orphans = _split_reactions(msgs)
    return _envelope(
        True, "messages search",
        result=f"{len(summarized)} hit(s) for {args.query!r}",
        details={
            "query": args.query,
            "filter_since": args.since,
            "filter_chat": args.chat,
            "count": len(summarized),
            "messages": summarized,
        },
    )


def cmd_messages_react(args):
    if args.reaction not in VALID_REACTIONS:
        return _envelope(
            False, "messages react",
            error=f"invalid reaction {args.reaction!r}. Valid: {sorted(VALID_REACTIONS)}",
        )
    if not args.confirm:
        return _envelope(
            False, "messages react",
            error="--confirm is required to send a reaction.",
        )
    # Need the target message's chatGuid; look it up.
    msg = _api("GET", f"/api/v1/message/{args.message}", params={"with": "chat"})
    md = msg.get("data") or {}
    chat = (md.get("chats") or [{}])[0]
    chat_guid = chat.get("guid")
    if not chat_guid:
        return _envelope(False, "messages react", error=f"could not resolve chat for message {args.message}")
    body = {
        "chatGuid": chat_guid,
        "selectedMessageGuid": args.message,
        "reaction": args.reaction,
    }
    data = _api("POST", "/api/v1/message/react", json_body=body)
    d = data.get("data") or {}
    return _envelope(
        True, "messages react",
        result=f"reacted {args.reaction!r} to {args.message}",
        details={"reactionGuid": d.get("guid"), "targetMessage": args.message, "chatGuid": chat_guid},
    )


def cmd_attachments_download(args):
    # Fetch metadata so we can use the original filename.
    meta = _api("GET", f"/api/v1/attachment/{args.guid}")
    md = meta.get("data") or {}
    name = md.get("transferName") or f"attachment-{args.guid}"
    out_dir = Path(args.out).expanduser() if args.out else DEFAULT_DOWNLOAD_DIR
    if out_dir.is_dir() or args.out is None:
        out_path = out_dir / name
    else:
        out_path = out_dir
    size = _api_download(f"/api/v1/attachment/{args.guid}/download", out_path)
    return _envelope(
        True, "attachments download",
        result=f"saved {size} bytes to {out_path}",
        details={
            "guid": args.guid,
            "path": str(out_path),
            "bytes": size,
            "mimeType": md.get("mimeType"),
            "transferName": md.get("transferName"),
        },
    )


def _send_image_inner(chat_guid: str, src: str, caption: str | None):
    local, is_temp = _resolve_image_source(src)
    try:
        api_resp = _upload_attachment(chat_guid, local)
        d = api_resp.get("data") or {}
        followup = None
        if caption:
            cap_body = {
                "chatGuid": chat_guid,
                "tempGuid": f"relay-{int(time.time() * 1000)}",
                "message": caption,
                "method": "apple-script",
            }
            cap = _api("POST", "/api/v1/message/text", json_body=cap_body)
            followup = (cap.get("data") or {}).get("guid")
        return {
            "guid": d.get("guid"),
            "captionGuid": followup,
            "chatGuid": chat_guid,
            "file": str(local),
            "dateCreated": d.get("dateCreated"),
        }
    finally:
        if is_temp:
            try:
                local.unlink()
            except OSError:
                pass


def cmd_messages_send_image(args):
    if not args.confirm:
        return _envelope(False, "messages send-image", error="--confirm is required to actually send.")
    details = _send_image_inner(args.chat, args.source, args.text)
    return _envelope(True, "messages send-image", result="image sent", details=details)


def _resolve_chat_from_message(message_guid: str) -> str:
    """Look up the chat GUID that owns a given message GUID. Single API call."""
    resp = _api("GET", f"/api/v1/message/{message_guid}", params={"with": "chat"})
    md = resp.get("data") or {}
    chat = (md.get("chats") or [{}])[0]
    chat_guid = chat.get("guid")
    if not chat_guid:
        raise RuntimeError(f"could not resolve chat for message {message_guid}")
    return chat_guid


def cmd_messages_reply(args):
    if not args.confirm:
        return _envelope(False, "messages reply", error="--confirm is required to actually send.")
    chat_guid = _resolve_chat_from_message(args.message)
    body = {
        "chatGuid": chat_guid,
        "tempGuid": f"relay-{int(time.time() * 1000)}",
        "message": args.text,
        "method": "apple-script",
    }
    data = _api("POST", "/api/v1/message/text", json_body=body)
    d = data.get("data") or {}
    return _envelope(
        True, "messages reply",
        result=f"reply sent to chat {chat_guid}",
        details={
            "guid": d.get("guid"),
            "replyToMessage": args.message,
            "chatGuid": chat_guid,
            "text": args.text,
            "dateCreated": d.get("dateCreated"),
        },
    )


def cmd_messages_reply_image(args):
    if not args.confirm:
        return _envelope(False, "messages reply-image", error="--confirm is required to actually send.")
    chat_guid = _resolve_chat_from_message(args.message)
    details = _send_image_inner(chat_guid, args.source, args.text)
    details["replyToMessage"] = args.message
    return _envelope(True, "messages reply-image", result=f"reply image sent to chat {chat_guid}", details=details)


def cmd_messages_reply_sticker(args):
    if not args.confirm:
        return _envelope(False, "messages reply-sticker", error="--confirm is required to actually send.")
    chat_guid = _resolve_chat_from_message(args.message)
    path = _resolve_sticker(args.name)
    details = _send_image_inner(chat_guid, str(path), args.text)
    details["sticker"] = args.name
    details["replyToMessage"] = args.message
    return _envelope(True, "messages reply-sticker", result=f"reply sticker {args.name!r} sent to chat {chat_guid}", details=details)


def cmd_stickers_list(_args):
    if not STICKERS_DIR.exists():
        return _envelope(True, "stickers list", result="0 sticker(s)", details={"count": 0, "stickers": []})
    items = []
    for p in sorted(STICKERS_DIR.iterdir()):
        if p.is_file() and not p.name.startswith("."):
            items.append({"name": p.stem, "file": p.name, "bytes": p.stat().st_size})
    return _envelope(
        True, "stickers list",
        result=f"{len(items)} sticker(s)",
        details={"count": len(items), "dir": str(STICKERS_DIR), "stickers": items},
    )


def cmd_messages_send_sticker(args):
    if not args.confirm:
        return _envelope(False, "messages send-sticker", error="--confirm is required to actually send.")
    path = _resolve_sticker(args.name)
    details = _send_image_inner(args.chat, str(path), args.text)
    details["sticker"] = args.name
    return _envelope(True, "messages send-sticker", result=f"sent sticker {args.name!r}", details=details)


# ---------- entry ----------

def main():
    ap = argparse.ArgumentParser(prog="messages_cli", description="BlueBubbles iMessage relay")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="Server info / health check").set_defaults(func=cmd_whoami)

    chats = sub.add_parser("chats", help="Chat operations").add_subparsers(dest="action", required=True)
    cl = chats.add_parser("list", help="List recent chats")
    cl.add_argument("--limit", type=int, default=20)
    cl.set_defaults(func=cmd_chats_list)
    ci = chats.add_parser("info", help="Chat metadata by GUID")
    ci.add_argument("guid")
    ci.set_defaults(func=cmd_chats_info)
    cf = chats.add_parser("find", help="Fuzzy-find chats by name or participant")
    cf.add_argument("query", help="name or phone/email fragment, e.g. 'mom' or '512'")
    cf.add_argument("--limit", type=int, default=5, help="max matches to return")
    cf.add_argument("--scan", type=int, default=200, help="how many recent chats to search through")
    cf.set_defaults(func=cmd_chats_find)

    msgs = sub.add_parser("messages", help="Message operations").add_subparsers(dest="action", required=True)
    ml = msgs.add_parser("list", help="List recent messages")
    ml.add_argument("--since", help="lookback window e.g. 5m / 1h / 2d")
    ml.add_argument("--chat", help="filter to a single chat GUID")
    ml.add_argument("--limit", type=int, default=50)
    ml.set_defaults(func=cmd_messages_list)
    mg = msgs.add_parser("get", help="Full message detail by GUID")
    mg.add_argument("guid")
    mg.set_defaults(func=cmd_messages_get)
    mse = msgs.add_parser("search", help="Text search across messages")
    mse.add_argument("query", help="text to search for")
    mse.add_argument("--chat", help="restrict to a single chat GUID")
    mse.add_argument("--since", help="lookback window e.g. 1d / 2w")
    mse.add_argument("--limit", type=int, default=50)
    mse.set_defaults(func=cmd_messages_search)
    md = msgs.add_parser("draft", help="Preview a send (does NOT send)")
    md.add_argument("chat", help="chat GUID to send to")
    md.add_argument("text", help="message body")
    md.set_defaults(func=cmd_messages_draft)
    ms = msgs.add_parser("send", help="Actually send a message (requires --confirm)")
    ms.add_argument("chat", help="chat GUID to send to")
    ms.add_argument("text", help="message body")
    ms.add_argument("--confirm", action="store_true", help="required to actually send")
    ms.set_defaults(func=cmd_messages_send)
    mr = msgs.add_parser("react", help="Send a tapback reaction (requires --confirm)")
    mr.add_argument("message", help="message GUID to react to")
    mr.add_argument("reaction", help=f"one of: {', '.join(sorted(VALID_REACTIONS))}")
    mr.add_argument("--confirm", action="store_true", help="required to actually send")
    mr.set_defaults(func=cmd_messages_react)
    mi = msgs.add_parser("send-image", help="Send an image from local path or URL (requires --confirm)")
    mi.add_argument("chat", help="chat GUID to send to")
    mi.add_argument("source", help="local file path or http(s) URL")
    mi.add_argument("--text", help="optional caption sent as a follow-up message")
    mi.add_argument("--confirm", action="store_true", help="required to actually send")
    mi.set_defaults(func=cmd_messages_send_image)
    mst = msgs.add_parser("send-sticker", help="Send a sticker from the repo stickers/ folder (requires --confirm)")
    mst.add_argument("chat", help="chat GUID to send to")
    mst.add_argument("name", help="sticker name (without extension), e.g. peepo-happy")
    mst.add_argument("--text", help="optional caption sent as a follow-up message")
    mst.add_argument("--confirm", action="store_true", help="required to actually send")
    mst.set_defaults(func=cmd_messages_send_sticker)
    mrp = msgs.add_parser("reply", help="Reply to a message — auto-resolves the chat (requires --confirm)")
    mrp.add_argument("message", help="message GUID to reply to")
    mrp.add_argument("text", help="reply body")
    mrp.add_argument("--confirm", action="store_true", help="required to actually send")
    mrp.set_defaults(func=cmd_messages_reply)
    mri = msgs.add_parser("reply-image", help="Reply to a message with an image (requires --confirm)")
    mri.add_argument("message", help="message GUID to reply to")
    mri.add_argument("source", help="local file path or http(s) URL")
    mri.add_argument("--text", help="optional caption sent as a follow-up message")
    mri.add_argument("--confirm", action="store_true", help="required to actually send")
    mri.set_defaults(func=cmd_messages_reply_image)
    mrs = msgs.add_parser("reply-sticker", help="Reply to a message with a sticker from stickers/ (requires --confirm)")
    mrs.add_argument("message", help="message GUID to reply to")
    mrs.add_argument("name", help="sticker name (without extension)")
    mrs.add_argument("--text", help="optional caption sent as a follow-up message")
    mrs.add_argument("--confirm", action="store_true", help="required to actually send")
    mrs.set_defaults(func=cmd_messages_reply_sticker)

    att = sub.add_parser("attachments", help="Attachment operations").add_subparsers(dest="action", required=True)
    ad = att.add_parser("download", help="Download an attachment by GUID")
    ad.add_argument("guid", help="attachment GUID (from messages get)")
    ad.add_argument("--out", help=f"output file or directory (default: {DEFAULT_DOWNLOAD_DIR})")
    ad.set_defaults(func=cmd_attachments_download)

    stk = sub.add_parser("stickers", help="Sticker library").add_subparsers(dest="action", required=True)
    sl = stk.add_parser("list", help="List available stickers")
    sl.set_defaults(func=cmd_stickers_list)

    args = ap.parse_args()
    cmd_label = args.cmd + (f" {args.action}" if getattr(args, "action", None) else "")

    try:
        out = args.func(args)
    except Exception as e:
        out = _envelope(False, cmd_label, error=f"{type(e).__name__}: {e}")

    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out.get("success") else 1)


if __name__ == "__main__":
    main()
