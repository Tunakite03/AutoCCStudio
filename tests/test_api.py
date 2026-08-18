import json
import shutil
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import backend.ai as ai_module
import backend.api.jobs as jobs_api
import backend.jobs.tasks as tasks_module
from backend.app import app
from backend.config import RUNTIME_DIR
from backend.jobs import runner, store
from backend.jobs.model import new_job
from backend.jobs.tasks import speaker_analysis_task, transcription_task, translation_task
from backend.messages import Message

client = TestClient(app)


def make_job(kind, **fields):
    """Create and persist a job, returning it for direct inspection."""

    job = new_job(kind, **{k: v for k, v in fields.items() if k in {"video_name", "subtitle_name"}})
    for key, value in fields.items():
        if key not in {"video_name", "subtitle_name"}:
            job[key] = value
    store.create(job)
    return job


def cleanup(job_id):
    store.discard_from_memory(job_id)
    shutil.rmtree(RUNTIME_DIR / job_id, ignore_errors=True)


def wait_for_status(job_id, *, not_status="processing", timeout=5.0):
    """Poll until the runner's pool has taken the job to a terminal state."""

    deadline = time.monotonic() + timeout
    job = client.get(f"/api/jobs/{job_id}").json()
    while time.monotonic() < deadline and job.get("status") == not_status:
        time.sleep(0.01)
        job = client.get(f"/api/jobs/{job_id}").json()
    return job


def analysis_report(total=1, failed_ids=()):
    return {
        "total_cues": total,
        "acoustic_split_cues": 0,
        "ai_modified_cues": total - len(failed_ids),
        "retried_cues": len(failed_ids),
        "failed_cues": len(failed_ids),
        "failed_cue_ids": list(failed_ids),
    }


# ── System ───────────────────────────────────────────────────────────


def test_health_and_capabilities():
    assert client.get("/api/health").json()["ok"] is True
    capabilities = client.get("/api/capabilities").json()
    assert "whisper_model" in capabilities
    assert "deepgram_configured" in capabilities
    assert "speaker_analysis_configured" in capabilities
    assert capabilities["deepgram_model"]
    assert capabilities["max_concurrent_jobs"] >= 1
    assert any(
        model["value"] == "nova-2-meeting"
        for model in capabilities["transcription_models"]["deepgram"]
    )


def test_capabilities_reports_configuration_without_the_credentials(monkeypatch):
    import backend.api.system as system_api

    monkeypatch.setattr(
        system_api,
        "settings",
        replace(
            system_api.settings,
            deepgram_api_key="dg-secret-value",
            llm_api_key="llm-secret-value",
        ),
    )
    body = client.get("/api/capabilities").text
    assert "dg-secret-value" not in body
    assert "llm-secret-value" not in body
    assert client.get("/api/capabilities").json()["deepgram_configured"] is True


@pytest.mark.parametrize("configured", ["MistralAI", "openai_compatible", "ollama"])
def test_a_hosted_translation_provider_is_reported_as_configured(monkeypatch, configured):
    """Any name that is not the mock or a local pipeline is spoken to over the
    OpenAI-compatible API, so capabilities must not call it unconfigured."""

    import backend.api.system as system_api

    monkeypatch.setattr(
        system_api,
        "settings",
        replace(
            system_api.settings,
            translation_provider=configured,
            llm_base_url="https://api.mistral.ai/v1",
        ),
    )
    body = client.get("/api/capabilities").json()
    assert body["translation_provider"] == "openai_compatible"
    assert body["translation_configured"] is True


# ── Transcription ────────────────────────────────────────────────────


def test_transcription_route_passes_selected_deepgram_model(monkeypatch):
    captured = {}

    def fake_transcribe(_path, model_size=None, language=None, provider=None, on_progress=None):
        captured.update(model=model_size, language=language, provider=provider)
        if on_progress:
            on_progress(3, 10, Message("progress.transcribing"))
        return [
            {
                "id": 1,
                "start": 0.0,
                "end": 1.0,
                "text": "[S1] How much? The fare is $5.50.",
                "translation": "",
                "speaker": 0,
            }
        ], "en"

    def fake_analyze(cues, language=None, return_report=False, on_progress=None):
        captured.update(analysis_language=language, analysis_input=cues[0]["text"])
        analyzed = [dict(cue) for cue in cues]
        analyzed[0]["text"] = "How much?\nThe fare is $5.50."
        return (analyzed, analysis_report()) if return_report else analyzed

    monkeypatch.setattr(
        jobs_api, "settings", replace(jobs_api.settings, deepgram_api_key="integration-key")
    )
    monkeypatch.setattr(tasks_module, "transcribe_video", fake_transcribe)
    monkeypatch.setattr(tasks_module, "analyze_dialogue_turns", fake_analyze)

    response = client.post(
        "/api/jobs/transcribe",
        data={
            "provider": "deepgram",
            "model": "nova-2-meeting",
            "source_language": "en",
            "analyze_speakers": "true",
        },
        files={"video": ("demo.mp4", b"fake-video", "video/mp4")},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    try:
        job = wait_for_status(job_id)
        assert job["status"] == "completed"
        assert job["transcription_model"] == "nova-2-meeting"
        assert job["speaker_analysis_status"] == "completed"
        assert job["speaker_analysis_report"]["ai_modified_cues"] == 1
        assert job["cues"][0]["text"] == "How much?\nThe fare is $5.50."
        assert job["cues"][0]["speaker"] == 0
        assert job["progress"] is None  # cleared once the job settles
        assert captured == {
            "model": "nova-2-meeting",
            "language": "en",
            "provider": "deepgram",
            "analysis_language": "en",
            "analysis_input": "How much? The fare is $5.50.",
        }
    finally:
        cleanup(job_id)


def test_transcription_reports_partial_speaker_analysis_without_losing_cues(monkeypatch):
    job = make_job(
        "transcription",
        video_name="dialogue.mp4",
        speaker_analysis_requested=True,
        speaker_analysis_status="pending",
    )
    job["video_path"] = str(RUNTIME_DIR / job["id"] / "video.mp4")
    store.create(job)

    monkeypatch.setattr(
        tasks_module,
        "transcribe_video",
        lambda *_args, **_kwargs: (
            [
                {"id": 1, "start": 0, "end": 1, "text": "Hello. Hi.", "translation": ""},
                {"id": 2, "start": 1, "end": 2, "text": "How much? Five dollars.", "translation": ""},
            ],
            "en",
        ),
    )

    def fake_analyze(cues, _language, return_report=False, on_progress=None):
        analyzed = [dict(cue) for cue in cues]
        analyzed[0]["text"] = "Hello.\nHi."
        return (analyzed, analysis_report(total=2, failed_ids=[2])) if return_report else analyzed

    monkeypatch.setattr(tasks_module, "analyze_dialogue_turns", fake_analyze)

    try:
        runner.run_blocking(
            job["id"], "transcription", transcription_task("deepgram", "nova-3", "en", True)
        )
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "completed"
        assert result["speaker_analysis_status"] == "partial"
        assert result["speaker_analysis_report"]["failed_cue_ids"] == [2]
        assert result["cues"][0]["text"] == "Hello.\nHi."
        assert result["cues"][1]["text"] == "How much? Five dollars."
    finally:
        cleanup(job["id"])


def test_existing_job_can_reanalyze_speakers_without_retranscription(monkeypatch):
    job = make_job(
        "subtitle_import",
        subtitle_name="dialogue.srt",
        cues=[{"id": 1, "start": 0, "end": 2, "text": "How much? Five dollars.", "translation": ""}],
    )

    def fake_analyze(cues, _language, return_report=False, on_progress=None):
        analyzed = [dict(cue) for cue in cues]
        analyzed[0]["text"] = "How much?\nFive dollars."
        return (analyzed, analysis_report()) if return_report else analyzed

    monkeypatch.setattr(tasks_module, "analyze_dialogue_turns", fake_analyze)
    monkeypatch.setattr(
        jobs_api,
        "settings",
        replace(
            jobs_api.settings,
            llm_base_url="http://local-llm.test/v1",
            llm_model="dialogue-model",
        ),
    )

    try:
        response = client.post(f"/api/jobs/{job['id']}/analyze-speakers")
        assert response.status_code == 200, response.text
        result = wait_for_status(job["id"])
        assert result["status"] == "completed"
        assert result["speaker_analysis_status"] == "completed"
        assert result["cues"][0]["text"] == "How much?\nFive dollars."
    finally:
        cleanup(job["id"])


def test_retranscribe_reuses_the_stored_video_without_an_upload(monkeypatch):
    """Reopening a project leaves the browser without the file; the server still
    has it, so recognition must be re-runnable from the job alone."""

    seen = {}

    def fake_transcribe(path, model_size=None, language=None, provider=None, on_progress=None):
        seen.update(path=str(path), model=model_size, language=language, provider=provider)
        return [{"id": 1, "start": 0.0, "end": 1.0, "text": "again", "translation": "", "speaker": 0}], "en"

    monkeypatch.setattr(
        jobs_api, "settings", replace(jobs_api.settings, deepgram_api_key="integration-key")
    )
    monkeypatch.setattr(tasks_module, "transcribe_video", fake_transcribe)

    job = make_job("transcription", video_name="kept.mp4")
    video_path = RUNTIME_DIR / job["id"] / "video.mp4"
    video_path.write_bytes(b"fake-video")
    with store.edit(job["id"]) as opened:
        opened["status"] = "completed"  # a reopened project is never mid-run
        opened["video_path"] = str(video_path)
        opened["cues"] = [{"id": 1, "start": 0.0, "end": 2.0, "text": "old", "translation": "cũ"}]
    job_id = job["id"]

    try:
        response = client.post(
            f"/api/jobs/{job_id}/transcribe",
            data={"provider": "deepgram", "model": "nova-3", "source_language": "en"},
        )
        assert response.status_code == 200, response.text
        refreshed = wait_for_status(job_id)
        assert seen["path"] == str(video_path)
        assert seen["provider"] == "deepgram"
        assert seen["model"] == "nova-3"
        assert refreshed["status"] == "completed"
        assert [cue["text"] for cue in refreshed["cues"]] == ["again"]
    finally:
        cleanup(job_id)


def test_retranscribe_rejects_jobs_without_a_stored_video():
    job = make_job("subtitle_import", subtitle_name="only-subs.srt")
    try:
        # faster_whisper needs no credentials, so the only thing that can fail
        # here is the missing video — a provider gated on an API key would reject
        # the request before the check under test ever runs.
        response = client.post(f"/api/jobs/{job['id']}/transcribe", data={"provider": "whisper"})
        assert response.status_code == 400, response.text
        assert response.json()["detail"]["code"] == "err.job.videoGone"
    finally:
        cleanup(job["id"])


# ── Editing and export ───────────────────────────────────────────────


def test_import_edit_and_download_srt():
    source = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    response = client.post(
        "/api/jobs/import-subtitle",
        files={"file": ("demo.srt", source, "application/x-subrip")},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    job_id = job["id"]
    try:
        assert job["cues"][0]["text"] == "Hello"
        edited = client.put(
            f"/api/jobs/{job_id}/cues",
            json={
                "cues": [
                    {
                        "id": 1,
                        "start": 0,
                        "end": 1.25,
                        "text": "Hello",
                        "translation": "Xin chào",
                        "speaker": 1,
                    }
                ]
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["cues"][0]["speaker"] == 1
        download = client.get(f"/api/jobs/{job_id}/download?track=translated&format=srt")
        assert download.status_code == 200
        assert b"Xin ch\xc3\xa0o" in download.content
        assert b"00:00:01,250" in download.content
    finally:
        cleanup(job_id)


def test_rejected_subtitle_import_leaves_no_orphan_directory():
    """The directory is created before the file can be validated; a rejection
    must take it back out rather than leave an invisible husk on disk."""

    before = set(RUNTIME_DIR.iterdir()) if RUNTIME_DIR.exists() else set()
    response = client.post(
        "/api/jobs/import-subtitle",
        files={"file": ("empty.srt", b"not a subtitle at all", "application/x-subrip")},
    )
    assert response.status_code == 400
    after = set(RUNTIME_DIR.iterdir()) if RUNTIME_DIR.exists() else set()
    assert after == before


def test_split_long_cues_api_route():
    job = make_job(
        "subtitle_import",
        subtitle_name="dialogue.srt",
        cues=[
            {
                "id": 1,
                "start": 0.0,
                "end": 20.0,
                "text": (
                    "How's everything coming along? The Shogun is coming to visit in one week. "
                    "Everyone knows he's trying to decide who his success will be. "
                    "And if I have anything to do with it, it'll be me."
                ),
                "translation": "",
            }
        ],
    )
    try:
        response = client.post(f"/api/jobs/{job['id']}/split-long-cues")
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["cues"]) >= 3
        assert data["cues"][0]["text"] == "How's everything coming along?"
        for cue in data["cues"]:
            assert cue["text"].count("\n") <= 1
            assert len(cue["text"]) <= 75
    finally:
        cleanup(job["id"])


# ── Live updates ─────────────────────────────────────────────────────


def test_sse_stream_emits_initial_and_completed_job_snapshots():
    job = make_job("transcription", video_name="demo.mp4")
    job_id = job["id"]

    def complete_job():
        with store.edit(job_id) as opened:
            opened["status"] = "completed"
            opened["cues"] = [{"id": 1, "start": 0, "end": 1, "text": "Hello", "translation": ""}]

    timer = threading.Timer(0.05, complete_job)
    timer.start()
    try:
        with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
            content = "".join(response.iter_text())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache, no-transform"
        snapshots = [
            json.loads(line.removeprefix("data: "))
            for line in content.splitlines()
            if line.startswith("data: ")
        ]
        assert [snapshot["status"] for snapshot in snapshots] == ["processing", "completed"]
        assert snapshots[1]["revision"] > snapshots[0]["revision"]
        assert snapshots[1]["cues"][0]["text"] == "Hello"
    finally:
        timer.cancel()
        cleanup(job_id)


def test_sse_stream_publishes_progress_without_a_status_change():
    """Long jobs used to sit on "processing" with nothing to show; progress ticks
    now reach the client as ordinary snapshots."""

    job = make_job("transcription", video_name="slow.mp4")
    job_id = job["id"]

    def report_then_finish():
        context = runner_context(job_id)
        context.progress(
            "transcribing", current=30, total=100, message=Message("progress.transcribing")
        )
        time.sleep(0.05)
        with store.edit(job_id) as opened:
            opened["status"] = "completed"

    timer = threading.Timer(0.05, report_then_finish)
    timer.start()
    try:
        with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
            content = "".join(response.iter_text())
        snapshots = [
            json.loads(line.removeprefix("data: "))
            for line in content.splitlines()
            if line.startswith("data: ")
        ]
        progressed = [s for s in snapshots if s.get("progress")]
        assert progressed, f"no progress snapshot in {snapshots}"
        assert progressed[-1]["progress"]["ratio"] == 0.3
        assert progressed[-1]["progress"]["phase"] == "transcribing"
        assert snapshots[-1]["status"] == "completed"
    finally:
        timer.cancel()
        cleanup(job_id)


def runner_context(job_id):
    from backend.jobs.runner import JobContext

    return JobContext(store, job_id, "test")


def test_sse_stream_returns_404_for_unknown_job():
    assert client.get("/api/jobs/not-a-real-job/events").status_code == 404


# ── Project list and deletion ────────────────────────────────────────


def test_project_list_summarises_jobs_without_cue_payload():
    job = make_job(
        "subtitle_import",
        subtitle_name="dashboard.srt",
        cues=[
            {"id": 1, "start": 0.0, "end": 2.0, "text": "one", "translation": "một"},
            {"id": 2, "start": 2.0, "end": 5.5, "text": "two", "translation": ""},
        ],
    )
    try:
        payload = client.get("/api/jobs").json()
        entry = next(item for item in payload["jobs"] if item["id"] == job["id"])
        assert entry["name"] == "dashboard.srt"
        assert entry["cue_count"] == 2
        assert entry["translated_count"] == 1
        assert entry["duration_seconds"] == 5.5
        assert entry["video_available"] is False
        assert entry["updated_at"] > 0
        assert "cues" not in entry
    finally:
        cleanup(job["id"])


def test_project_list_reflects_an_edit_despite_the_summary_cache():
    """Summaries are cached against mtimes, so a stale entry would be invisible
    until the next restart."""

    job = make_job(
        "subtitle_import",
        subtitle_name="cached.srt",
        cues=[{"id": 1, "start": 0, "end": 1, "text": "one", "translation": ""}],
    )
    try:
        first = next(i for i in client.get("/api/jobs").json()["jobs"] if i["id"] == job["id"])
        assert first["cue_count"] == 1

        client.put(
            f"/api/jobs/{job['id']}/cues",
            json={
                "cues": [
                    {"id": 1, "start": 0, "end": 1, "text": "one", "translation": "một"},
                    {"id": 2, "start": 1, "end": 2, "text": "two", "translation": ""},
                ]
            },
        )
        second = next(i for i in client.get("/api/jobs").json()["jobs"] if i["id"] == job["id"])
        assert second["cue_count"] == 2
        assert second["translated_count"] == 1
    finally:
        cleanup(job["id"])


def test_delete_job_removes_directory_and_forgets_state():
    job = make_job("subtitle_import", subtitle_name="throwaway.srt")
    job_id = job["id"]

    assert (RUNTIME_DIR / job_id).exists()
    response = client.delete(f"/api/jobs/{job_id}")
    assert response.status_code == 200
    assert response.json()["deleted"] == job_id
    assert not (RUNTIME_DIR / job_id).exists()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404


def test_deleting_a_running_job_does_not_let_its_worker_recreate_it():
    """The worker still holds the job after the delete; its final write must not
    bring the directory back from the dead."""

    job = make_job("transcription", video_name="doomed.mp4")
    job_id = job["id"]

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert not (RUNTIME_DIR / job_id).exists()

    job["status"] = "completed"
    job["cues"] = [{"id": 1, "start": 0, "end": 1, "text": "late", "translation": ""}]
    store._persist(job)

    assert not (RUNTIME_DIR / job_id).exists()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_worker_scheduled_before_a_delete_exits_quietly():
    job = make_job("transcription", video_name="raced.mp4")
    job_id = job["id"]

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    # Queued before the delete landed: it must not raise and must not resurrect.
    runner.run_blocking(job_id, "translation", translation_task("Tiếng Việt"))
    assert not (RUNTIME_DIR / job_id).exists()


@pytest.mark.parametrize("job_id", ["not-a-real-job", "0" * 31, "Z" * 32, "..%5Cescape"])
def test_paths_reject_ids_that_could_never_name_a_job(job_id):
    """Validation lives in the store, so every route inherits it."""

    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/thumbnail").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/waveform").status_code == 404


@pytest.mark.parametrize("job_id", ["..", "../secret", r"..\..\windows", "a" * 33, ""])
def test_the_store_never_builds_a_path_from_an_untrusted_id(job_id):
    """A job id is the only part of a filesystem path a caller controls, and on
    Windows a backslash survives the URL router."""

    from backend.jobs import JobNotFound

    with pytest.raises(JobNotFound):
        store.job_dir(job_id)


# ── Failure handling ─────────────────────────────────────────────────


def test_unexpected_worker_error_fails_the_job_instead_of_hanging_it(monkeypatch):
    """An exception the worker does not anticipate must still end the job, or the
    status stays "processing" forever and every SSE client hangs on it."""

    job = make_job("transcription", video_name="boom.mp4")
    job["video_path"] = str(RUNTIME_DIR / job["id"] / "video.mp4")
    store.create(job)

    def exploding_transcribe(*_args, **_kwargs):
        raise KeyError("segments")

    monkeypatch.setattr(tasks_module, "transcribe_video", exploding_transcribe)

    try:
        runner.run_blocking(
            job["id"], "transcription", transcription_task("deepgram", "nova-3", "en", False)
        )
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "error"
        assert result["error"] == {"code": "err.unexpected", "params": {"type": "KeyError"}}
        assert result["progress"] is None
    finally:
        cleanup(job["id"])


def test_unexpected_speaker_analysis_error_keeps_the_transcription(monkeypatch):
    job = make_job("transcription", video_name="partial.mp4")
    job["video_path"] = str(RUNTIME_DIR / job["id"] / "video.mp4")
    store.create(job)

    monkeypatch.setattr(
        tasks_module,
        "transcribe_video",
        lambda *_args, **_kwargs: (
            [{"id": 1, "start": 0, "end": 1, "text": "kept", "translation": ""}],
            "en",
        ),
    )

    def exploding_analyze(*_args, **_kwargs):
        raise TypeError("report is not subscriptable")

    monkeypatch.setattr(tasks_module, "analyze_dialogue_turns", exploding_analyze)

    try:
        runner.run_blocking(
            job["id"], "transcription", transcription_task("deepgram", "nova-3", "en", True)
        )
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "completed"
        assert result["speaker_analysis_status"] == "failed"
        assert result["speaker_analysis_error"]["params"]["type"] == "TypeError"
        assert [cue["text"] for cue in result["cues"]] == ["kept"]
    finally:
        cleanup(job["id"])


def test_a_failed_batch_keeps_the_translations_that_already_succeeded(monkeypatch):
    """Losing forty minutes of translation because batch 41 came back malformed
    was the single most expensive failure mode in the old worker."""

    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    calls = {"n": 0}
    real_batch = ai_module._translate_batch

    def flaky_batch(lines, target_language, **context):
        calls["n"] += 1
        if calls["n"] > 2:
            raise ai_module.AIProviderError("err.test.providerDied")
        return real_batch(lines, target_language, **context)

    monkeypatch.setattr(ai_module, "_translate_batch", flaky_batch)

    # 50 lines at the default batch size of 20: two batches land, the third dies.
    job = make_job(
        "subtitle_import",
        subtitle_name="long.srt",
        cues=[
            {"id": i, "start": i, "end": i + 1, "text": f"line {i}", "translation": ""}
            for i in range(1, 51)
        ],
    )
    try:
        runner.run_blocking(job["id"], "translation", translation_task("Tiếng Việt"))
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "error"
        assert result["error"]["code"] == "err.test.providerDied"
        translated = [cue["translation"] for cue in result["cues"] if cue["translation"]]
        assert len(translated) == 40, len(translated)
        assert translated[0] == "[Tiếng Việt] line 1"
    finally:
        cleanup(job["id"])


# ── Stopping a run ───────────────────────────────────────────────────


def long_job(name="long.srt", count=50, **fields):
    """A subtitle project with enough cues to span several translation batches."""

    return make_job(
        "subtitle_import",
        subtitle_name=name,
        cues=[
            {"id": i, "start": i, "end": i + 1, "text": f"line {i}", "translation": ""}
            for i in range(1, count + 1)
        ],
        **fields,
    )


def test_a_stop_request_lands_at_the_next_checkpoint_and_keeps_what_was_translated(monkeypatch):
    """A worker thread cannot be killed, so stopping is a flag it notices. What
    it had already checkpointed is the point of stopping rather than deleting."""

    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    job = long_job(status="processing")
    calls = {"n": 0}
    real_batch = ai_module._translate_batch

    def stop_after_second_batch(lines, target_language, **context):
        calls["n"] += 1
        translations = real_batch(lines, target_language, **context)
        if calls["n"] == 2:
            assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        return translations

    monkeypatch.setattr(ai_module, "_translate_batch", stop_after_second_batch)

    try:
        runner.run_blocking(job["id"], "translation", translation_task("Tiếng Việt"))
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "cancelled"
        assert result["error"] is None
        assert result["progress"] is None
        # Lowered again, or the next run would stop before it started.
        assert result["cancel_requested"] is False
        assert calls["n"] == 2, "the third batch was paid for after the stop"
        translated = [cue["translation"] for cue in result["cues"] if cue["translation"]]
        assert len(translated) == 40, len(translated)
    finally:
        cleanup(job["id"])


def test_a_job_stays_processing_until_the_worker_notices_the_stop():
    """The whole "đang dừng…" state: Deepgram sends one request for an entire
    video, so the flag can be up for minutes before anything acts on it."""

    job = long_job(name="inflight.srt", status="processing")
    try:
        accepted = client.post(f"/api/jobs/{job['id']}/cancel").json()
        assert accepted["cancel_requested"] is True
        assert accepted["status"] == "processing"

        # And it survives a reload, so a refreshed browser still shows it.
        store.discard_from_memory(job["id"])
        assert json.loads((RUNTIME_DIR / job["id"] / "job.json").read_text("utf-8"))[
            "cancel_requested"
        ] is True
    finally:
        cleanup(job["id"])


def test_a_stop_reaches_a_worker_that_is_waiting_out_a_rate_limit(monkeypatch):
    """Rate-limit backoffs are the longest a worker ever blocks. If the stop is
    only read between HTTP calls, the button looks dead for a whole minute."""

    from backend import httpclient

    job = long_job(name="waiting.srt", count=2, status="processing")

    def wait_like_a_rate_limited_provider(*_args, **_kwargs):
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        httpclient._wait(30.0)
        pytest.fail("the backoff sat through the stop request")

    monkeypatch.setattr(tasks_module, "translate_cues", wait_like_a_rate_limited_provider)

    try:
        started = time.monotonic()
        runner.run_blocking(job["id"], "translation", translation_task("Tiếng Việt"))
        assert time.monotonic() - started < 5.0, "it waited out the whole backoff"
        assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "cancelled"
    finally:
        cleanup(job["id"])


def test_stopping_a_job_that_is_not_running_is_refused():
    job = long_job(name="idle.srt", count=2)
    try:
        response = client.post(f"/api/jobs/{job['id']}/cancel")
        assert response.status_code == 409
        assert client.get(f"/api/jobs/{job['id']}").json()["cancel_requested"] is False
    finally:
        cleanup(job["id"])


def test_a_stop_between_phases_keeps_the_transcript_and_skips_the_speaker_pass(monkeypatch):
    """Deepgram's single opaque request cannot be interrupted, so the stop is
    noticed once it returns — after the expensive part is safely written."""

    job = make_job(
        "transcription",
        video_name="stop.mp4",
        status="processing",
        speaker_analysis_requested=True,
        speaker_analysis_status="pending",
    )
    job["video_path"] = str(RUNTIME_DIR / job["id"] / "video.mp4")
    store.create(job)

    def transcribe_then_stop(*_args, **_kwargs):
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        return [{"id": 1, "start": 0, "end": 1, "text": "kept", "translation": ""}], "en"

    def unreachable_analysis(*_args, **_kwargs):
        pytest.fail("speaker analysis ran after the job was stopped")

    monkeypatch.setattr(tasks_module, "transcribe_video", transcribe_then_stop)
    monkeypatch.setattr(tasks_module, "analyze_dialogue_turns", unreachable_analysis)

    try:
        runner.run_blocking(
            job["id"], "transcription", transcription_task("deepgram", "nova-3", "en", True)
        )
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "cancelled"
        assert [cue["text"] for cue in result["cues"]] == ["kept"]
        assert result["speaker_analysis_status"] == "cancelled"
    finally:
        cleanup(job["id"])


def test_editing_the_cues_of_a_stopped_job_hands_the_project_back(monkeypatch):
    job = long_job(name="stopped.srt", count=2, status="cancelled")
    try:
        response = client.put(
            f"/api/jobs/{job['id']}/cues",
            json={"cues": [{"id": 1, "start": 0, "end": 1, "text": "sửa tay"}]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
    finally:
        cleanup(job["id"])


# ── Resuming a translation ───────────────────────────────────────────


def test_translation_resumes_from_a_cue_without_paying_for_the_earlier_ones(monkeypatch):
    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    sent = []
    real_batch = ai_module._translate_batch

    def recording_batch(lines, target_language, **context):
        sent.extend(line["text"] for line in lines)
        return real_batch(lines, target_language, **context)

    monkeypatch.setattr(ai_module, "_translate_batch", recording_batch)

    job = make_job(
        "subtitle_import",
        subtitle_name="resume.srt",
        cues=[
            {"id": 1, "start": 0, "end": 1, "text": "line 1", "translation": "giữ nguyên"},
            {"id": 2, "start": 1, "end": 2, "text": "line 2", "translation": ""},
            {"id": 3, "start": 2, "end": 3, "text": "line 3", "translation": ""},
        ],
    )
    try:
        runner.run_blocking(
            job["id"], "translation", translation_task("Tiếng Việt", from_cue=1)
        )
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert sent == ["line 2", "line 3"]
        assert [cue["translation"] for cue in result["cues"]] == [
            "giữ nguyên",
            "[Tiếng Việt] line 2",
            "[Tiếng Việt] line 3",
        ]
    finally:
        cleanup(job["id"])


def test_a_resumed_batch_still_sees_the_lines_translated_before_it(monkeypatch):
    """Consistency is the whole reason batches carry context. A resume that
    quoted empty strings back to the model would drift at the seam."""

    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    context_seen = {}
    real_batch = ai_module._translate_batch

    def recording_batch(lines, target_language, **context):
        context_seen.setdefault("before", context.get("context_before"))
        return real_batch(lines, target_language, **context)

    monkeypatch.setattr(ai_module, "_translate_batch", recording_batch)

    job = make_job(
        "subtitle_import",
        subtitle_name="seam.srt",
        cues=[
            {"id": 1, "start": 0, "end": 1, "text": "line 1", "translation": "đã dịch 1"},
            {"id": 2, "start": 1, "end": 2, "text": "line 2", "translation": ""},
        ],
    )
    try:
        runner.run_blocking(
            job["id"], "translation", translation_task("Tiếng Việt", from_cue=1)
        )
        assert [item["translation"] for item in context_seen["before"]] == ["đã dịch 1"]
    finally:
        cleanup(job["id"])


def test_translate_route_rejects_a_start_cue_past_the_last_one():
    job = long_job(name="short.srt", count=3)
    try:
        response = client.post(
            f"/api/jobs/{job['id']}/translate",
            json={"target_language": "Tiếng Việt", "from_cue": 3},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "err.translation.noCuesFromHere"
    finally:
        cleanup(job["id"])


def test_processing_job_loaded_only_from_disk_is_marked_interrupted():
    job = make_job(
        "transcription",
        video_name="interrupted.mp4",
        speaker_analysis_requested=True,
        speaker_analysis_status="pending",
    )
    job_id = job["id"]
    store.discard_from_memory(job_id)

    try:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        restored = response.json()
        assert restored["status"] == "error"
        assert restored["speaker_analysis_status"] == "not_run"
        assert restored["error"]["code"] == "err.job.interrupted"
    finally:
        cleanup(job_id)


def test_job_metadata_is_replaced_atomically():
    """job.json is written through a temp file, so a crash mid-write cannot leave
    a truncated document that makes the project unreadable forever."""

    job = make_job("subtitle_import", subtitle_name="atomic.srt")
    job_id = job["id"]
    try:
        for index in range(5):
            with store.edit(job_id) as opened:
                opened["cues"] = [
                    {"id": i, "start": i, "end": i + 1, "text": "x" * 500, "translation": ""}
                    for i in range(index * 20)
                ]
        assert not (RUNTIME_DIR / job_id / "job.json.tmp").exists()
        on_disk = json.loads((RUNTIME_DIR / job_id / "job.json").read_text(encoding="utf-8"))
        assert on_disk["id"] == job_id
        assert len(on_disk["cues"]) == 80
    finally:
        cleanup(job_id)


def test_thumbnail_requires_a_video():
    job = make_job("subtitle_import", subtitle_name="no-video.srt")
    try:
        assert client.get(f"/api/jobs/{job['id']}/thumbnail").status_code == 404
    finally:
        cleanup(job["id"])


# ── Concurrency guards ───────────────────────────────────────────────


def test_cues_cannot_be_edited_while_a_worker_owns_them():
    job = make_job(
        "transcription",
        video_name="busy.mp4",
        cues=[{"id": 1, "start": 0, "end": 1, "text": "original", "translation": ""}],
    )
    job_id = job["id"]
    try:
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "processing"
        response = client.put(
            f"/api/jobs/{job_id}/cues",
            json={"cues": [{"id": 1, "start": 0, "end": 1, "text": "edited", "translation": ""}]},
        )
        assert response.status_code == 409
        assert client.get(f"/api/jobs/{job_id}").json()["cues"][0]["text"] == "original"

        with store.edit(job_id) as opened:
            opened["status"] = "completed"
        allowed = client.put(
            f"/api/jobs/{job_id}/cues",
            json={"cues": [{"id": 1, "start": 0, "end": 1, "text": "edited", "translation": ""}]},
        )
        assert allowed.status_code == 200
        assert allowed.json()["cues"][0]["text"] == "edited"
    finally:
        cleanup(job_id)


def test_a_busy_job_refuses_every_operation_that_would_race_its_worker():
    job = make_job(
        "transcription",
        video_name="busy.mp4",
        cues=[{"id": 1, "start": 0, "end": 1, "text": "one", "translation": ""}],
    )
    job_id = job["id"]
    try:
        assert client.post(f"/api/jobs/{job_id}/split-long-cues").status_code == 409
        assert client.post(
            f"/api/jobs/{job_id}/translate", json={"target_language": "Tiếng Việt"}
        ).status_code == 409
        assert client.post(f"/api/jobs/{job_id}/transcribe", data={}).status_code == 409
    finally:
        cleanup(job_id)


def test_translation_style_is_stored_on_the_job_and_survives_a_reload(monkeypatch):
    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    job = make_job(
        "subtitle_import",
        subtitle_name="wuxia.srt",
        cues=[{"id": 1, "start": 0, "end": 1, "text": "大哥", "translation": ""}],
        detected_language="zh",
    )
    try:
        response = client.post(
            f"/api/jobs/{job['id']}/translate",
            json={
                "target_language": "Tiếng Việt",
                "style": "han_viet",
                "style_notes": "陛下 → bệ hạ",
            },
        )
        assert response.status_code == 200, response.text
        wait_for_status(job["id"], timeout=10.0)
        # Reopening the project has to show what it was actually translated with.
        reloaded = client.get(f"/api/jobs/{job['id']}").json()
        assert reloaded["translation_style"] == "han_viet"
        assert reloaded["translation_style_notes"] == "陛下 → bệ hạ"
    finally:
        cleanup(job["id"])


def test_an_unknown_style_is_refused_rather_than_silently_ignored():
    job = make_job(
        "subtitle_import",
        subtitle_name="styled.srt",
        cues=[{"id": 1, "start": 0, "end": 1, "text": "one", "translation": ""}],
    )
    try:
        response = client.post(
            f"/api/jobs/{job['id']}/translate",
            json={"target_language": "Tiếng Việt", "style": "pirate"},
        )
        assert response.status_code == 400
        oversized = client.post(
            f"/api/jobs/{job['id']}/translate",
            json={"target_language": "Tiếng Việt", "style_notes": "x" * 2001},
        )
        assert oversized.status_code == 400
    finally:
        cleanup(job["id"])


def test_more_jobs_than_workers_all_reach_completion(monkeypatch):
    """The pool is bounded, so extra work must queue rather than be dropped."""

    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    jobs = [
        make_job(
            "subtitle_import",
            subtitle_name=f"queued-{index}.srt",
            cues=[{"id": 1, "start": 0, "end": 1, "text": f"line {index}", "translation": ""}],
        )
        for index in range(6)
    ]
    try:
        for job in jobs:
            response = client.post(
                f"/api/jobs/{job['id']}/translate", json={"target_language": "Tiếng Việt"}
            )
            assert response.status_code == 200, response.text
        for job in jobs:
            result = wait_for_status(job["id"], timeout=10.0)
            assert result["status"] == "completed", result
            assert result["cues"][0]["translation"].startswith("[Tiếng Việt]")
    finally:
        for job in jobs:
            cleanup(job["id"])


# ── Transport ────────────────────────────────────────────────────────


def test_cors_allows_localhost_but_not_arbitrary_sites():
    """There is no auth layer, so a wildcard would let any page the user has open
    read their projects off 127.0.0.1."""

    allowed = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    blocked = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in blocked.headers


def test_translation_provider_and_model_can_be_selected(monkeypatch):
    monkeypatch.setattr(
        ai_module, "settings", replace(ai_module.settings, translation_provider="mock")
    )
    job = make_job(
        "subtitle_import",
        subtitle_name="custom_model.srt",
        cues=[{"id": 1, "start": 0, "end": 1, "text": "Hello world", "translation": ""}],
    )
    try:
        response = client.post(
            f"/api/jobs/{job['id']}/translate",
            json={
                "target_language": "Tiếng Việt",
                "provider": "mock",
                "model": "my-custom-model",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["translation_provider"] == "mock"
        assert body["translation_model"] == "my-custom-model"

        result = wait_for_status(job["id"], timeout=10.0)
        assert result["status"] == "completed"
        assert result["translation_provider"] == "mock"
        assert result["translation_model"] == "my-custom-model"
    finally:
        cleanup(job["id"])


def test_capabilities_reports_translation_models():
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    data = response.json()
    assert "translation_models" in data
    assert "openai_compatible" in data["translation_models"]
    assert "transformers" in data["translation_models"]
    # Which catalogue comes back depends on LLM_BASE_URL, so this test only
    # asserts the picker is populated and well-formed; the host-to-catalogue
    # mapping is covered by the monkeypatched tests further down.
    for group in ("openai_compatible", "transformers"):
        options = data["translation_models"][group]
        assert options, group
        assert all(item["value"] and item["name"] for item in options)



# ── Translation model catalogue ──────────────────────────────────────


def test_the_model_picker_only_offers_models_the_endpoint_can_run(monkeypatch):
    """A model name from the wrong catalogue is a 400 at translate time, which
    the UI cannot warn about — so it must never be offered in the first place."""

    import backend.api.system as system_api

    monkeypatch.setattr(
        system_api,
        "settings",
        replace(
            system_api.settings,
            llm_base_url="https://api.mistral.ai/v1",
            llm_model="mistral-large-latest",
        ),
    )
    options = client.get("/api/capabilities").json()["translation_models"]
    values = [item["value"] for item in options["openai_compatible"]]

    assert "mistral-large-latest" in values
    assert not [value for value in values if value.startswith("gpt-")]
    assert not [value for value in values if value.startswith("qwen")]


def test_an_openai_endpoint_offers_openai_models(monkeypatch):
    import backend.api.system as system_api

    monkeypatch.setattr(
        system_api,
        "settings",
        replace(
            system_api.settings,
            llm_base_url="https://api.openai.com/v1",
            llm_model="gpt-4o",
        ),
    )
    values = [
        item["value"]
        for item in client.get("/api/capabilities").json()["translation_models"][
            "openai_compatible"
        ]
    ]

    assert "gpt-4o" in values
    assert not [value for value in values if value.startswith("mistral")]


def test_a_local_endpoint_falls_back_to_the_local_model_list(monkeypatch):
    import backend.api.system as system_api

    monkeypatch.setattr(
        system_api,
        "settings",
        replace(
            system_api.settings,
            llm_base_url="http://localhost:11434/v1",
            llm_model="qwen2.5:7b",
        ),
    )
    values = [
        item["value"]
        for item in client.get("/api/capabilities").json()["translation_models"][
            "openai_compatible"
        ]
    ]

    assert "qwen2.5:7b" in values
    assert "llama3.1:8b" in values


def test_a_model_configured_in_env_is_always_offered(monkeypatch):
    import backend.api.system as system_api

    monkeypatch.setattr(
        system_api,
        "settings",
        replace(
            system_api.settings,
            llm_base_url="https://api.mistral.ai/v1",
            llm_model="ministral-8b-latest",
        ),
    )
    body = client.get("/api/capabilities").json()
    options = body["translation_models"]["openai_compatible"]

    assert options[0]["value"] == "ministral-8b-latest"
    assert options[0]["hint"]["code"] == "model.fromEnv"
    assert body["llm_endpoint"] == "api.mistral.ai"


# ── Dubbing ──────────────────────────────────────────────────────────

DUB_CUES = [
    {"id": 1, "start": 0.0, "end": 3.0, "text": "Hello there", "translation": "Xin chào"},
    {"id": 2, "start": 4.0, "end": 7.0, "text": "Good day", "translation": "Chào buổi sáng"},
]


def dub_job(**fields):
    """A project with dialogue, ready to be read out by the mock voice."""

    return make_job("subtitle_import", subtitle_name="dub.srt", cues=DUB_CUES, **fields)


def test_dubbing_refuses_a_project_with_nothing_to_say():
    job = make_job("subtitle_import", subtitle_name="empty.srt", cues=[])
    try:
        response = client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "mock"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "err.job.noCuesToDub"
    finally:
        cleanup(job["id"])


def test_dubbing_rejects_a_voice_engine_that_does_not_exist():
    job = dub_job()
    try:
        response = client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "nope"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "err.tts.badProvider"
    finally:
        cleanup(job["id"])


def test_dubbing_rejects_an_original_level_outside_the_mix():
    job = dub_job()
    try:
        response = client.post(
            f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "original_gain": 4}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "err.dub.badGain"
    finally:
        cleanup(job["id"])


def test_a_dub_run_produces_a_track_and_says_how_every_line_fitted():
    job = dub_job()
    try:
        started = client.post(
            f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "shorten": False}
        )
        assert started.status_code == 200
        assert started.json()["dubbing_status"] == "pending"

        finished = wait_for_status(job["id"], timeout=60.0)
        assert finished["status"] == "completed", finished.get("error")
        assert finished["dubbing_status"] == "completed"
        assert finished["dub_audio_available"] is True
        assert finished["dubbing_provider"] == "mock"

        report = finished["dubbing_report"]
        assert report["voiced_cues"] == 2
        assert report["failed_cues"] == 0
        assert sum(report["fits"].values()) == 2
    finally:
        cleanup(job["id"])


def test_the_dub_can_be_played_back_before_anything_is_exported():
    job = dub_job()
    try:
        assert client.get(f"/api/jobs/{job['id']}/dub-audio").status_code == 404

        client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "shorten": False})
        assert wait_for_status(job["id"], timeout=60.0)["status"] == "completed"

        played = client.get(f"/api/jobs/{job['id']}/dub-audio")
        assert played.status_code == 200
        assert played.content
    finally:
        cleanup(job["id"])


def test_a_running_dub_will_not_start_a_second_one():
    job = dub_job(status="processing")
    try:
        response = client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "mock"})
        assert response.status_code == 409
    finally:
        cleanup(job["id"])


def test_export_rejects_an_audio_choice_it_does_not_have():
    job = dub_job(video_path=str(RUNTIME_DIR / "nothing.mp4"))
    try:
        response = client.post(f"/api/jobs/{job['id']}/mux?audio=surround")
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "err.dub.badAudioChoice"
    finally:
        cleanup(job["id"])


def test_a_dubbed_export_is_refused_before_the_project_has_a_dub():
    job = dub_job(video_path=str(RUNTIME_DIR / "nothing.mp4"))
    try:
        response = client.post(f"/api/jobs/{job['id']}/mux?audio=dubbed")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "err.dub.noTrack"
    finally:
        cleanup(job["id"])


def test_capabilities_reports_the_voices_this_install_can_use(monkeypatch):
    import backend.api.system as system_api
    import backend.tts as tts_module

    monkeypatch.setattr(
        tts_module, "settings", replace(tts_module.settings, tts_provider="mock")
    )
    monkeypatch.setattr(
        system_api, "settings", replace(system_api.settings, tts_provider="mock")
    )
    body = client.get("/api/capabilities").json()

    assert body["tts_provider"] == "mock"
    assert body["tts_configured"] is True
    assert body["dubbing_configured"] is True
    assert any(voice["value"] == "mock" for voice in body["tts_voices"])


def test_dubbing_rejects_a_fitting_preference_that_does_not_exist():
    job = dub_job()
    try:
        response = client.post(
            f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "prefer": "loud"}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "err.dub.badPreference"
    finally:
        cleanup(job["id"])


def test_the_fitting_preference_can_be_chosen_per_run():
    """Settable per request so both strategies can be compared without a restart."""

    job = dub_job()
    try:
        client.post(
            f"/api/jobs/{job['id']}/dub",
            json={"provider": "mock", "prefer": "natural", "shorten": False},
        )
        finished = wait_for_status(job["id"], timeout=60.0)
        assert finished["status"] == "completed", finished.get("error")
        assert finished["dubbing_report"]["prefer"] == "natural"
    finally:
        cleanup(job["id"])


def test_a_fresh_dub_is_not_reported_as_stale():
    job = dub_job()
    try:
        client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "shorten": False})
        finished = wait_for_status(job["id"], timeout=60.0)
        assert finished["dub_audio_available"] is True
        assert finished["dub_stale"] is False
    finally:
        cleanup(job["id"])


def test_editing_a_line_after_a_dub_marks_the_recording_out_of_date():
    """Otherwise the export quietly ships the take from before the edit."""

    job = dub_job()
    try:
        client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "shorten": False})
        assert wait_for_status(job["id"], timeout=60.0)["dub_stale"] is False

        edited = [dict(cue) for cue in DUB_CUES]
        edited[0]["translation"] = "Chào nhé"
        saved = client.put(f"/api/jobs/{job['id']}/cues", json={"cues": edited})

        assert saved.status_code == 200
        assert saved.json()["dub_stale"] is True
        # The dub is still there to listen to and still exportable — behind a
        # confirm, not a block. It is out of date, not broken.
        assert saved.json()["dub_audio_available"] is True
    finally:
        cleanup(job["id"])


def test_moving_a_cue_after_a_dub_marks_the_recording_out_of_date():
    job = dub_job()
    try:
        client.post(f"/api/jobs/{job['id']}/dub", json={"provider": "mock", "shorten": False})
        wait_for_status(job["id"], timeout=60.0)

        moved = [dict(cue) for cue in DUB_CUES]
        moved[1]["start"] = 4.5
        saved = client.put(f"/api/jobs/{job['id']}/cues", json={"cues": moved})

        assert saved.json()["dub_stale"] is True
    finally:
        cleanup(job["id"])


def test_a_project_that_was_never_dubbed_is_never_stale():
    job = dub_job()
    try:
        body = client.get(f"/api/jobs/{job['id']}").json()
        assert body["dub_audio_available"] is False
        assert body["dub_stale"] is False
    finally:
        cleanup(job["id"])
