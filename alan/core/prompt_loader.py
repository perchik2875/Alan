"""Load, validate, and render the Markdown prompt library.

Prompt text is product behaviour, not code.  It lives as readable Markdown in
``<vault>/.alan/prompts`` and is copied there from the packaged defaults on
first use.  The loader is intentionally strict: a missing file, a missing
required section, an unknown placeholder, or an unsubstituted placeholder is a
loud failure rather than a silently degraded prompt.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import AlanError

log = logging.getLogger("alan.prompts")

PROMPT_NAMES = (
    "persona",
    "capture",
    "clarification",
    "retrieval",
    "proactive",
    "onboarding",
    "conflict",
    "learning",
    "safety",
)

REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "persona": ("Identity", "Tone", "Language", "Memory", "Honesty"),
    "capture": ("Classification", "Durable intent", "Drafting", "Output"),
    "clarification": ("When to ask", "When not to ask"),
    "retrieval": ("Answering", "Output"),
    "proactive": ("Send", "Output"),
    "onboarding": (
        "Intro (ru)", "Intro (en)",
        "Questions (ru)", "Questions (en)",
        "Pause (ru)", "Pause (en)",
        "Resume (ru)", "Resume (en)",
        "Summary (ru)", "Summary (en)",
        "Complaint (ru)", "Complaint (en)",
        "Declined (ru)", "Declined (en)",
    ),
    "conflict": ("Rules",),
    "learning": ("Lesson rules", "Format"),
    "safety": ("Privacy", "Disclosure", "Controls"),
}

# Only these names may appear as {placeholders} in the Markdown.  Anything else
# is a typo or a leak, and loading fails loudly.
KNOWN_VARIABLES = {
    "sir",
    "resume_command",
    "step",
    "total",
    "directions",
    "sources",
    "question",
    "answer",
    "user",
    "model",
    "provider",
    "embedding",
    "detail",
    "memory",
    "date",
    "timezone",
    "vault_path",
    "prompts_path",
}

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class PromptError(AlanError):
    """The prompt library is missing, malformed, or unresolved."""


@dataclass(slots=True)
class PromptFile:
    name: str
    path: Path
    text: str
    mtime: float
    sections: dict[str, str]


def _parse_sections(text: str) -> dict[str, str]:
    matches = list(_SECTION.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[start:end].strip()
    return sections


def packaged_prompts_dir() -> Path:
    """The prompts shipped with the code (repo root ``prompts/``)."""

    return Path(__file__).resolve().parents[2] / "prompts"


class PromptLibrary:
    def __init__(self, root: Path, *, packaged: Path | None = None) -> None:
        self.root = Path(root)
        self.packaged = Path(packaged) if packaged is not None else packaged_prompts_dir()
        self._files: dict[str, PromptFile] = {}
        self._loaded = False

    # -- lifecycle -----------------------------------------------------

    def ensure(self) -> None:
        """Create the user-editable prompt directory from packaged defaults.

        Existing files are never overwritten: the user owns them after the
        first copy.
        """

        self.root.mkdir(parents=True, exist_ok=True)
        for name in PROMPT_NAMES:
            target = self.root / f"{name}.md"
            if target.exists():
                continue
            source = self.packaged / f"{name}.md"
            if not source.exists():
                raise PromptError(
                    f"Packaged prompt is missing: {source}. The prompt library cannot be installed."
                )
            shutil.copyfile(source, target)
        if not self._loaded:
            self.reload()

    def reload(self) -> None:
        files: dict[str, PromptFile] = {}
        for name in PROMPT_NAMES:
            path = self.root / f"{name}.md"
            if not path.exists():
                raise PromptError(
                    f"Required prompt file is missing: {path}. Restore it from {self.packaged}."
                )
            try:
                text = path.read_text(encoding="utf-8")
                mtime = path.stat().st_mtime
            except OSError as exc:
                raise PromptError(f"Cannot read prompt file {path}: {exc}") from exc
            sections = _parse_sections(text)
            missing = [s for s in REQUIRED_SECTIONS[name] if s not in sections]
            if missing:
                raise PromptError(
                    f"Prompt {path.name} is missing required section(s): {', '.join(missing)}."
                )
            unknown = {
                token
                for token in _PLACEHOLDER.findall(text)
                if token not in KNOWN_VARIABLES
            }
            if unknown:
                raise PromptError(
                    f"Prompt {path.name} uses unknown placeholder(s): "
                    f"{', '.join(sorted(unknown))}. Add them to KNOWN_VARIABLES or fix the text."
                )
            files[name] = PromptFile(
                name=name, path=path, text=text, mtime=mtime, sections=sections
            )
        self._files = files
        self._loaded = True

    def reload_if_changed(self) -> bool:
        if not self._loaded:
            self.reload()
            return True
        try:
            current = {name: (self.root / f"{name}.md").stat().st_mtime for name in PROMPT_NAMES}
        except OSError:
            self.reload()
            return True
        if current != {name: f.mtime for name, f in self._files.items()}:
            self.reload()
            return True
        return False

    # -- access --------------------------------------------------------

    def file(self, name: str) -> PromptFile:
        if name not in PROMPT_NAMES:
            raise PromptError(f"Unknown prompt: {name}")
        if not self._loaded:
            self.ensure()
        return self._files[name]

    def section(self, name: str, section: str) -> str:
        file = self.file(name)
        if section not in file.sections:
            raise PromptError(f"Prompt {name}.md has no section {section!r}.")
        return file.sections[section]

    def render(self, name: str, *, section: str | None = None, **values: object) -> str:
        template = self.section(name, section) if section else self.file(name).text
        missing = sorted(
            {
                token
                for token in _PLACEHOLDER.findall(template)
                if token not in values
            }
        )
        if missing:
            raise PromptError(
                f"Prompt {name}.md is missing value(s) for: {', '.join(missing)}."
            )
        rendered = _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), template)
        leftover = _PLACEHOLDER.findall(rendered)
        if leftover:
            raise PromptError(
                f"Prompt {name}.md rendered with unresolved placeholder(s): "
                f"{', '.join(sorted(set(leftover)))}."
            )
        return rendered


_LIBRARIES: dict[str, PromptLibrary] = {}


def library_for(root: str | Path, *, packaged: str | Path | None = None) -> PromptLibrary:
    """Return a cached library for a prompt directory, reloading edits."""

    key = str(Path(root).resolve())
    library = _LIBRARIES.get(key)
    if library is None:
        library = PromptLibrary(
            Path(root), packaged=Path(packaged) if packaged is not None else None
        )
        library.ensure()
        _LIBRARIES[key] = library
    else:
        library.reload_if_changed()
    return library


def prompts_library(cfg) -> PromptLibrary:
    """Library for a Config, installed into ``<vault>/.alan/prompts``."""

    return library_for(cfg.prompts_dir)


def reload_prompts(cfg=None) -> None:
    if cfg is None:
        for library in _LIBRARIES.values():
            library.reload()
    else:
        library_for(cfg.prompts_dir).reload()
