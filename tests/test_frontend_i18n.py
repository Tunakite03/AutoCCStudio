"""Guards for the interface catalogue.

The codes are a contract between three places: the backend emits them, the markup
references them, and the modules ask for them by name. Nothing fails loudly when
one of the three drifts — a missing key renders as itself — so the drift is what
these tests look for.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"

# A catalogue key: dotted, lowerCamel segments. Matches the codes the backend
# emits as well, because they are the same names.
KEY = re.compile(r"^[a-z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+$")

# Persisted-state keys share the shape of a catalogue key and are not one.
NOT_KEYS = ("autocc.",)


def catalogue(name: str) -> dict[str, str]:
    """The keys of frontend/i18n/<name>.js, read without running JavaScript."""

    text = (FRONTEND / "i18n" / f"{name}.js").read_text(encoding="utf-8")
    return dict.fromkeys(re.findall(r'^\s{2}"([^"]+)":', text, flags=re.MULTILINE), "")


def frontend_modules() -> list[Path]:
    return [path for path in FRONTEND.rglob("*.js") if path.parent.name != "i18n"]


def keys_asked_for(source: str) -> set[str]:
    """Every key a module passes to t() or names in a `{code: ...}` literal.

    The whole argument list is scanned rather than just the first token, so a
    `t(flag ? "a" : "b")` contributes both of its branches.
    """

    found: set[str] = set()
    for arguments in re.findall(r"\bt\(([^;]*?)\)", source, flags=re.DOTALL):
        found.update(re.findall(r"""["']([^"']+)["']""", arguments))
    found.update(re.findall(r"""code:\s*["']([^"']+)["']""", source))
    return {key for key in found if KEY.match(key) and not key.startswith(NOT_KEYS)}


def test_english_mirrors_every_vietnamese_key():
    """A gap here shows a Vietnamese sentence to an English reader, silently."""

    vi, en = catalogue("vi"), catalogue("en")
    assert set(en) == set(vi), {
        "missing_in_en": sorted(set(vi) - set(en)),
        "only_in_en": sorted(set(en) - set(vi)),
    }


def test_no_key_is_defined_twice():
    """A duplicate literal key is legal JavaScript and silently wins."""

    for name in ("vi", "en"):
        text = (FRONTEND / "i18n" / f"{name}.js").read_text(encoding="utf-8")
        keys = re.findall(r'^\s{2}"([^"]+)":', text, flags=re.MULTILINE)
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        assert not duplicates, f"{name}.js defines twice: {duplicates}"


def test_every_code_the_backend_emits_has_an_entry():
    """The backend writes no prose, so an unlisted code reaches the user raw."""

    emitted: set[str] = set()
    for path in BACKEND.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for literal in re.findall(r'"([a-z][A-Za-z0-9.]+)"', source):
            if KEY.match(literal) and literal.split(".")[0] in {
                "err",
                "progress",
                "op",
                "style",
                "model",
            }:
                emitted.add(literal)

    assert emitted, "no codes found — the scan is looking in the wrong place"
    missing = sorted(emitted - set(catalogue("vi")))
    assert not missing, f"backend codes with no catalogue entry: {missing}"


def test_every_key_the_markup_references_has_an_entry():
    markup = (FRONTEND / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', markup))
    for group in re.findall(r'data-i18n-attr="([^"]+)"', markup):
        for pair in group.split(";"):
            _name, _, key = pair.partition(":")
            if key:
                referenced.add(key.strip())

    missing = sorted(referenced - set(catalogue("vi")))
    assert not missing, f"markup references missing keys: {missing}"


def test_every_key_the_modules_ask_for_has_an_entry():
    vi = set(catalogue("vi"))
    missing = {}
    for path in frontend_modules():
        gaps = sorted(keys_asked_for(path.read_text(encoding="utf-8")) - vi)
        if gaps:
            missing[path.relative_to(FRONTEND).as_posix()] = gaps
    assert not missing, missing
