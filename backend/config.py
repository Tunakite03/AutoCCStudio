import logging
from dataclasses import dataclass
from os import environ, getenv
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "frontend"
RUNTIME_DIR = ROOT_DIR / "runtime"


def _load_local_env() -> None:
    """Load the small .env file without adding a runtime dependency."""

    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in environ:
            environ[key] = value


_load_local_env()


def _int_env(name: str, default: int) -> int:
    try:
        return int(getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(getenv(name, str(default)))
    except ValueError:
        return default


def parse_api_keys(raw: str) -> tuple[str, ...]:
    """Read one *or many* API keys out of a single env value.

    `.env` has no list type, so a pool is written the way a person would write
    one: `LLM_API_KEY=[key-a, key-b]`. A lone key stays a lone key.

    Duplicates are dropped: the same credential listed twice is one quota
    pretending to be two, and it would take a rotation slot from a real key.
    """

    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    keys: list[str] = []
    for chunk in text.replace("\n", ",").replace(";", ",").split(","):
        key = chunk.strip().strip('"').strip("'").strip()
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


@dataclass(frozen=True)
class Settings:
    transcription_provider: str = getenv("TRANSCRIPTION_PROVIDER", "faster_whisper")
    whisper_model: str = getenv("WHISPER_MODEL", "small")
    whisper_device: str = getenv("WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = getenv("WHISPER_COMPUTE_TYPE", "int8")
    deepgram_api_key: str = getenv("DEEPGRAM_API_KEY", "")
    deepgram_base_url: str = getenv("DEEPGRAM_BASE_URL", "https://api.deepgram.com")
    deepgram_model: str = getenv("DEEPGRAM_MODEL", "nova-3")
    deepgram_diarize_model: str = getenv("DEEPGRAM_DIARIZE_MODEL", "latest")
    deepgram_timeout_seconds: int = _int_env("DEEPGRAM_TIMEOUT_SECONDS", 900)
    translation_provider: str = getenv("TRANSLATION_PROVIDER", "openai_compatible")
    translation_model: str = getenv("TRANSLATION_MODEL", "")
    transformers_target_language: str = getenv(
        "TRANSFORMERS_TARGET_LANGUAGE", "Tiếng Việt"
    )
    transformers_device: str = getenv("TRANSFORMERS_DEVICE", "auto")
    llm_base_url: str = getenv("LLM_BASE_URL", "http://localhost:11434/v1")
    llm_api_key: str = getenv("LLM_API_KEY", "")
    llm_model: str = getenv("LLM_MODEL", "qwen2.5:7b")
    llm_timeout_seconds: int = _int_env("LLM_TIMEOUT_SECONDS", 180)
    # Seconds to keep between two LLM calls. Hosted providers meter requests per
    # second, and a translation is a long burst of small calls; 0 leaves a local
    # model running flat out.
    llm_min_interval_seconds: float = _float_env("LLM_MIN_INTERVAL_SECONDS", 0.0)
    speaker_analysis_model: str = getenv("SPEAKER_ANALYSIS_MODEL", "")
    ffmpeg_binary: str = getenv("FFMPEG_BINARY", "ffmpeg")
    max_upload_mb: int = _int_env("MAX_UPLOAD_MB", 2048)
    log_level: str = getenv("LOG_LEVEL", "INFO")
    # One transcription already saturates a CPU; a second queues rather than
    # thrashing. Raise it only when the providers are remote.
    max_concurrent_jobs: int = _int_env("MAX_CONCURRENT_JOBS", 2)
    # Each cached Whisper model holds its weights in RAM (large-v3 int8 ≈ 1.5 GB),
    # so the default trades a reload on model switch for a smaller footprint.
    whisper_model_cache: int = _int_env("WHISPER_MODEL_CACHE", 1)
    http_retries: int = _int_env("HTTP_RETRIES", 2)
    # A rate limit needs to be waited out, not retried a couple of times, so it
    # gets a budget of its own.
    http_rate_limit_retries: int = _int_env("HTTP_RATE_LIMIT_RETRIES", 5)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def llm_api_keys(self) -> tuple[str, ...]:
        """Every LLM key configured, in the order they were written.

        Derived rather than stored so `dataclasses.replace(settings, ...)` — how
        the tests and every override path build a variant — cannot leave the raw
        value and the parsed pool disagreeing.
        """

        return parse_api_keys(self.llm_api_key)


settings = Settings()


def _configure_logging(level_name: str) -> logging.Logger:
    """Give the app its own stderr handler.

    uvicorn only configures the `uvicorn.*` loggers, so without a handler here
    everything below WARNING falls through to `logging.lastResort` and is lost.
    """

    logger = logging.getLogger("autocc")
    logger.setLevel(getattr(logging, level_name.strip().upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    return logger


_APP_LOGGER = _configure_logging(settings.log_level)


def get_logger(name: str) -> logging.Logger:
    """Return the `autocc.<name>` child logger."""

    return _APP_LOGGER.getChild(name)
