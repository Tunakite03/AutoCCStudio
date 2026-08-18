"""The user's own translation styles: list, save, rename, delete.

Kept off `/api/capabilities` on purpose. Capabilities describe what this install
*can* do and are fetched once at boot; this list changes whenever someone saves
a style, and a client that had to refetch the whole capability probe to see its
own new entry would be refetching model catalogues and an ffmpeg probe with it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import RUNTIME_DIR
from ..core.messages import detail
from ..domain.subtitles.styles import StyleNotFound, StyleRejected, StyleStore, is_valid_style_id
from ..domain.translation.style import STYLE_AUTO

router = APIRouter(prefix="/api/styles", tags=["styles"])

store = StyleStore(RUNTIME_DIR / "styles.json")


class StylePayload(BaseModel):
    name: str
    base: str = STYLE_AUTO
    notes: str = ""


class StylePatch(BaseModel):
    """Every field optional: renaming a style must not require resending it."""

    name: str | None = None
    base: str | None = None
    notes: str | None = None


def _resolve(style_id: str) -> str:
    """Reject an id that could never name a style before touching the store."""

    if not is_valid_style_id(style_id):
        raise HTTPException(status_code=404, detail=detail("err.style.notFound"))
    return style_id


@router.get("")
def list_styles() -> dict:
    return {"styles": store.list()}


@router.post("", status_code=201)
def create_style(payload: StylePayload) -> dict:
    try:
        return store.create(name=payload.name, base=payload.base, notes=payload.notes)
    except StyleRejected as exc:
        raise HTTPException(status_code=400, detail=detail(exc.message)) from exc


@router.patch("/{style_id}")
def update_style(style_id: str, payload: StylePatch) -> dict:
    try:
        return store.update(
            _resolve(style_id),
            name=payload.name,
            base=payload.base,
            notes=payload.notes,
        )
    except StyleNotFound as exc:
        raise HTTPException(status_code=404, detail=detail("err.style.notFound")) from exc
    except StyleRejected as exc:
        raise HTTPException(status_code=400, detail=detail(exc.message)) from exc


@router.delete("/{style_id}", status_code=204)
def delete_style(style_id: str) -> None:
    try:
        store.delete(_resolve(style_id))
    except StyleNotFound as exc:
        raise HTTPException(status_code=404, detail=detail("err.style.notFound")) from exc
    except StyleRejected as exc:
        raise HTTPException(status_code=400, detail=detail(exc.message)) from exc
