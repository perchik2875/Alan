"""Configuration, loaded from the environment.

Every setting has an ``ALAN_``-prefixed environment variable.  Nothing here
is model-specific: the provider name is a value, not a dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigError


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(slots=True)
class LLMConfig:
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.2
    max_tokens: int = 2400
    timeout: float = 90.0
    max_retries: int = 2


@dataclass(slots=True)
class EmbeddingConfig:
    provider: str = "none"
    model: str = "text-embedding-3-small"
    api_key: str = ""
    base_url: str = ""
    timeout: float = 60.0
    dimensions: int = 0


@dataclass(slots=True)
class ProactiveConfig:
    enabled: bool = True
    poll_minutes: int = 30
    quiet_start: int = 22
    quiet_end: int = 9
    max_messages_per_day: int = 1
    min_hours_between: int = 20
    stale_project_days: int = 14
    open_question_days: int = 10
    conflict_days: int = 2
    due_soon_hours: int = 48
    min_candidate_age_days: int = 2


VAULT_FOLDERS = (
    "00_Inbox",
    "01_Notes",
    "02_Projects",
    "03_Decisions",
    "04_Questions",
    "05_People",
    "06_References",
    "07_Daily",
    "08_Archive",
    "09_Meta",
    "09_Meta/Templates",
    "09_Meta/Preferences",
    "09_Meta/Lessons",
    "09_Meta/System",
    "attachments",
)


@dataclass(slots=True)
class Config:
    telegram_token: str = ""
    owner_ids: list[int] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: Path("data"))
    db_path: Path = field(default_factory=lambda: Path("data/alan.db"))
    # The vault is the user-facing Markdown workspace.  It is deliberately
    # separate from the database, raw captures, logs, and credentials.
    vault_dir: Path = field(default_factory=lambda: Path("AlanVault"))
    timezone: str = "UTC"
    llm: LLMConfig = field(default_factory=LLMConfig)
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    clarify_ttl_minutes: int = 45
    history_turns: int = 30
    search_top_k: int = 6
    answer_context_k: int = 8
    default_language: str = "en"
    onboarding_enabled: bool = True
    import_batch_size: int = 40
    markdown_watch: bool = True
    log_level: str = "INFO"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def notes_dir(self) -> Path:
        """Legacy default note folder; notes now live in the vault inbox."""

        return self.vault_dir / "00_Inbox"

    @property
    def prompts_dir(self) -> Path:
        return self.vault_dir / ".alan" / "prompts"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    def note_path(self, relative: str | Path) -> Path:
        """Resolve a note path, which is stored relative to the vault."""

        return self.vault_dir / relative

    def resolve_paths(self) -> None:
        self.data_dir = self.data_dir.resolve()
        if not self.db_path.is_absolute():
            self.db_path = (self.data_dir / self.db_path.name).resolve()
        if not self.vault_dir.is_absolute():
            self.vault_dir = (self.data_dir.parent / self.vault_dir).resolve()

    def ensure_dirs(self) -> None:
        self.resolve_paths()
        for path in (
            self.data_dir,
            self.raw_dir,
            self.export_dir,
            self.incoming_dir,
            self.db_path.parent,
            self.vault_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for folder in VAULT_FOLDERS:
            (self.vault_dir / folder).mkdir(parents=True, exist_ok=True)
        self.prompts_dir.mkdir(parents=True, exist_ok=True)


def load_config(env_file: str | os.PathLike[str] | None = ".env") -> Config:
    """Build a Config from the environment (and an optional .env file)."""

    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    data_dir = Path(_env("ALAN_DATA_DIR", "data") or "data")

    cfg = Config(
        telegram_token=_env("ALAN_TELEGRAM_TOKEN", "") or "",
        owner_ids=[int(x) for x in _env_list("ALAN_OWNER_IDS") if x.lstrip("-").isdigit()],
        data_dir=data_dir,
        db_path=Path(_env("ALAN_DB_PATH", "") or (data_dir / "alan.db")),
        vault_dir=Path(_env("ALAN_VAULT_DIR", "") or "AlanVault"),
        timezone=_env("ALAN_TIMEZONE", "UTC") or "UTC",
        clarify_ttl_minutes=_env_int("ALAN_CLARIFY_TTL_MINUTES", 45),
        history_turns=_env_int("ALAN_HISTORY_TURNS", 30),
        search_top_k=_env_int("ALAN_SEARCH_TOP_K", 6),
        answer_context_k=_env_int("ALAN_ANSWER_CONTEXT_K", 8),
        default_language=_env("ALAN_DEFAULT_LANGUAGE", "en") or "en",
        onboarding_enabled=_env_bool("ALAN_ONBOARDING", True),
        import_batch_size=_env_int("ALAN_IMPORT_BATCH_SIZE", 40),
        markdown_watch=_env_bool("ALAN_MARKDOWN_WATCH", True),
        log_level=_env("ALAN_LOG_LEVEL", "INFO") or "INFO",
        llm=LLMConfig(
            provider=(_env("ALAN_LLM_PROVIDER", "openai") or "openai").lower(),
            model=_env("ALAN_LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
            api_key=_env("ALAN_LLM_API_KEY", "") or "",
            base_url=(_env("ALAN_LLM_BASE_URL", "") or "").rstrip("/"),
            temperature=_env_float("ALAN_LLM_TEMPERATURE", 0.2),
            max_tokens=_env_int("ALAN_LLM_MAX_TOKENS", 2400),
            timeout=_env_float("ALAN_LLM_TIMEOUT", 90.0),
            max_retries=_env_int("ALAN_LLM_MAX_RETRIES", 2),
        ),
        embeddings=EmbeddingConfig(
            provider=(_env("ALAN_EMBED_PROVIDER", "none") or "none").lower(),
            model=_env("ALAN_EMBED_MODEL", "text-embedding-3-small") or "text-embedding-3-small",
            api_key=_env("ALAN_EMBED_API_KEY", "") or "",
            base_url=(_env("ALAN_EMBED_BASE_URL", "") or "").rstrip("/"),
            timeout=_env_float("ALAN_EMBED_TIMEOUT", 60.0),
            dimensions=_env_int("ALAN_EMBED_DIMENSIONS", 0),
        ),
        proactive=ProactiveConfig(
            enabled=_env_bool("ALAN_PROACTIVE_ENABLED", True),
            poll_minutes=_env_int("ALAN_PROACTIVE_POLL_MINUTES", 30),
            quiet_start=_env_int("ALAN_PROACTIVE_QUIET_START", 22),
            quiet_end=_env_int("ALAN_PROACTIVE_QUIET_END", 9),
            max_messages_per_day=_env_int("ALAN_PROACTIVE_MAX_PER_DAY", 1),
            min_hours_between=_env_int("ALAN_PROACTIVE_MIN_HOURS", 20),
            stale_project_days=_env_int("ALAN_STALE_PROJECT_DAYS", 14),
            open_question_days=_env_int("ALAN_OPEN_QUESTION_DAYS", 10),
            conflict_days=_env_int("ALAN_CONFLICT_DAYS", 2),
            due_soon_hours=_env_int("ALAN_DUE_SOON_HOURS", 48),
            min_candidate_age_days=_env_int("ALAN_MIN_CANDIDATE_AGE_DAYS", 2),
        ),
    )

    # Embedding credentials default to the main provider when compatible.
    if cfg.embeddings.provider in {"openai", "gemini"} and not cfg.embeddings.api_key:
        if cfg.llm.provider == cfg.embeddings.provider:
            cfg.embeddings.api_key = cfg.llm.api_key
    if cfg.embeddings.provider == "openai" and not cfg.embeddings.base_url:
        cfg.embeddings.base_url = cfg.llm.base_url

    cfg.resolve_paths()
    return cfg


def require_telegram_token(cfg: Config) -> str:
    if not cfg.telegram_token:
        raise ConfigError(
            "ALAN_TELEGRAM_TOKEN is not set. Create a bot with @BotFather and put the "
            "token in .env (see .env.example)."
        )
    return cfg.telegram_token


def require_llm(cfg: Config) -> LLMConfig:
    if cfg.llm.provider not in {"openai", "anthropic", "gemini"}:
        raise ConfigError(
            f"Unknown ALAN_LLM_PROVIDER={cfg.llm.provider!r}; expected openai, anthropic or gemini."
        )
    if cfg.llm.provider in {"openai", "anthropic", "gemini"} and not cfg.llm.api_key:
        raise ConfigError("ALAN_LLM_API_KEY is not set.")
    return cfg.llm
