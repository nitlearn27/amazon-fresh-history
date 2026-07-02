"""
Persistent store for skills the agent learns while self-healing.

A skill is DATA, never code — a small JSON recipe the automation replays
before asking the LLM again. Two kinds:

- "action":   click/wait steps that recover a failed interaction (e.g. a
              product card whose Add button is really a size-variant control).
              Applies when its `marker_selector` exists on the current page.
- "selector": an alternative CSS selector for a named slot (e.g.
              "search.result_card") tried when the built-in selector finds
              nothing.

Skills live in skills.json (SKILLS_PATH env override). Like auth_state.json,
the file is durable locally but ephemeral on cloud hosts — a valuable learned
skill can be committed to the repo. Only one scrape/cart/search run is in
flight at a time, so plain file I/O with a process-wide lock is sufficient.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def _skills_path() -> Path:
    return Path(os.getenv("SKILLS_PATH") or "skills.json")


def _load() -> list[dict]:
    path = _skills_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"[skills] Could not read {path} ({exc}); starting empty.")
        return []


def _save(skills: list[dict]) -> None:
    path = _skills_path()
    try:
        path.write_text(json.dumps(skills, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[skills] Could not write {path}: {exc}")


def all_skills() -> list[dict]:
    with _LOCK:
        return _load()


def applicable_action_skills(context: str) -> list[dict]:
    """Learned action skills for a hook context, most-used first."""
    with _LOCK:
        skills = [s for s in _load() if s.get("kind") == "action" and s.get("context") == context]
    return sorted(skills, key=lambda s: s.get("hits", 0), reverse=True)


def selector_overrides(slot: str) -> list[dict]:
    """Learned selector overrides for a named selector slot, most-used first."""
    with _LOCK:
        skills = [s for s in _load() if s.get("kind") == "selector" and s.get("slot") == slot]
    return sorted(skills, key=lambda s: s.get("hits", 0), reverse=True)


def add_skill(skill: dict) -> dict:
    """Persist a new skill; fills id/metadata. Returns the stored record."""
    skill = dict(skill)
    skill["id"] = uuid.uuid4().hex[:12]
    skill["created_at"] = datetime.now(timezone.utc).isoformat()
    skill["hits"] = 0
    skill["last_used_at"] = None
    with _LOCK:
        skills = _load()
        skills.append(skill)
        _save(skills)
    print(f"[skills] Learned new {skill.get('kind')} skill {skill['id']}: {skill.get('description', '')[:80]}")
    return skill


def record_hit(skill_id: str) -> None:
    with _LOCK:
        skills = _load()
        for s in skills:
            if s.get("id") == skill_id:
                s["hits"] = int(s.get("hits", 0)) + 1
                s["last_used_at"] = datetime.now(timezone.utc).isoformat()
                break
        _save(skills)


def delete_skill(skill_id: str) -> bool:
    with _LOCK:
        skills = _load()
        remaining = [s for s in skills if s.get("id") != skill_id]
        if len(remaining) == len(skills):
            return False
        _save(remaining)
    return True
