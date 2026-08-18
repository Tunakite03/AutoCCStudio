import json

import pytest
from fastapi.testclient import TestClient

import backend.api.styles as styles_api
from backend.app import app
from backend.styles import StyleNotFound, StyleRejected, StyleStore

client = TestClient(app)


@pytest.fixture
def store(tmp_path):
    return StyleStore(tmp_path / "styles.json")


@pytest.fixture
def api(tmp_path, monkeypatch):
    """The routes, writing to a throwaway file instead of runtime/styles.json."""

    monkeypatch.setattr(styles_api, "store", StyleStore(tmp_path / "styles.json"))
    return client


def test_a_saved_style_is_a_name_a_preset_and_the_rules(store):
    style = store.create(name="  Phim  Thái ", base="genz", notes=" ครับ → dạ ")

    # The name is squeezed to one line: it has to fit an <option>.
    assert style["name"] == "Phim Thái"
    assert style["base"] == "genz"
    assert style["notes"] == "ครับ → dạ"
    assert store.get(style["id"]) == style


def test_styles_survive_a_restart(store, tmp_path):
    store.create(name="Phim Thái", base="neutral", notes="a → b")

    reopened = StyleStore(tmp_path / "styles.json")
    assert [style["name"] for style in reopened.list()] == ["Phim Thái"]
    assert reopened.list()[0]["notes"] == "a → b"


def test_the_list_is_ordered_by_name_not_by_when_it_was_saved(store):
    for name in ("Zulu", "anime", "Bolero"):
        store.create(name=name, base="auto", notes="")

    assert [style["name"] for style in store.list()] == ["anime", "Bolero", "Zulu"]


def test_two_styles_may_not_share_a_name(store):
    store.create(name="Phim Thái", base="auto", notes="")

    # Case-insensitively: the picker would show two identical-looking entries.
    with pytest.raises(StyleRejected) as raised:
        store.create(name="phim thái", base="auto", notes="")
    assert raised.value.code == "err.style.nameTaken"


def test_a_style_keeps_the_fields_an_update_does_not_mention(store):
    style = store.create(name="Phim Thái", base="genz", notes="a → b")

    renamed = store.update(style["id"], name="Phim Thái Lan")
    assert renamed["name"] == "Phim Thái Lan"
    assert (renamed["base"], renamed["notes"]) == ("genz", "a → b")
    # Renaming to the name it already has is not a duplicate of itself.
    assert store.update(style["id"], name="Phim Thái Lan")["name"] == "Phim Thái Lan"


def test_a_style_is_rejected_rather_than_stored_unusable(store):
    with pytest.raises(StyleRejected) as raised:
        store.create(name="   ", base="auto", notes="")
    assert raised.value.code == "err.style.nameMissing"

    with pytest.raises(StyleRejected) as raised:
        store.create(name="x", base="not-a-preset", notes="")
    assert raised.value.code == "err.style.badBase"

    # A saved style is applied *as* the per-job notes, so it has to fit in them.
    with pytest.raises(StyleRejected) as raised:
        store.create(name="x", base="auto", notes="y" * 2001)
    assert raised.value.code == "err.style.notesTooLong"


def test_deleting_a_style_removes_it_for_good(store):
    style = store.create(name="Phim Thái", base="auto", notes="")

    store.delete(style["id"])
    assert store.list() == []
    with pytest.raises(StyleNotFound):
        store.get(style["id"])
    with pytest.raises(StyleNotFound):
        store.delete(style["id"])


def test_one_corrupt_record_does_not_cost_the_whole_catalogue(tmp_path):
    """A hand-edited file must degrade to what is still readable."""

    path = tmp_path / "styles.json"
    path.write_text(
        json.dumps(
            [
                {"id": "aaaa1111", "name": "Good", "base": "genz", "notes": "a → b"},
                {"id": "bbbb2222", "name": "", "base": "auto", "notes": ""},
                {"id": "../escape", "name": "Bad id", "base": "auto", "notes": ""},
                "not even a record",
            ]
        ),
        encoding="utf-8",
    )

    assert [style["name"] for style in StyleStore(path).list()] == ["Good"]


def test_an_unreadable_file_leaves_the_app_translating_with_presets(tmp_path):
    path = tmp_path / "styles.json"
    path.write_text("{ not json", encoding="utf-8")

    assert StyleStore(path).list() == []


# ── Routes ───────────────────────────────────────────────────────────


def test_the_styles_route_round_trips_a_style(api):
    assert api.get("/api/styles").json() == {"styles": []}

    created = api.post(
        "/api/styles", json={"name": "Phim Thái", "base": "genz", "notes": "a → b"}
    )
    assert created.status_code == 201
    style_id = created.json()["id"]

    assert api.get("/api/styles").json()["styles"][0]["name"] == "Phim Thái"

    patched = api.patch(f"/api/styles/{style_id}", json={"notes": "c → d"})
    assert patched.status_code == 200
    assert patched.json()["notes"] == "c → d"
    assert patched.json()["base"] == "genz"

    assert api.delete(f"/api/styles/{style_id}").status_code == 204
    assert api.get("/api/styles").json() == {"styles": []}


def test_a_rejected_style_comes_back_as_a_code_the_client_can_render(api):
    response = api.post("/api/styles", json={"name": "", "base": "auto", "notes": ""})

    assert response.status_code == 400
    assert response.json()["detail"] == {"code": "err.style.nameMissing", "params": {}}


def test_a_style_that_is_not_there_is_a_404_whatever_the_id_looks_like(api):
    assert api.patch("/api/styles/deadbeef", json={"name": "x"}).status_code == 404
    assert api.delete("/api/styles/deadbeef").status_code == 404
    # An id that could never name a style is refused before the store is touched.
    assert api.delete("/api/styles/not.an.id").status_code == 404
