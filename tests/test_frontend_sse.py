from pathlib import Path


FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


def frontend_source() -> str:
    """The frontend is split across ES modules, so assert against all of them."""

    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(FRONTEND_DIR.rglob("*.js"))
    )


def test_frontend_uses_event_source_without_short_polling():
    source = frontend_source()
    assert "new EventSource(" in source
    assert "/events`" in source
    assert "pollTimer" not in source
    assert "setTimeout(poll" not in source
    assert "waitForJob" not in source


def test_frontend_sends_dynamic_transcription_model():
    source = frontend_source()
    assert 'form.append("model", $("#transcription-model").value)' in source
    assert "transcription_models" in source
    assert '$("#transcription-model-label").textContent' in source


def test_frontend_can_reanalyze_existing_cues_without_transcribing_again():
    source = frontend_source()
    assert "/analyze-speakers" in source
    assert '$("#reanalyze-speakers-btn").addEventListener' in source


def test_router_screen_lookup_cannot_match_the_document_element():
    """<html> carries the active-screen marker for CSS. A bare [data-screen]
    query would match it first and hide every real screen — a blank app."""

    source = (FRONTEND_DIR / "core" / "router.js").read_text(encoding="utf-8")
    assert '$$(".screens > [data-screen]")' in source
    assert "documentElement.dataset.screen" not in source
    assert "documentElement.dataset.activeScreen" in source
