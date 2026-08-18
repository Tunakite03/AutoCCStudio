"""Translation styles written by the person using the app, saved on the server.

A preset in `translation_style.py` is code: rules in English, a pinned glossary,
and a `label_code` the client translates. What is saved here is the other half —
a name someone typed, the preset it starts from, and the very same free-text
rules the "house rules" box already accepts.

The two are deliberately kept apart. A saved style is *stored* here but
*resolved* nowhere: picking one only fills the base preset and the notes box the
translator already understands, so nothing in the prompt path has to learn a
second kind of style, and deleting a style can never break a project that was
translated with it — the project keeps the base and the notes it actually ran.

The file is a single JSON document rather than a directory of them: this is a
handful of short records read on every page load, and one atomic write is
cheaper to reason about than a tree.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .config import get_logger
from .messages import CodedError
from .translation_style import STYLE_AUTO, STYLE_NOTES_LIMIT, STYLES

logger = get_logger("styles")

NAME_LIMIT = 60
# Not a quota anyone should reach — a guard so a runaway client cannot grow the
# file until reading it stalls the page load.
STYLE_LIMIT = 200


class StyleNotFound(LookupError):
    """No saved style with this id."""


class StyleRejected(CodedError):
    """The record is not one we are willing to store."""


def is_valid_style_id(style_id: str) -> bool:
    return bool(style_id) and len(style_id) <= 40 and style_id.isalnum()


def _clean_name(name: str) -> str:
    # A name is one line: it goes in an <option>, where a newline would simply
    # vanish and leave two styles looking identically named.
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise StyleRejected("err.style.nameMissing")
    if len(cleaned) > NAME_LIMIT:
        raise StyleRejected("err.style.nameTooLong", limit=NAME_LIMIT)
    return cleaned


def _clean_notes(notes: str) -> str:
    cleaned = str(notes or "").strip()
    if len(cleaned) > STYLE_NOTES_LIMIT:
        raise StyleRejected("err.style.notesTooLong", limit=STYLE_NOTES_LIMIT)
    return cleaned


def _clean_base(base: str) -> str:
    cleaned = str(base or STYLE_AUTO).strip().lower() or STYLE_AUTO
    if cleaned != STYLE_AUTO and cleaned not in STYLES:
        raise StyleRejected("err.style.badBase")
    return cleaned


class StyleStore:
    """The saved styles, kept in memory and mirrored to one JSON file.

    Every method holds the lock: the list is read by every page load and
    rewritten whole on each change, so two concurrent edits must not interleave
    into a file that has lost one of them.
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._styles: dict[str, dict] | None = None

    # ── Storage ──────────────────────────────────────────────────────

    def _loaded(self) -> dict[str, dict]:
        """The records, read from disk once. Callers must hold the lock."""

        if self._styles is not None:
            return self._styles

        styles: dict[str, dict] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = []
        except (OSError, json.JSONDecodeError) as exc:
            # An unreadable file must not take the whole app down with it: the
            # picker falls back to presets only, which still translates.
            logger.warning("styles: %s unreadable, starting empty: %s", self._path, exc)
            raw = []

        for record in raw if isinstance(raw, list) else []:
            if not isinstance(record, dict):
                continue
            style_id = str(record.get("id") or "")
            if not is_valid_style_id(style_id) or style_id in styles:
                continue
            try:
                styles[style_id] = {
                    "id": style_id,
                    "name": _clean_name(record.get("name", "")),
                    "base": _clean_base(record.get("base", STYLE_AUTO)),
                    "notes": _clean_notes(record.get("notes", "")),
                    "created_at": float(record.get("created_at") or 0.0),
                    "updated_at": float(record.get("updated_at") or 0.0),
                }
            except (StyleRejected, TypeError, ValueError) as exc:
                # One bad row is dropped; the rest of the catalogue survives.
                logger.warning("styles: dropped %s: %s", style_id, exc)

        self._styles = styles
        return styles

    def _persist(self) -> None:
        """Write via a sibling temp file so a crash cannot truncate the list."""

        payload = json.dumps(
            list((self._styles or {}).values()), ensure_ascii=False, indent=2
        )
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self._path)
        except OSError as exc:
            # In-memory stays authoritative for this process; the user is told,
            # because a style they believe is saved would be gone on restart.
            logger.warning("styles: could not persist %s: %s", self._path, exc)
            raise StyleRejected("err.style.notSaved") from exc

    # ── Reading ──────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        """Every saved style, by name — the order the picker shows them in."""

        with self._lock:
            return sorted(
                (dict(style) for style in self._loaded().values()),
                key=lambda style: style["name"].lower(),
            )

    def get(self, style_id: str) -> dict:
        with self._lock:
            style = self._loaded().get(style_id)
            if style is None:
                raise StyleNotFound(style_id)
            return dict(style)

    # ── Writing ──────────────────────────────────────────────────────

    def _reject_duplicate_name(self, name: str, *, ignoring: str = "") -> None:
        """Two styles with one name are indistinguishable in the picker."""

        lowered = name.lower()
        for style in self._loaded().values():
            if style["id"] != ignoring and style["name"].lower() == lowered:
                raise StyleRejected("err.style.nameTaken", name=name)

    def create(self, *, name: str, base: str, notes: str) -> dict:
        name, base, notes = _clean_name(name), _clean_base(base), _clean_notes(notes)
        with self._lock:
            styles = self._loaded()
            if len(styles) >= STYLE_LIMIT:
                raise StyleRejected("err.style.tooMany", limit=STYLE_LIMIT)
            self._reject_duplicate_name(name)
            now = time.time()
            style = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "base": base,
                "notes": notes,
                "created_at": now,
                "updated_at": now,
            }
            styles[style["id"]] = style
            self._persist()
            return dict(style)

    def update(self, style_id: str, **changes) -> dict:
        """Replace the fields given; anything omitted keeps its current value."""

        with self._lock:
            styles = self._loaded()
            style = styles.get(style_id)
            if style is None:
                raise StyleNotFound(style_id)

            updated = dict(style)
            if changes.get("name") is not None:
                updated["name"] = _clean_name(changes["name"])
                self._reject_duplicate_name(updated["name"], ignoring=style_id)
            if changes.get("base") is not None:
                updated["base"] = _clean_base(changes["base"])
            if changes.get("notes") is not None:
                updated["notes"] = _clean_notes(changes["notes"])
            updated["updated_at"] = time.time()

            styles[style_id] = updated
            self._persist()
            return dict(updated)

    def delete(self, style_id: str) -> None:
        with self._lock:
            styles = self._loaded()
            if styles.pop(style_id, None) is None:
                raise StyleNotFound(style_id)
            self._persist()
