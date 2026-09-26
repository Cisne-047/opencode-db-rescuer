"""Migrate OpenCode 1.x sessions into a 2.0.18 database.

Reads the renamed v1/beta backup (opencode.db.pre-v2-fix) and writes
session_v2 / session_message rows in the shape OpenCode 2.0.18 decodes.
The beta install is never launched, and the backup is opened read-only.

By default the result is a new file, opencode.v1-migrated.db, cloned from
the working database so the Desktop app keeps running. Pass --apply only
after OpenCode is fully closed to swap that file into place.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote

ROOT = Path.home() / ".local" / "share" / "opencode"
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
FINISH = {"stop", "length", "tool-calls", "content-filter", "error", "unknown"}
TOOL_RENAMES = {"bash": "shell", "task": "subagent", "apply_patch": "patch"}
PATH_TOOLS = ("read", "edit", "write")
REMOVED_TOOLS = ("todowrite",)
ERROR_TYPES = {
    "ProviderAuthError": "provider.auth",
    "ContentFilterError": "provider.content-filter",
    "ContextOverflowError": "provider.invalid-request",
    "StructuredOutputError": "provider.invalid-output",
    "MessageOutputLengthError": "provider.invalid-output",
    "MessageAbortedError": "aborted",
    "APIError": "provider.error",
}


def canon(path: str | None) -> str:
    if not path:
        return ""
    text = path.replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        text = text[0].upper() + text[1:]
    while len(text) > 3 and text.endswith("/"):
        text = text[:-1]
    return text


def project_id_for_directory(worktree: str) -> str:
    """OpenCode 2.0.18 id for a directory that does not already have a project row."""
    if worktree in ("", "/"):
        return "global"
    native = worktree.replace("/", "\\")
    return hashlib.sha1(f"directory:{native}".encode("utf-8")).hexdigest()


def visible_path(directory: str, path: str | None) -> str:
    """2.0.18 shows a session when path is empty and directory is the project root.

    V1 global sessions stored either the full directory or the directory with
    the drive prefix removed. Both hide the row from the project session list.
    """
    raw = canon(path)
    if raw in ("", directory):
        return ""
    if len(directory) > 2 and directory[1] == ":" and raw == directory[2:].lstrip("/"):
        return ""
    if raw.lower() == directory.lower():
        return ""
    return raw or ""


def loads(raw: str | None):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def paragraph(parts: list[str]) -> str:
    return "\n\n".join(parts)


def synthetic_id(source_id: str, used: set[str]) -> str:
    prefix = source_id[:16]
    attempt = 0
    while True:
        material = (
            f"v1-synthetic:{source_id}"
            if attempt == 0
            else f"v1-synthetic:{source_id}:{attempt}"
        )
        value = int(hashlib.sha256(material.encode("utf-8")).hexdigest(), 16)
        chars = ""
        while len(chars) < 14:
            chars = ALPHABET[value % 62] + chars
            value //= 62
        candidate = prefix + chars
        if candidate not in used:
            used.add(candidate)
            return candidate
        attempt += 1


def map_finish(value) -> str | None:
    if not value:
        return None
    if value in FINISH:
        return value
    return "unknown"


def message_error(error) -> dict:
    if not isinstance(error, dict):
        return {"type": "unknown", "message": str(error)}
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    if "message" in data:
        message = data["message"]
    elif error.get("name") == "MessageOutputLengthError":
        message = "The model exceeded its output limit"
    else:
        message = error.get("name") or "Unknown error"
    if not isinstance(message, str):
        message = dumps(message)
    name = str(error.get("name") or "")
    return {"type": ERROR_TYPES.get(name, "unknown"), "message": message}


def model_object(value) -> dict | None:
    parsed = loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        return None
    model_id = parsed.get("id") or parsed.get("modelID")
    provider = parsed.get("providerID")
    if not model_id or not provider:
        return None
    return {
        "id": model_id,
        "providerID": provider,
        "variant": parsed.get("variant") or "default",
    }


def model_column(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = model_object(value)
        return dumps(parsed) if parsed else value
    if isinstance(value, dict):
        parsed = model_object(value)
        return dumps(parsed) if parsed else None
    return None


def attachment_placeholder(part: dict) -> str:
    source = part.get("source") if isinstance(part.get("source"), dict) else {}
    name = part.get("filename")
    if not name and source.get("type") == "resource":
        name = source.get("uri")
    if not name:
        name = part.get("url")
    return f"[Attachment unavailable after migration: {name} ({part.get('mime')})]"


def inline_files(part: dict) -> list[dict]:
    url = part.get("url") or ""
    if not isinstance(url, str) or not url.startswith("data:"):
        return []
    comma = url.find(",")
    if comma < 0:
        return []
    head, payload = url[:comma], url[comma + 1 :]
    try:
        if head.endswith(";base64"):
            data = base64.b64encode(base64.b64decode(payload)).decode("ascii")
        else:
            data = base64.b64encode(unquote(payload).encode("utf-8")).decode("ascii")
    except Exception:
        return []
    source = part.get("source") if isinstance(part.get("source"), dict) else None
    if source and source.get("type") == "resource":
        source_out = {"type": "uri", "uri": source.get("uri")}
    else:
        source_out = {"type": "inline"}
    item = {"data": data, "mime": part.get("mime"), "source": source_out}
    if part.get("filename"):
        item["name"] = part["filename"]
    if source:
        text = source.get("text") if isinstance(source.get("text"), dict) else {}
        item["mention"] = {
            "text": text.get("value"),
            "start": text.get("start"),
            "end": text.get("end"),
        }
    return [item]


def tool_error_message(error) -> str:
    if isinstance(error, dict):
        message = error.get("message") or error.get("name")
        if isinstance(message, str):
            return message
        return dumps(error)
    if error is None:
        return ""
    return str(error)


def convert_tool(part: dict, fallback_time: int) -> dict:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    item = {"type": "tool", "id": part.get("callID") or part.get("id"), "name": part.get("tool") or "tool"}
    if part.get("metadata"):
        item["providerState"] = part["metadata"]
    status = state.get("status")
    timing = state.get("time") if isinstance(state.get("time"), dict) else {}
    if status == "completed":
        if timing.get("compacted") is not None:
            content = [{"type": "text", "text": "[Old tool result content cleared]"}]
        else:
            output = state.get("output")
            if isinstance(output, str):
                text = output
            elif output is None:
                text = ""
            else:
                text = dumps(output)
            content = [{"type": "text", "text": text}]
            attachments = state.get("attachments") or []
            if isinstance(attachments, list):
                for attachment in attachments:
                    if not isinstance(attachment, dict) or not attachment.get("url"):
                        continue
                    file_item = {
                        "type": "file",
                        "uri": attachment["url"],
                        "mime": attachment.get("mime") or "application/octet-stream",
                    }
                    if attachment.get("filename"):
                        file_item["name"] = attachment["filename"]
                    content.append(file_item)
        tool_state = {"status": "completed", "input": state.get("input") or {}, "content": content}
        if state.get("metadata") is not None:
            tool_state["metadata"] = state["metadata"]
        created = timing.get("start", fallback_time)
        tool_time = {"created": created}
        if timing.get("end") is not None:
            tool_time["completed"] = timing["end"]
        item["state"] = tool_state
        item["time"] = tool_time
        return item
    if status == "error":
        tool_state = {
            "status": "error",
            "input": state.get("input") or {},
            "error": {"type": "tool.execution", "message": tool_error_message(state.get("error"))},
        }
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else None
        if metadata and isinstance(metadata.get("output"), str):
            tool_state["content"] = [{"type": "text", "text": metadata["output"]}]
        if metadata is not None:
            tool_state["metadata"] = metadata
        tool_time = {"created": timing.get("start", fallback_time)}
        if timing.get("end") is not None:
            tool_time["completed"] = timing["end"]
        item["state"] = tool_state
        item["time"] = tool_time
        return item
    tool_state = {
        "status": "error",
        "input": state.get("input") or {},
        "error": {
            "type": "tool.interrupted",
            "message": "Tool execution was interrupted before V2 migration",
        },
    }
    if status == "running" and state.get("metadata") is not None:
        tool_state["metadata"] = state["metadata"]
    created = timing.get("start", fallback_time) if status == "running" else fallback_time
    item["state"] = tool_state
    item["time"] = {"created": created}
    return item


def assistant_content(parts: list[dict], created: int) -> list[dict]:
    content = []
    for part in parts:
        kind = part.get("type")
        if kind == "text":
            item = {"type": "text", "text": part.get("text") or ""}
            if part.get("metadata"):
                item["state"] = part["metadata"]
            content.append(item)
        elif kind == "reasoning":
            item = {"type": "reasoning", "text": part.get("text") or ""}
            if part.get("metadata"):
                item["state"] = part["metadata"]
            timing = part.get("time") if isinstance(part.get("time"), dict) else None
            if timing and timing.get("start") is not None:
                item_time = {"created": timing["start"]}
                if timing.get("end") is not None:
                    item_time["completed"] = timing["end"]
                item["time"] = item_time
            content.append(item)
        elif kind == "tool":
            content.append(convert_tool(part, created))
    return content


def snapshot_of(parts: list[dict]) -> dict | None:
    start = next(
        (part.get("snapshot") for part in parts if part.get("type") == "step-start" and part.get("snapshot")),
        None,
    )
    if start is None:
        start = next(
            (part.get("snapshot") for part in parts if part.get("type") == "snapshot" and part.get("snapshot")),
            None,
        )
    if start is None:
        start = next(
            (part.get("hash") for part in parts if part.get("type") == "patch" and part.get("hash")),
            None,
        )
    ends = [part.get("snapshot") for part in parts if part.get("type") == "step-finish" and part.get("snapshot")]
    files: list = []
    seen = set()
    for part in parts:
        if part.get("type") != "patch":
            continue
        for name in part.get("files") or []:
            if name in seen:
                continue
            seen.add(name)
            files.append(name)
    if not start and not ends and not files:
        return None
    snapshot = {}
    if start:
        snapshot["start"] = start
    if ends:
        snapshot["end"] = ends[-1]
    if files:
        snapshot["files"] = files
    return snapshot


def recent_transcript(messages: list[dict], parts_by_id: dict, start: int, end: int) -> str:
    lines = []
    for message in messages[start:end]:
        parts = parts_by_id.get(message["row"]["id"], [])
        if message["value"].get("role") == "user":
            text = paragraph(
                part.get("text") or ""
                for part in parts
                if part.get("type") == "text" and not part.get("ignored")
            )
            lines.append(f"[User]: {text}")
            continue
        for part in parts:
            if part.get("type") == "text":
                lines.append(f"[Assistant]: {part.get('text') or ''}")
            elif part.get("type") == "reasoning" and part.get("text"):
                lines.append(f"[Assistant reasoning]: {part['text']}")
    return paragraph(lines)


def system_note(messages: list[dict]) -> str | None:
    start = 0
    for index, message in enumerate(messages):
        if message["type"] == "compaction":
            start = index + 1
    names = []
    for message in messages[start:]:
        if message["type"] != "assistant":
            continue
        content = message["data"].get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool" and isinstance(item.get("name"), str):
                names.append(item["name"])
    used = set(names)
    renamed = [name for name in TOOL_RENAMES if name in used]
    path_tools = [name for name in PATH_TOOLS if name in used]
    removed = [name for name in REMOVED_TOOLS if name in used]
    notes = []
    if len(renamed) == 1:
        old = renamed[0]
        notes.append(f"The `{old}` tool is now `{TOOL_RENAMES[old]}` and must be called by that name.")
    elif len(renamed) > 1:
        pairs = "; ".join(f"`{name}` is now `{TOOL_RENAMES[name]}`" for name in renamed)
        notes.append(f"The following tools were renamed and must be called by their new names: {pairs}.")
    if "task" in used:
        notes.append("The `subagent` tool takes `agent` instead of `subagent_type` and `sessionID` instead of `task_id`.")
    if len(path_tools) == 1:
        notes.append(f"The `{path_tools[0]}` tool now takes `path` instead of `filePath`.")
    elif len(path_tools) > 1:
        listed = ", ".join(f"`{name}`" for name in path_tools)
        notes.append(f"The following tools now take `path` instead of `filePath`: {listed}.")
    if "skill" in used:
        notes.append("The `skill` tool now takes `id` instead of `name`.")
    if len(removed) == 1:
        notes.append(f"The `{removed[0]}` tool is no longer available and must not be called.")
    elif len(removed) > 1:
        listed = ", ".join(f"`{name}`" for name in removed)
        notes.append(f"The following tools are no longer available and must not be called: {listed}.")
    if not notes:
        return None
    return paragraph(["The available tools have changed.", *notes])


def projected_row(row: dict, payload: dict) -> dict:
    body = dict(payload)
    message_id = body.pop("id")
    message_type = body.pop("type")
    return {
        "id": message_id,
        "session_id": row["session_id"],
        "type": message_type,
        "seq": 0,
        "time_created": row["time_created"],
        "time_updated": row["time_updated"],
        "data": body,
    }


def transform_session(session: dict, message_rows: list[dict], part_rows: list[dict]) -> dict:
    warnings = []
    decoded = []
    for row in message_rows:
        value = loads(row["data"])
        if not isinstance(value, dict) or value.get("role") not in ("user", "assistant"):
            warnings.append({"reason": "invalid-message", "sessionID": session["id"], "messageID": row["id"]})
            continue
        value = dict(value)
        value["id"] = row["id"]
        value["sessionID"] = row["session_id"]
        decoded.append({"row": row, "value": value})
    decoded.sort(key=lambda item: (item["row"]["time_created"], item["row"]["id"]))
    message_ids = {item["row"]["id"] for item in decoded}
    known_ids = {row["id"] for row in message_rows}

    parts = []
    for row in part_rows:
        if row["message_id"] not in known_ids:
            warnings.append(
                {
                    "reason": "orphan-part",
                    "sessionID": session["id"],
                    "messageID": row["message_id"],
                    "partID": row["id"],
                }
            )
            continue
        value = loads(row["data"])
        if not isinstance(value, dict) or "type" not in value:
            warnings.append(
                {
                    "reason": "invalid-part",
                    "sessionID": session["id"],
                    "messageID": row["message_id"],
                    "partID": row["id"],
                }
            )
            continue
        value = dict(value)
        value["id"] = row["id"]
        value["messageID"] = row["message_id"]
        value["sessionID"] = row["session_id"]
        parts.append({"row": row, "value": value})
    parts.sort(key=lambda item: item["row"]["id"])
    parts_by_message: dict[str, list[dict]] = {}
    for item in parts:
        parts_by_message.setdefault(item["row"]["message_id"], []).append(item["value"])

    consumed = set()
    used_ids = set(message_ids)
    projected = []
    for item in decoded:
        if item["row"]["id"] in consumed:
            continue
        own_parts = parts_by_message.get(item["row"]["id"], [])
        role = item["value"].get("role")
        if role == "user":
            projected.extend(project_user(item, own_parts, decoded, parts_by_message, consumed, used_ids))
            continue
        if role != "assistant":
            continue
        parent_id = item["value"].get("parentID")
        parent_parts = parts_by_message.get(parent_id, []) if parent_id else []
        if any(part.get("type") == "subtask" for part in parent_parts) and any(
            part.get("type") == "tool" and part.get("tool") == "task" for part in own_parts
        ):
            continue
        projected.append(project_assistant(item, own_parts))

    for index, message in enumerate(projected):
        message["seq"] = index
    note = system_note(projected)
    if projected and note:
        last = projected[-1]
        projected.append(
            projected_row(
                {
                    "session_id": last["session_id"],
                    "time_created": last["time_created"],
                    "time_updated": last["time_updated"],
                },
                {
                    "id": synthetic_id(last["id"], used_ids),
                    "type": "system",
                    "text": note,
                    "time": {"created": last["time_created"]},
                },
            )
        )
        projected[-1]["seq"] = len(projected) - 1

    assistants = [item["value"] for item in decoded if item["value"].get("role") == "assistant"]
    anchor = last_user(decoded, parts_by_message)
    agent = session.get("agent")
    if not agent and anchor and anchor["value"].get("role") == "user":
        agent = anchor["value"].get("agent")
    model = model_object(session.get("model"))
    if model is None and anchor and anchor["value"].get("role") == "user":
        model = model_object(anchor["value"].get("model"))

    def token(name: str) -> int:
        total = 0
        for assistant in assistants:
            tokens = assistant.get("tokens") if isinstance(assistant.get("tokens"), dict) else {}
            if name == "cache_read":
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                total += int(cache.get("read") or 0)
            elif name == "cache_write":
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                total += int(cache.get("write") or 0)
            else:
                total += int(tokens.get(name) or 0)
        return total

    return {
        "messages": projected,
        "warnings": warnings,
        "watermark": len(projected) - 1,
        "session": {
            "agent": agent,
            "model": model_column(model),
            "cost": sum(float(item.get("cost") or 0) for item in assistants),
            "tokens_input": token("input"),
            "tokens_output": token("output"),
            "tokens_reasoning": token("reasoning"),
            "tokens_cache_read": token("cache_read"),
            "tokens_cache_write": token("cache_write"),
        },
    }


def last_user(decoded: list[dict], parts_by_message: dict[str, list[dict]]):
    for item in reversed(decoded):
        if item["value"].get("role") != "user":
            continue
        parts = parts_by_message.get(item["row"]["id"], [])
        if any(part.get("type") == "compaction" for part in parts):
            continue
        subtasks = [part for part in parts if part.get("type") == "subtask"]
        if subtasks and len(subtasks) == len(parts):
            continue
        return item
    return None


def project_user(item, own_parts, decoded, parts_by_message, consumed, used_ids) -> list[dict]:
    compaction = next((part for part in own_parts if part.get("type") == "compaction"), None)
    if compaction:
        summary = next(
            (
                candidate
                for candidate in decoded
                if candidate["value"].get("role") == "assistant"
                and candidate["value"].get("parentID") == item["row"]["id"]
                and candidate["value"].get("summary") is True
            ),
            None,
        )
        if summary is None:
            return []
        consumed.add(summary["row"]["id"])
        if summary["value"].get("error") or summary["value"].get("time", {}).get("completed") is None:
            return []
        summary_parts = parts_by_message.get(summary["row"]["id"], [])
        summary_text = paragraph(
            part.get("text") or ""
            for part in summary_parts
            if part.get("type") == "text" and part.get("text")
        )
        tail_id = compaction.get("tail_start_id")
        tail_index = next((i for i, candidate in enumerate(decoded) if candidate["row"]["id"] == tail_id), -1) if tail_id else -1
        user_index = next(i for i, candidate in enumerate(decoded) if candidate["row"]["id"] == item["row"]["id"])
        recent = "" if tail_index < 0 else recent_transcript(decoded, parts_by_message, tail_index, user_index)
        row = dict(item["row"])
        row["time_updated"] = max(item["row"]["time_updated"], summary["row"]["time_updated"])
        return [
            projected_row(
                row,
                {
                    "id": item["row"]["id"],
                    "type": "compaction",
                    "status": "completed",
                    "reason": "auto" if compaction.get("auto") else "manual",
                    "summary": summary_text,
                    "recent": recent,
                    "time": {"created": item["row"]["time_created"]},
                },
            )
        ]

    subtasks = [part for part in own_parts if part.get("type") == "subtask"]
    texts = [part for part in own_parts if part.get("type") == "text" and not part.get("ignored")]
    files = [part for part in own_parts if part.get("type") == "file"]
    agents = [part for part in own_parts if part.get("type") == "agent"]
    if subtasks and not texts and not files and not agents:
        return []
    real_text = [part for part in texts if not part.get("synthetic")]
    synthetic = [part for part in texts if part.get("synthetic")]
    inline = [file for part in files for file in inline_files(part)]
    external = [part for part in files if not str(part.get("url") or "").startswith("data:")]
    chunks = []
    for part in own_parts:
        if part.get("type") == "text" and not part.get("ignored") and not part.get("synthetic"):
            chunks.append(part.get("text") or "")
        elif part.get("type") == "file" and not str(part.get("url") or "").startswith("data:"):
            chunks.append(attachment_placeholder(part))
    agent_items = []
    for part in agents:
        entry = {"name": part.get("name")}
        source = part.get("source") if isinstance(part.get("source"), dict) else None
        if source:
            entry["mention"] = {
                "text": source.get("value"),
                "start": source.get("start"),
                "end": source.get("end"),
            }
        agent_items.append(entry)
    if not real_text and not external and synthetic and not inline and not agent_items:
        return [
            projected_row(
                item["row"],
                {
                    "id": item["row"]["id"],
                    "type": "synthetic",
                    "text": paragraph(part.get("text") or "" for part in synthetic),
                    "time": {"created": item["row"]["time_created"]},
                },
            )
        ]
    user = {
        "id": item["row"]["id"],
        "type": "user",
        "text": paragraph(chunks),
        "time": {"created": item["row"]["time_created"]},
    }
    if inline:
        user["files"] = inline
    if agent_items:
        user["agents"] = agent_items
    rows = [projected_row(item["row"], user)]
    if synthetic:
        rows.append(
            projected_row(
                item["row"],
                {
                    "id": synthetic_id(item["row"]["id"], used_ids),
                    "type": "synthetic",
                    "text": paragraph(part.get("text") or "" for part in synthetic),
                    "time": {"created": item["row"]["time_created"]},
                },
            )
        )
    return rows


def project_assistant(item, own_parts) -> dict:
    value = item["value"]
    tokens = value.get("tokens") if isinstance(value.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    variant = value.get("variant") or "default"
    payload = {
        "id": item["row"]["id"],
        "type": "assistant",
        "agent": value.get("agent") or value.get("mode") or "build",
        "model": {
            "providerID": value.get("providerID") or "unknown",
            "id": value.get("modelID") or "unknown",
            "variant": variant,
        },
        "content": assistant_content(own_parts, item["row"]["time_created"]),
        "cost": float(value.get("cost") or 0),
        "tokens": {
            "input": int(tokens.get("input") or 0),
            "output": int(tokens.get("output") or 0),
            "reasoning": int(tokens.get("reasoning") or 0),
            "cache": {
                "read": int(cache.get("read") or 0),
                "write": int(cache.get("write") or 0),
            },
        },
        "time": {"created": item["row"]["time_created"]},
    }
    timing = value.get("time") if isinstance(value.get("time"), dict) else {}
    if timing.get("completed") is not None:
        payload["time"]["completed"] = item["row"]["time_updated"]
    snapshot = snapshot_of(own_parts)
    if snapshot:
        payload["snapshot"] = snapshot
    finish = map_finish(value.get("finish"))
    if finish:
        payload["finish"] = finish
    if value.get("error"):
        payload["error"] = message_error(value["error"])
    return projected_row(item["row"], payload)


def connect_source(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def snapshot_database(src: Path, dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(dest) + suffix)
        if side.exists():
            side.unlink()
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def load_projects(dest: sqlite3.Connection) -> dict[str, str]:
    found = {}
    for project_id, worktree in dest.execute("SELECT id, worktree FROM project"):
        found[canon(worktree)] = project_id
    return found


def ensure_project(dest: sqlite3.Connection, projects: dict[str, str], directory: str, when: int) -> str:
    worktree = canon(directory) or "/"
    existing = projects.get(worktree)
    if existing:
        return existing
    if worktree == "/":
        projects["/"] = "global"
        return "global"
    project_id = project_id_for_directory(worktree)
    vcs = "git" if (Path(worktree) / ".git").exists() else None
    dest.execute(
        """
        INSERT OR IGNORE INTO project (
            id, worktree, vcs, name, time_created, time_updated, time_active, sandboxes
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, '[]')
        """,
        (project_id, worktree, vcs, when, when, when),
    )
    projects[worktree] = project_id
    return project_id


def rehome_global_sessions(dest: sqlite3.Connection, projects: dict[str, str]) -> int:
    rows = dest.execute(
        """
        SELECT id, directory, path, time_created, time_updated
        FROM session_v2
        WHERE project_id = 'global' AND directory NOT IN ('', '/')
        """
    ).fetchall()
    changed = 0
    for row in rows:
        directory = canon(row["directory"])
        project_id = ensure_project(dest, projects, directory, row["time_created"])
        path = visible_path(directory, row["path"])
        dest.execute(
            """
            UPDATE session_v2
            SET project_id = ?, directory = ?, path = ?, workspace_id = NULL
            WHERE id = ?
            """,
            (project_id, directory, path, row["id"]),
        )
        changed += 1
    return changed


def insert_session(dest: sqlite3.Connection, session: dict, project_id: str, directory: str, transformed: dict) -> None:
    patch = transformed["session"]
    dest.execute(
        """
        INSERT INTO session_v2 (
            id, project_id, workspace_id, parent_id, slug, directory, path, title, version,
            share_url, summary_additions, summary_deletions, summary_files, summary_diffs,
            metadata, cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,
            tokens_cache_write, revert, permission, agent, model, time_created, time_updated,
            time_compacting, time_archived
        ) VALUES (
            ?, ?, NULL, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?,
            ?, NULL, NULL, ?, ?, ?, ?,
            NULL, ?
        )
        """,
        (
            session["id"],
            project_id,
            session["parent_id"],
            session["slug"],
            directory,
            visible_path(directory, session["path"]),
            session["title"],
            session["version"],
            session["share_url"],
            session["summary_additions"],
            session["summary_deletions"],
            session["summary_files"],
            session["summary_diffs"],
            session["metadata"],
            patch["cost"],
            patch["tokens_input"],
            patch["tokens_output"],
            patch["tokens_reasoning"],
            patch["tokens_cache_read"],
            patch["tokens_cache_write"],
            patch["agent"],
            patch["model"],
            session["time_created"],
            session["time_updated"],
            session["time_archived"],
        ),
    )
    dest.executemany(
        """
        INSERT INTO session_message (
            id, session_id, type, seq, time_created, time_updated, data
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                message["id"],
                message["session_id"],
                message["type"],
                message["seq"],
                message["time_created"],
                message["time_updated"],
                dumps(message["data"]),
            )
            for message in transformed["messages"]
        ],
    )
    dest.execute(
        """
        INSERT INTO event_sequence (aggregate_id, seq, owner_id)
        VALUES (?, ?, NULL)
        ON CONFLICT(aggregate_id) DO UPDATE SET seq = excluded.seq, owner_id = NULL
        """,
        (session["id"], transformed["watermark"]),
    )


def default_source() -> Path:
    original = ROOT / "opencode.db.pre-v2-fix"
    if original.exists():
        return original
    copies = sorted(ROOT.glob("history-recovery-*/old.db"), reverse=True)
    if copies:
        return copies[0]
    raise SystemExit(f"No v1 backup found in {ROOT}")


def migrate(source: Path, base: Path, dest: Path, limit: int | None, force: bool) -> dict:
    if source.resolve() == dest.resolve():
        raise SystemExit("Refusing to write the migration into the v1 backup.")
    if not source.exists():
        raise SystemExit(f"Missing source database: {source}")
    if not base.exists():
        raise SystemExit(f"Missing working database to clone: {base}")
    if dest.exists() and not force:
        raise SystemExit(f"{dest} already exists. Pass --force to replace it.")

    print(f"Source (read-only): {source}")
    print(f"Schema base:        {base}")
    print(f"Destination:        {dest}")
    started = time.time()
    print("Cloning the working 2.0.18 database...")
    snapshot_database(base, dest)

    src = connect_source(source)
    dst = sqlite3.connect(dest, isolation_level=None)
    dst.row_factory = sqlite3.Row
    dst.execute("PRAGMA foreign_keys = ON")
    dst.execute("PRAGMA synchronous = NORMAL")
    dst.execute("PRAGMA temp_store = MEMORY")
    try:
        projects = load_projects(dst)
        projects_before = set(projects.values())
        rehomed = rehome_global_sessions(dst, projects)
        existing = {row[0] for row in dst.execute("SELECT id FROM session_v2")}
        sessions = src.execute(
            """
            SELECT id, project_id, parent_id, slug, directory, path, title, version,
                   share_url, summary_additions, summary_deletions, summary_files,
                   summary_diffs, metadata, agent, model, time_created, time_updated,
                   time_archived
            FROM session
            ORDER BY time_created, id
            """
        ).fetchall()
        if limit is not None:
            sessions = sessions[:limit]
        print(f"V1 sessions to read: {len(sessions)}")
        print(f"Rehomed global v2 sessions already in the working database: {rehomed}")

        migrated = 0
        skipped = 0
        messages = 0
        warnings = 0
        for index, session in enumerate(sessions, start=1):
            if session["id"] in existing:
                skipped += 1
                continue
            directory = canon(session["directory"]) or "/"
            project_id = ensure_project(dst, projects, directory, session["time_created"])
            message_rows = [
                dict(row)
                for row in src.execute(
                    """
                    SELECT id, session_id, time_created, time_updated, data
                    FROM message WHERE session_id = ?
                    """,
                    (session["id"],),
                )
            ]
            part_rows = [
                dict(row)
                for row in src.execute(
                    """
                    SELECT id, message_id, session_id, time_created, time_updated, data
                    FROM part WHERE session_id = ?
                    """,
                    (session["id"],),
                )
            ]
            try:
                transformed = transform_session(dict(session), message_rows, part_rows)
                dst.execute("BEGIN")
                try:
                    insert_session(dst, dict(session), project_id, directory, transformed)
                    dst.execute("COMMIT")
                except Exception:
                    dst.execute("ROLLBACK")
                    raise
            except Exception as exc:
                warnings += 1
                print(f"  SKIP {session['id']} {session['title'][:70]}: {exc}", flush=True)
                continue
            migrated += 1
            messages += len(transformed["messages"])
            warnings += len(transformed["warnings"])
            if index == 1 or index % 10 == 0 or index == len(sessions):
                print(
                    f"  {index}/{len(sessions)}  {session['title'][:70]}  "
                    f"+{len(transformed['messages'])} messages",
                    flush=True,
                )

        fk = dst.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise SystemExit(f"Foreign key check failed: {fk[:5]}")
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        counts = {
            "session_v2": dst.execute("SELECT COUNT(*) FROM session_v2").fetchone()[0],
            "session_message": dst.execute("SELECT COUNT(*) FROM session_message").fetchone()[0],
            "project": dst.execute("SELECT COUNT(*) FROM project").fetchone()[0],
        }
    finally:
        src.close()
        dst.close()

    report = {
        "source": str(source),
        "destination": str(dest),
        "migrated_sessions": migrated,
        "skipped_existing": skipped,
        "messages": messages,
        "warnings": warnings,
        "rehomed_global_sessions": rehomed,
        "projects_created": len(set(projects.values()) - projects_before),
        "counts": counts,
        "seconds": round(time.time() - started, 1),
    }
    report_path = dest.with_suffix(".report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print()
    print(f"Migrated sessions: {migrated}")
    print(f"Messages written:  {messages}")
    print(f"Warnings:          {warnings}")
    print(f"Projects created:  {report['projects_created']}")
    print(f"Destination totals: {counts}")
    print(f"Report: {report_path}")
    return report


def opencode_is_running() -> list[str]:
    import subprocess

    try:
        output = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, errors="replace")
    except (OSError, subprocess.CalledProcessError):
        return []
    names = ("opencode.exe", "opencode-cli.exe", "opencode2.exe", "OpenCode.exe")
    running = []
    for line in output.splitlines():
        lower = line.lower()
        for name in names:
            if name.lower() in lower:
                running.append(name)
    return sorted(set(running))


def apply_database(migrated: Path, live: Path) -> None:
    if not migrated.exists():
        raise SystemExit(f"Missing migrated database: {migrated}")
    running = opencode_is_running()
    if running:
        raise SystemExit(
            "OpenCode is still running ("
            + ", ".join(running)
            + "). Quit Desktop completely, then run this script again with --apply."
        )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = live.with_name(f"opencode.db.before-v1-apply-{stamp}")
    print(f"Backing up the working database to {backup.name}")
    snapshot_database(live, backup)
    for suffix in ("-wal", "-shm"):
        side = Path(str(live) + suffix)
        if side.exists():
            side.replace(Path(str(backup) + suffix))
    shutil.copy2(migrated, live)
    print(f"Installed {migrated.name} as {live.name}")
    print("Start OpenCode Desktop and open one of the old project folders.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate an OpenCode v1 database into a 2.0.18 database.")
    parser.add_argument("--source", type=Path, default=None, help="Read-only v1 backup. Defaults to opencode.db.pre-v2-fix.")
    parser.add_argument("--base", type=Path, default=ROOT / "opencode.db", help="Working 2.0.18 database used only as a schema clone.")
    parser.add_argument("--dest", type=Path, default=ROOT / "opencode.v1-migrated.db", help="New database to write.")
    parser.add_argument("--limit", type=int, default=None, help="Migrate only the oldest N v1 sessions.")
    parser.add_argument("--force", action="store_true", help="Replace an existing destination file.")
    parser.add_argument("--apply", action="store_true", help="After migrating, replace the live database. OpenCode must be closed.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    source = args.source or default_source()
    migrate(source, args.base, args.dest, args.limit, args.force or args.apply)
    if args.apply:
        apply_database(args.dest, args.base)
    else:
        print()
        print("The live opencode.db was not modified.")
        print("Close OpenCode, then run this script again with --apply to install the migrated database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
