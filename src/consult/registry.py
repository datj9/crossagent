"""Named, resumable consultation sessions.

Stores each consultation's underlying session id keyed by ``advisor:slug`` so a
later turn on the same decision can resume the same conversation. Only advisors
that emit a session id (currently Claude) populate this; the file is otherwise a
harmless no-op.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = Path.home() / ".config" / "consult" / "sessions.json"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "consultation"


def session_key(advisor: str, name: str | None) -> str:
    return f"{advisor}:{slugify(name)}" if name else ""


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + ".corrupt")
        path.replace(backup)
        print(f"[consult] Registry was invalid JSON; moved to {backup}", file=sys.stderr)
        return {"sessions": {}}
    if not isinstance(data, dict):
        return {"sessions": {}}
    if not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stored_session_id(registry: dict[str, Any], key: str) -> str | None:
    entry = registry.get("sessions", {}).get(key) if key else None
    return entry.get("session_id") if isinstance(entry, dict) else None


def record(path: Path, registry: dict[str, Any], key: str, *, session_id: str, name: str | None,
           cwd: str, advisor: str, model: str) -> dict[str, Any]:
    """Return a NEW registry dict with the session recorded, and persist it."""
    sessions = dict(registry.get("sessions", {}))
    sessions[key] = {
        "session_id": session_id,
        "name": name,
        "advisor": advisor,
        "cwd": str(Path(cwd).resolve()),
        "model": model,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = {**registry, "sessions": sessions}
    save(path, updated)
    return updated
