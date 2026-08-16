import json
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import backend.ai as ai


def test_transcription_clamps_segments_to_media_duration(monkeypatch):
    class FakeModel:
        def transcribe(self, *_args, **_kwargs):
            return iter(
                [
                    SimpleNamespace(start=42.56, end=55.84, text=" Hold on tight "),
                    SimpleNamespace(start=50.0, end=52.0, text=" Outside media "),
                ]
            ), SimpleNamespace(language="en")

    monkeypatch.setattr(ai, "_whisper_model", lambda *_args: FakeModel())
    monkeypatch.setattr(ai, "media_duration_seconds", lambda _path: 46.09161)

    cues, language = ai.transcribe_video(
        Path("test.mp4"),
        model_size="tiny",
        provider="faster_whisper",
    )

    assert language == "en"
    assert cues == [
        {
            "id": 1,
            "start": 42.56,
            "end": 46.092,
            "text": "Hold on tight",
            "translation": "",
        }
    ]


def test_deepgram_transcription_uses_diarized_utterances(monkeypatch):
    captured = {}
    payload = {
        "results": {
            "channels": [{"detected_language": "en", "alternatives": []}],
            "utterances": [
                {"start": 0.2, "end": 1.4, "speaker": 0, "transcript": "Good morning."},
                {"start": 1.5, "end": 8.0, "speaker": 1, "transcript": "Hello."},
            ],
        }
    }

    def fake_request(path, params, on_progress=None):
        captured["path"] = path
        captured["params"] = params
        return payload

    monkeypatch.setattr(ai, "_request_deepgram", fake_request)
    monkeypatch.setattr(ai, "media_duration_seconds", lambda _path: 5.0)

    cues, language = ai.transcribe_video(
        Path("conversation.mp4"),
        provider="deepgram",
        model_size="nova-2-meeting",
    )

    assert language == "en"
    assert captured["params"]["model"] == "nova-2-meeting"
    assert captured["params"]["diarize_model"] == "latest"
    assert captured["params"]["utterances"] == "true"
    assert captured["params"]["detect_language"] == "true"
    assert [cue["text"] for cue in cues] == ["Good morning.", "Hello."]
    assert [cue["speaker"] for cue in cues] == [0, 1]
    assert cues[-1]["end"] == 5.0


def test_deepgram_uses_word_level_speakers_inside_one_utterance():
    payload = {
        "results": {
            "utterances": [
                {
                    "start": 0.0,
                    "end": 3.0,
                    "speaker": 0,
                    "transcript": "How much? The fare is $5.50.",
                    "words": [
                        {
                            "word": "How",
                            "punctuated_word": "How",
                            "speaker": 0,
                            "speaker_confidence": 0.94,
                        },
                        {
                            "word": "much",
                            "punctuated_word": "much?",
                            "speaker": 0,
                            "speaker_confidence": 0.92,
                        },
                        {
                            "word": "The",
                            "punctuated_word": "The",
                            "speaker": 1,
                            "speaker_confidence": 0.9,
                        },
                        {
                            "word": "fare",
                            "punctuated_word": "fare",
                            "speaker": 1,
                            "speaker_confidence": 0.88,
                        },
                        {
                            "word": "is",
                            "punctuated_word": "is",
                            "speaker": 1,
                            "speaker_confidence": 0.91,
                        },
                        {
                            "word": "five fifty",
                            "punctuated_word": "$5.50.",
                            "speaker": 1,
                            "speaker_confidence": 0.89,
                        },
                    ],
                }
            ]
        }
    }

    cues = ai._deepgram_cues(payload, media_duration=5.0)

    assert len(cues) == 2
    assert cues[0]["text"] == "How much?"
    assert cues[0]["speaker"] == 0
    assert cues[1]["text"] == "The fare is $5.50."
    assert cues[1]["speaker"] == 1
    assert cues[0]["speaker_analysis_source"] == "deepgram_words"


def test_deepgram_segments_long_utterances_into_short_cues():
    words = [
        {"word": "How's", "punctuated_word": "How's", "start": 0.0, "end": 0.4},
        {"word": "everything", "punctuated_word": "everything", "start": 0.4, "end": 0.9},
        {"word": "coming", "punctuated_word": "coming", "start": 0.9, "end": 1.2},
        {"word": "along", "punctuated_word": "along?", "start": 1.2, "end": 1.7},
        {"word": "The", "punctuated_word": "The", "start": 1.9, "end": 2.1},
        {"word": "Shogun", "punctuated_word": "Shogun", "start": 2.1, "end": 2.5},
        {"word": "is", "punctuated_word": "is", "start": 2.5, "end": 2.7},
        {"word": "coming", "punctuated_word": "coming", "start": 2.7, "end": 3.0},
        {"word": "to", "punctuated_word": "to", "start": 3.0, "end": 3.2},
        {"word": "visit", "punctuated_word": "visit", "start": 3.2, "end": 3.5},
        {"word": "in", "punctuated_word": "in", "start": 3.5, "end": 3.7},
        {"word": "one", "punctuated_word": "one", "start": 3.7, "end": 3.9},
        {"word": "week", "punctuated_word": "week.", "start": 3.9, "end": 4.4},
        {"word": "Everyone", "punctuated_word": "Everyone", "start": 4.6, "end": 5.0},
        {"word": "knows", "punctuated_word": "knows", "start": 5.0, "end": 5.3},
        {"word": "he's", "punctuated_word": "he's", "start": 5.3, "end": 5.6},
        {"word": "trying", "punctuated_word": "trying", "start": 5.6, "end": 5.9},
        {"word": "to", "punctuated_word": "to", "start": 5.9, "end": 6.1},
        {"word": "decide", "punctuated_word": "decide", "start": 6.1, "end": 6.5},
        {"word": "who", "punctuated_word": "who", "start": 6.5, "end": 6.7},
        {"word": "his", "punctuated_word": "his", "start": 6.7, "end": 6.9},
        {"word": "success", "punctuated_word": "success", "start": 6.9, "end": 7.3},
        {"word": "will", "punctuated_word": "will", "start": 7.3, "end": 7.5},
        {"word": "be", "punctuated_word": "be.", "start": 7.5, "end": 8.0},
    ]
    payload = {
        "results": {
            "utterances": [
                {
                    "start": 0.0,
                    "end": 8.0,
                    "speaker": 0,
                    "transcript": "How's everything coming along? The Shogun is coming to visit in one week. Everyone knows he's trying to decide who his success will be.",
                    "words": words,
                }
            ]
        }
    }

    cues = ai._deepgram_cues(payload, media_duration=10.0)

    assert len(cues) == 3
    assert cues[0]["text"] == "How's everything coming along?"
    assert cues[0]["start"] == 0.0
    assert cues[0]["end"] == 1.7
    assert cues[1]["text"].replace("\n", " ") == "The Shogun is coming to visit in one week."
    assert cues[1]["start"] == 1.9
    assert cues[1]["end"] == 4.4
    # All cues must have at most 2 lines and be short
    for cue in cues:
        assert cue["text"].count("\n") <= 1
        assert len(cue["text"]) <= 75
        assert (cue["end"] - cue["start"]) <= 6.0



def test_deepgram_explicit_language_disables_detection(monkeypatch):
    captured = {}

    payload = {
        "results": {
            "channels": [{"alternatives": []}],
            "utterances": [
                {"start": 0.0, "end": 1.0, "speaker": 0, "transcript": "Xin chào."}
            ],
        }
    }
    monkeypatch.setattr(
        ai,
        "_request_deepgram",
        lambda _path, params, on_progress=None: captured.update(params) or payload,
    )
    monkeypatch.setattr(ai, "media_duration_seconds", lambda _path: 2.0)

    _, language = ai.transcribe_video(
        Path("conversation.mp4"), provider="deepgram", language="vi"
    )

    assert language == "vi"
    assert captured["language"] == "vi"
    assert "detect_language" not in captured


def test_deepgram_multilingual_language_option(monkeypatch):
    captured = {}

    payload = {
        "results": {
            "channels": [{"alternatives": []}],
            "utterances": [
                {"start": 0.0, "end": 1.0, "speaker": 0, "transcript": "Hello and xin chào."}
            ],
        }
    }
    monkeypatch.setattr(
        ai,
        "_request_deepgram",
        lambda _path, params, on_progress=None: captured.update(params) or payload,
    )
    monkeypatch.setattr(ai, "media_duration_seconds", lambda _path: 2.0)

    _, language = ai.transcribe_video(
        Path("conversation.mp4"), provider="deepgram", language="multi"
    )

    assert language == "multi"
    assert captured["language"] == "multi"
    assert "detect_language" not in captured


def test_whisper_normalizes_regional_language_code(monkeypatch):
    received_kwargs = {}

    class FakeModel:
        def transcribe(self, *_args, **kwargs):
            received_kwargs.update(kwargs)
            return iter([SimpleNamespace(start=0.0, end=1.0, text="Test")]), SimpleNamespace(language="en")

    monkeypatch.setattr(ai, "_whisper_model", lambda *_args: FakeModel())
    monkeypatch.setattr(ai, "media_duration_seconds", lambda _path: 2.0)

    ai.transcribe_video(Path("test.mp4"), provider="faster_whisper", language="en-US")
    assert received_kwargs["language"] == "en"

    ai.transcribe_video(Path("test.mp4"), provider="faster_whisper", language="multi")
    assert received_kwargs["language"] is None


def test_deepgram_http_request_streams_media_and_auth(monkeypatch, tmp_path):
    captured = {}

    class DeepgramStubHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["authorization"] = self.headers.get("Authorization")
            captured["content_type"] = self.headers.get("Content-Type")
            captured["body"] = self.rfile.read(length)
            body = b'{"results":{"channels":[],"utterances":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), DeepgramStubHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    media_path = tmp_path / "sample.mp4"
    media_path.write_bytes(b"fake-mp4-payload")
    monkeypatch.setattr(
        ai,
        "settings",
        replace(
            ai.settings,
            deepgram_api_key="integration-key",
            deepgram_base_url=f"http://127.0.0.1:{server.server_port}",
        ),
    )

    try:
        payload = ai._request_deepgram(
            media_path,
            {
                "model": "nova-3",
                "utterances": "true",
                "diarize_model": "latest",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    query = parse_qs(urlparse(captured["path"]).query)
    assert payload["results"]["utterances"] == []
    assert captured["authorization"] == "Token integration-key"
    assert captured["content_type"] == "video/mp4"
    assert captured["body"] == b"fake-mp4-payload"
    assert query == {
        "model": ["nova-3"],
        "utterances": ["true"],
        "diarize_model": ["latest"],
    }


def test_deepgram_http_request_extracts_audio_and_cleans_up(monkeypatch, tmp_path):
    captured = {}

    class DeepgramStubHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            captured["authorization"] = self.headers.get("Authorization")
            captured["content_type"] = self.headers.get("Content-Type")
            captured["body"] = self.rfile.read(length)
            body = b'{"results":{"channels":[],"utterances":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), DeepgramStubHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    audio_file = tmp_path / "extracted.m4a"
    audio_file.write_bytes(b"extracted-m4a-audio-data")

    monkeypatch.setattr(ai, "extract_transcription_audio", lambda _path: audio_file)
    monkeypatch.setattr(
        ai,
        "settings",
        replace(
            ai.settings,
            deepgram_api_key="test-key",
            deepgram_base_url=f"http://127.0.0.1:{server.server_port}",
        ),
    )

    try:
        payload = ai._request_deepgram(
            tmp_path / "huge_movie.mp4",
            {"model": "nova-3"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert payload["results"]["utterances"] == []
    assert captured["authorization"] == "Token test-key"
    assert "audio" in captured["content_type"]
    assert captured["body"] == b"extracted-m4a-audio-data"
    # Audio file should have been cleaned up
    assert not audio_file.exists()



def test_mock_translation_preserves_timing_and_batch_order(monkeypatch):
    monkeypatch.setattr(
        ai,
        "settings",
        replace(ai.settings, translation_provider="mock"),
    )
    cues = [
        {"id": 1, "start": 0, "end": 1, "text": "Hello", "translation": ""},
        {"id": 2, "start": 1, "end": 2, "text": "World", "translation": ""},
    ]
    result = ai.translate_cues(cues, "Tiếng Việt", batch_size=1)
    assert [cue["translation"] for cue in result] == [
        "[Tiếng Việt] Hello",
        "[Tiếng Việt] World",
    ]
    assert [(cue["start"], cue["end"]) for cue in result] == [(0, 1), (1, 2)]


def test_translation_preserves_dialogue_lines_and_speaker_metadata(monkeypatch):
    monkeypatch.setattr(
        ai,
        "settings",
        replace(ai.settings, translation_provider="mock"),
    )
    cues = [
        {
            "id": 1,
            "start": 0,
            "end": 1,
            "text": "[S2] Hello\nHow are you?",
            "translation": "",
            "speaker": 1,
        }
    ]

    result = ai.translate_cues(cues, "Tiếng Việt")

    assert result[0]["text"] == "Hello\nHow are you?"
    assert result[0]["translation"] == "[Tiếng Việt] Hello\n[Tiếng Việt] How are you?"
    assert result[0]["speaker"] == 1


def _hosted_translation(monkeypatch, completion):
    monkeypatch.setattr(
        ai,
        "settings",
        replace(
            ai.settings,
            translation_provider="mistral",
            llm_base_url="http://127.0.0.1:1/v1",
            llm_model="translate-model",
        ),
    )
    monkeypatch.setattr(ai, "_llm_completion", completion)


def _request_payload(messages):
    """The JSON block a translation request ends with, after the instructions."""

    return json.loads(messages[1]["content"].rsplit("\n\n", 1)[-1])


def _requested_lines(messages):
    """The source lines of one translation request, in the order they were sent."""

    return [item["text"] for item in _request_payload(messages)["lines"].values()]


def test_each_batch_sees_its_neighbours_and_the_terms_already_agreed(monkeypatch):
    """Twenty isolated lines at a time is how a film loses its pronouns."""

    seen = []

    def fake_completion(messages, *, temperature, operation, model=None):
        payload = _request_payload(messages)
        seen.append(payload)
        return json.dumps(
            {
                "translations": {
                    key: f"VI:{item['text']}" for key, item in payload["lines"].items()
                },
                "glossary": {"Anna": "Anna (chị)"},
            },
            ensure_ascii=False,
        )

    _hosted_translation(monkeypatch, fake_completion)
    cues = [
        {
            "id": index,
            "start": index,
            "end": index + 1,
            "text": f"line {index}",
            "translation": "",
            "speaker": index % 2,
        }
        for index in range(1, 5)
    ]

    ai.translate_cues(cues, "Tiếng Việt", batch_size=2)

    assert len(seen) == 2
    first, second = seen
    assert "context_before" not in first  # nothing precedes the opening lines
    assert "glossary" not in first
    assert [item["text"] for item in first["context_after"]] == ["line 3", "line 4"]
    assert first["lines"]["1"]["speaker"] == 1
    # The second batch reads what the first one actually produced, not its source.
    assert second["context_before"][-1] == {
        "text": "line 2",
        "speaker": 0,
        "translation": "VI:line 2",
    }
    assert second["glossary"] == {"Anna": "Anna (chị)"}


def test_style_terms_are_pinned_into_every_batch(monkeypatch):
    """"đại ca" is the whole point: a correct "anh cả" is the wrong subtitle."""

    seen = []

    def fake_completion(messages, *, temperature, operation, model=None):
        payload = _request_payload(messages)
        seen.append((messages[1]["content"], payload))
        return json.dumps(
            {
                "translations": {
                    key: f"VI:{item['text']}" for key, item in payload["lines"].items()
                },
                # The model tries to overrule the house style; it must not win.
                "glossary": {"大哥": "anh cả", "李云": "Lý Vân"},
            },
            ensure_ascii=False,
        )

    _hosted_translation(monkeypatch, fake_completion)
    cues = [
        {"id": index, "start": index, "end": index + 1, "text": f"line {index}"}
        for index in range(1, 5)
    ]

    ai.translate_cues(
        cues,
        "Tiếng Việt",
        batch_size=2,
        source_language="zh",
        style="auto",
        style_notes="陛下 → bệ hạ\nGiữ giọng cổ trang",
    )

    first_prompt, first = seen[0]
    _second_prompt, second = seen[1]
    assert first["glossary"]["大哥"] == "đại ca"  # seeded, not learned
    assert first["glossary"]["陛下"] == "bệ hạ"
    assert "Giữ giọng cổ trang" in first_prompt
    assert "đại ca" in first_prompt  # the preset's rules ride along too
    assert second["glossary"]["大哥"] == "đại ca"  # the model's override was refused
    assert second["glossary"]["李云"] == "Lý Vân"  # but it can still add its own


def test_translation_retranslates_only_the_line_the_model_merged(monkeypatch):
    """Merging two short lines into one sentence is what models actually do to
    subtitles; it used to fail the whole job on a length mismatch."""

    calls = []

    def fake_completion(messages, *, temperature, operation, model=None):
        requested = _requested_lines(messages)
        calls.append(requested)
        if len(requested) > 1:
            # "Two" was folded into the first line and its key never came back.
            return json.dumps({"1": "Một hai", "3": "Ba"}, ensure_ascii=False)
        return json.dumps({"1": "Hai"}, ensure_ascii=False)

    _hosted_translation(monkeypatch, fake_completion)
    cues = [
        {"id": index, "start": index, "end": index + 1, "text": text, "translation": ""}
        for index, text in enumerate(["One", "Two", "Three"], start=1)
    ]

    result = ai.translate_cues(cues, "Tiếng Việt", batch_size=3)

    assert [cue["translation"] for cue in result] == ["Một hai", "Hai", "Ba"]
    assert calls == [["One", "Two", "Three"], ["Two"]]


def test_translation_reports_which_lines_the_model_never_returned(monkeypatch):
    def fake_completion(messages, *, temperature, operation, model=None):
        if len(_requested_lines(messages)) == 1:
            return json.dumps({}, ensure_ascii=False)
        return json.dumps({"1": "Một"}, ensure_ascii=False)

    _hosted_translation(monkeypatch, fake_completion)
    cues = [
        {"id": index, "start": index, "end": index + 1, "text": text, "translation": ""}
        for index, text in enumerate(["One", "Two"], start=1)
    ]

    try:
        ai.translate_cues(cues, "Tiếng Việt", batch_size=2)
    except ai.AIProviderError as error:
        assert "dòng 2" in str(error), str(error)
    else:
        raise AssertionError("expected the unrepairable line to fail the batch")


def test_dialogue_analysis_inserts_only_line_breaks(monkeypatch):
    captured = {}

    def fake_analyze(targets, context_before, context_after, language):
        captured.update(
            targets=targets,
            context_before=context_before,
            context_after=context_after,
            language=language,
        )
        return {
            1: "How much is the ticket?\nThe fare is $5.50.\nCan I pay cash?\nYes."
        }

    monkeypatch.setattr(ai, "_analyze_dialogue_batch", fake_analyze)
    cues = [
        {
            "id": 1,
            "start": 0,
            "end": 4,
            "text": "[S1] How much is the ticket? The fare is $5.50. Can I pay cash? Yes.",
            "translation": "",
            "speaker": 0,
        }
    ]

    result = ai.analyze_dialogue_turns(cues, "en")

    assert result[0]["text"] == (
        "How much is the ticket?\nThe fare is $5.50.\nCan I pay cash?\nYes."
    )
    assert result[0]["speaker"] == 0
    assert captured["language"] == "en"


def test_dialogue_analysis_keeps_original_when_retry_still_changes_transcript(monkeypatch):
    monkeypatch.setattr(
        ai,
        "_analyze_dialogue_batch",
        lambda *_args: {1: "The fare is six dollars."},
    )

    result, report = ai.analyze_dialogue_turns(
        [
            {
                "id": 1,
                "start": 0,
                "end": 1,
                "text": "The fare is five dollars.",
                "translation": "",
            }
        ],
        return_report=True,
    )

    assert result[0]["text"] == "The fare is five dollars."
    assert report["retried_cues"] == 1
    assert report["failed_cue_ids"] == [1]


def test_dialogue_analysis_retries_only_missing_cue(monkeypatch):
    calls = []

    def fake_analyze(targets, *_args):
        cue_ids = [index + 1 for index, _cue in targets]
        calls.append(cue_ids)
        if cue_ids == [1, 2]:
            return {1: "Hello.\nHi."}
        return {2: "How much?\nFive dollars."}

    monkeypatch.setattr(ai, "_analyze_dialogue_batch", fake_analyze)
    result, report = ai.analyze_dialogue_turns(
        [
            {"id": 1, "start": 0, "end": 1, "text": "Hello. Hi.", "translation": ""},
            {
                "id": 2,
                "start": 1,
                "end": 2,
                "text": "How much? Five dollars.",
                "translation": "",
            },
        ],
        batch_size=2,
        return_report=True,
    )

    assert calls == [[1, 2], [2]]
    assert [cue["text"] for cue in result] == [
        "Hello.\nHi.",
        "How much?\nFive dollars.",
    ]
    assert report["retried_cues"] == 1
    assert report["failed_cues"] == 0


def test_dialogue_analysis_calls_openai_compatible_endpoint(monkeypatch):
    captured = {}

    class DialogueStubHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["authorization"] = self.headers.get("Authorization")
            captured["payload"] = json.loads(self.rfile.read(length).decode("utf-8"))
            content = json.dumps(
                {"1": "How much?\nThe fare is $5.50."}, ensure_ascii=False
            )
            body = json.dumps(
                {"choices": [{"message": {"content": content}}]},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), DialogueStubHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        ai,
        "settings",
        replace(
            ai.settings,
            llm_base_url=f"http://127.0.0.1:{server.server_port}/v1",
            llm_api_key="analysis-key",
            llm_model="dialogue-model",
            speaker_analysis_model="",
        ),
    )

    try:
        result = ai.analyze_dialogue_turns(
            [
                {
                    "id": 1,
                    "start": 0,
                    "end": 2,
                    "text": "How much? The fare is $5.50.",
                    "translation": "",
                    "speaker": 0,
                }
            ],
            "en",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result[0]["text"] == "How much?\nThe fare is $5.50."
    assert captured["path"] == "/v1/chat/completions"
    assert captured["authorization"] == "Bearer analysis-key"
    assert captured["payload"]["model"] == "dialogue-model"
    assert captured["payload"]["temperature"] == 0.0
    assert '"speaker_hint": 0' in captured["payload"]["messages"][1]["content"]


def test_llm_calls_are_paced_when_an_interval_is_configured(monkeypatch):
    """Cheapest rate limit is the one never triggered."""

    monkeypatch.setattr(
        ai, "settings", replace(ai.settings, llm_min_interval_seconds=0.05)
    )
    monkeypatch.setattr(ai, "_llm_next_call_at", 0.0)
    slept = []
    monkeypatch.setattr(ai.time, "sleep", slept.append)

    ai._wait_for_llm_slot()  # nothing to wait for yet
    ai._wait_for_llm_slot()

    assert len(slept) == 1, slept
    assert 0.04 <= slept[0] <= 0.06, slept


def test_llm_calls_run_flat_out_without_an_interval(monkeypatch):
    monkeypatch.setattr(
        ai, "settings", replace(ai.settings, llm_min_interval_seconds=0.0)
    )
    monkeypatch.setattr(ai, "_llm_next_call_at", time.monotonic() + 30)

    started = time.monotonic()
    ai._wait_for_llm_slot()

    assert time.monotonic() - started < 1


def test_translation_map_parser_accepts_markdown_fence_and_plain_arrays():
    assert ai._extract_translation_map(
        '```json\n{"1": "Một", "2": "Hai"}\n```', [1, 2]
    ) == {1: "Một", 2: "Hai"}
    # A bare array is positional, so it is only trusted at the exact length.
    assert ai._extract_translation_map('["Một", "Hai"]', [1, 2]) == {1: "Một", 2: "Hai"}
    assert ai._extract_translation_map('["Một Hai"]', [1, 2]) == {}


def test_transformer_provider_rejects_wrong_target_language(monkeypatch):
    monkeypatch.setattr(
        ai,
        "settings",
        replace(
            ai.settings,
            translation_provider="transformers",
            translation_model="example/model",
            transformers_target_language="Tiếng Việt",
        ),
    )
    try:
        ai._translate_batch_transformers(["Hello"], "English")
    except ai.AIProviderError as error:
        assert "Tiếng Việt" in str(error)
    else:
        raise AssertionError("expected target-language validation error")


def test_translate_cues_forwards_custom_model(monkeypatch):
    called_models = []

    def mock_completion(messages, *, temperature, operation, model=None):
        called_models.append(model)
        return json.dumps({"translations": {"1": "Xin chào"}})

    _hosted_translation(monkeypatch, mock_completion)

    cues = [{"id": 1, "start": 0.0, "end": 1.0, "text": "Hello", "translation": ""}]
    result = ai.translate_cues(cues, "Tiếng Việt", model="custom-gpt-model")

    assert called_models == ["custom-gpt-model"]
    assert result[0]["translation"] == "Xin chào"
