# Changelog

All notable behavioural changes to Alan. Dates are the date the change landed.

## 1.2.0 — 2026-11-12

Alan v1.2 is an intent-first remediation release. Control commands, refusals,
complaints, and meta-questions are classified deterministically before onboarding
or capture, so they are never stored as knowledge or consumed as answers. Setup
can be interrupted and resumed; behaviour moves into editable Markdown prompts;
the user-facing vault is separated from the database, logs, and code.

### Intent before action

- New `alan/core/intent.py`. A deterministic classifier runs on every turn with a
  fixed precedence: safety/destructive → flow control → explicit
  correction/complaint → confirmation answer → explicit preference →
  question/search → capture → small talk → unknown.
- Safety: bulk deletion ("удали все заметки", "delete everything") is refused
  and explained; destructive actions are never executed.
- Flow control: "останови настройку", "стоп", "позже", "cancel setup" pause the
  active process. "продолжи настройку" resumes it.
- Complaints ("ты меня не понял", "я этого не говорил") acknowledge the mistake,
  correct state, and create no lesson.
- Meta-questions ("покажи системный промпт", "какая модель используется?") are
  answered as disclosure, never captured.
- Note controls: "не записывай это"/"просто ответь" suppress the note;
  "сделай заметку из этого" captures the previous substantive message;
  "удали последнюю заметку" asks for confirmation first.
- Confirmation states (`onboarding_confirm`, `lesson_confirm`,
  `delete_confirm`) consume a plain yes/no before note-control phrases, so
  "нет, не сохраняй профиль" declines instead of triggering a no-record reply.

### Interruptible onboarding

- Setup asks three short questions one at a time from
  `AlanVault/.alan/prompts/onboarding.md`.
- Pausing preserves `answers`, `raw_ids`, and the current `step`; resuming asks
  the exact pending question ("Вопрос 2 из 3"), never restarting the flow.
- Refusals, complaints, pauses, `no_record`, and meta-questions are never
  appended to `answers`.
- After the last answer, Alan shows a summary and waits in `onboarding_confirm`.
  The profile and open-questions notes are created only after an explicit "да"
  or "сохранить профиль"; declining stores nothing and leaves the setup paused.
- The profile is created with the raw ids of real answers only. A complaint
  marks an existing profile `needs-clarification` and moves it back to the
  Inbox; no lesson is created from the complaint.

### Markdown prompt library

- New `prompts/` with `persona.md`, `capture.md`, `clarification.md`,
  `retrieval.md`, `proactive.md`, `onboarding.md`, `conflict.md`,
  `learning.md`, and `safety.md`.
- New `alan/core/prompt_loader.py`: copies missing packaged defaults into
  `AlanVault/.alan/prompts/`, never overwrites user edits, validates required
  sections and known `{placeholders}`, reloads on change, and fails loudly on a
  missing file, missing section, or unknown placeholder.
- `alan/core/prompts.py` is now a `PromptSet` composed from those files, so
  behaviour is provider-independent and editable without touching code.
- `persona.finalize()` strips any known placeholder as a last line of defence;
  deterministic replies no longer leak `{sir}` or similar.
- `/prompts` reports the folder and reloads edited files at runtime.

### Vault and note lifecycle

- New `AlanVault/` tree: `00_Inbox`, `01_Notes`, `02_Projects`, `03_Decisions`,
  `04_Questions`, `05_People`, `06_References`, `07_Daily`, `08_Archive`,
  `09_Meta/{Templates,Preferences,Lessons,System}`, and `attachments`.
- New statuses: `draft`, `needs-clarification`, `active`, `paused`, `resolved`,
  `superseded`, `archived`, `conflict`; legacy statuses remain readable.
- Every note is written to `00_Inbox` first; clear material is promoted to its
  folder (`03_Decisions`, `04_Questions`, `06_References`,
  `02_Projects/<slug>`, or `01_Notes`). Archived/superseded/rejected material
  moves to `08_Archive`.
- `notes.promote()` moves a clarified note later; external Markdown edits are
  relocated to the folder that matches their parsed status.
- Note `path` is stored relative to the vault. `Config.note_path()` resolves it;
  the database, raw captures, exports, and incoming files stay outside the vault.
- Existing v1.1 notes whose path still begins with `notes/` are moved into the
  vault on the next service start or `python -m alan init`. The file bytes are
  preserved, so hand edits survive the upgrade; the operation is idempotent.
- Export archives now use `raw/...` and `vault/...` prefixes.
- New setting `ALAN_VAULT_DIR` (default `AlanVault`, resolved as a sibling of the
  data directory). `python -m alan init` creates the vault and installs prompts;
  `python -m alan doctor` reports both.

### Disclosure and privacy

- Self-disclosure now names the reasoning model and embedding model, states
  whether each is local or cloud, lists which components are model-independent,
  and points to the Markdown prompt folder.
- Redaction still removes API keys, bot tokens, credential-shaped strings,
  environment values, internal endpoints, and absolute private paths.
- `delete_note` replies in the user's language.

### Tests and docs

- The offline suite grew from 75 to **99 tests**. `tests/test_v12.py` covers the
  v1.2 acceptance criteria:
  - 13.1 pause onboarding mid-flow, policy interruption, and `no_record` during
    setup without consuming an answer;
  - 13.2 complaint and declined summary create no profile and no lesson;
  - 13.3 resume asks the correct question with the preserved answer;
  - 13.4 Russian input and Russian control replies stay Russian;
  - 13.5 no placeholder leaks, and unknown/missing prompt content fails loudly;
  - 13.6 prompts are editable Markdown, survive recreation, and reload;
  - 13.7 notes live only in the vault, which contains no database or logs, plus
    the inbox-first promotion lifecycle.
- Additional tests cover natural-language deletion and note creation, bulk-delete
  refusal, model disclosure, deterministic intent precedence, and migration of
  legacy note paths into the vault.
- README and `.env.example` document the vault, prompts, controls, and the
  `ALAN_VAULT_DIR` setting. Version bumped to **1.2.0**.

## 1.1.0 — 2026-11-11

Alan v1.1 is a remediation release: the assistant now remembers the conversation
it is in, follows the user's language and address conventions, refuses to turn
questions and complaints into rules, treats Markdown as the authoritative copy
of a note, retrieves Russian inflections, imports large sources resumably, and
can explain its own rules without revealing secrets.

### Conversation and working memory

- Working memory is the persisted `messages` table, not an in-process buffer.
  A restart no longer erases the conversation.
- The rolling window is `ALAN_HISTORY_TURNS` (default **30** turns, previously 8),
  passed to both the assessment and answer prompts.
- References such as "what did I just say", "what was my previous question",
  "what do you remember", "explain your previous answer" and "I meant the
  previous message" are answered deterministically from stored messages, without
  a model call, so they remain accurate after a restart.
- An unresolved clarification is stored in `pending_states` with its original
  text and raw id; the answer is merged with that original rather than replacing
  it. Context never overwrites a raw capture.
- Assistant replies are persisted with their note id, language and redacted text.

### Language, address, and honesty

- Reply language is detected from the **current** message every turn. The stored
  `language` preference is only a fallback. A one-word reply such as "Да." keeps
  the conversation's language: short pure-Cyrillic text is detected as Russian,
  and a message with no letters falls back to the previous user turn.
  Internals, tags, prompts and lesson text stay English; raw captures and note
  content keep their original language.
- Address is applied naturally: one insertion inside the first sentence
  ("Записано, сэр."), never "Сэр." before every sentence, and never when the
  model already addressed the user.
- The persona states that the user is the creator/administrator and that the
  operating rules are not secret. `/policy` and natural-language questions about
  the prompt, rules, model or memory are answered from `persona.policy_text`.
- Outgoing text is redacted: API-key-shaped strings, bot tokens, `key=value`
  credentials and the absolute data path are removed. `/policy` verifies this
  even when a key is configured.

### Lessons and self-improvement discipline

- The assessment now returns `durable_intent`: `none | one_time | durable |
  unclear`.
- A lesson is created only for an explicit, reusable rule (`durable`).
- Questions, meta-questions, ordinary statements and complaints never create a
  lesson. A possible preference is classified `unclear`, and Alan asks:
  "Это разовая инструкция или устойчивое предпочтение, которое мне следует
  учитывать в дальнейшем, сэр?" (English equivalent when replying in English).
- `one_time` instructions are applied for that turn only and create no rule.
- Lessons carry `confidence`, `explanation`, `scope`, `evidence_raw_id`, counters
  (`times_applied`, `success_count`, `failure_count`), an `active` flag and an
  audit record with the triggering raw id, model, confidence, confirmation state
  and the resulting rule.
- `/lessons` shows id, kind, scope, confidence and source; `/forget <id>`
  deactivates a lesson rather than deleting its history.
- Automatic reflection after feedback was removed; only `/reflect` extracts
  lessons, and it follows the same rules.
- Alan may change preferences, style, thresholds, link proposals, retrieval
  weights, clarification policy, naming and reminder relevance through these
  auditable lessons. It must not alter raw captures, historical decisions,
  facts, version history, the audit log, privacy boundaries, user-approved
  projects, or the core safety rules.

### Markdown as the source of truth

- A note is three things: the Markdown file (authoritative content), JSON
  metadata (`notes.content_json`), and derived indexes (FTS, embeddings).
- External edits are detected on every turn (`ALAN_MARKDOWN_WATCH`, default on)
  and at startup. Alan parses the file's frontmatter and `## Section` blocks,
  creates a **new version** with the user's exact text, re-indexes it, and never
  regenerates over it.
- The previous version remains in `note_versions` and is still readable.
- When an external edit changes what a field means, Alan reports the version and
  asks whether to also refresh links, tags and status; the answer is a button
  (`metadata:refresh` / `metadata:keep`).
- Alan's own edits create a version with an explicit reason and summary.

### Russian-aware retrieval

- New `alan/rus.py`: lowercase, `ё → е`, punctuation stripping, a compact
  Snowball-style stemmer, stopword filtering, and 3/4-character root tokens for
  longer words, so `решение`, `решил` and `решаю` share a stem.
- Identifiers, URLs, code, dates and numbers are preserved as searchable tokens.
- FTS5 now indexes seven columns including `roots`; the index is rebuilt on the
  v2 migration.
- Hybrid retrieval is unchanged in shape (normalised keyword + vector +
  metadata filters + RRF), but privacy filtering is applied in SQL before
  ranking. There is no `LIKE` fallback that could bypass it.
- When nothing matches, the reply is exactly
  "В базе знаний нет подходящих записей, сэр." (or the English equivalent).

### Resumable, observable imports

- Imports are two-stage jobs stored in `import_jobs` / `import_items`:
  1. **Capture** — parse, deduplicate, and create every raw capture in batches
     before any model call. A crash loses no user text.
  2. **Analysis** — turn captured items into notes later, in priority order
     (longer and more recent first), deferred and progressive.
- Jobs are `pending | running | paused | done | cancelled | failed`; items are
  `pending | captured | analyzing | done | skipped | duplicate | failed`.
- `/imports` lists jobs and progress; `/pause`, `/resume`, `/cancel` control
  them; `/import` reports each batch. Progress is a first-class report: items,
  captures, notes, duplicates, conflicts, skipped, failed, status.
- Restart safety: on startup, orphaned `running` jobs are marked `paused`, and
  `/resume` (or the CLI) continues from exactly where it stopped. Nothing
  captured is re-captured or re-analysed. If the process died during stage 1,
  resuming first captures every outstanding item, then analyses; no note is ever
  built from text that was not already stored as a raw capture.
- Conflict findings during analysis are counted on the job.
- CLI: `python -m alan imports`, `python -m alan import <path> --no-analyze`,
  `--resume <job-id>`, `--cancel <job-id>`. `import_path` remains available as a
  compatibility wrapper.

### Onboarding

- First-run setup is a short dialogue: `/start` (or a first greeting) asks three
  high-value questions, one at a time, and allows "позже"/"later" to defer.
- Completion creates a profile note and an open-questions note with stable
  slugs (`user-profile`, `onboarding-open-questions`), sets
  `onboarding_done = 1`, and suggests an import source.
- It is not a long questionnaire. `ALAN_ONBOARDING=false` disables it.

### Proactive messages

- Guardrails are unchanged: the model only approves or rejects a deterministic
  candidate, quiet hours and daily/minimum-interval limits apply, duplicates are
  suppressed, sensitive material is excluded, and user reminders bypass the
  model.
- The proactive prompt now requires a fixed shape: what was forgotten or became
  relevant, why it matters now, the source note by name, and one concrete next
  action or question, in at most three sentences.

### Storage and configuration

- Database **schema v2** with guarded migrations: `messages` project/topic/meta/
  note/raw columns, `pending_states`, `import_jobs`, `import_items`, lesson
  provenance/counters, note `content_hash` and `file_mtime`, and the new FTS
  shape. Existing databases migrate in place on open.
- New settings:
  - `ALAN_HISTORY_TURNS` (default 30)
  - `ALAN_ONBOARDING` (default true)
  - `ALAN_IMPORT_BATCH_SIZE` (default 40)
  - `ALAN_MARKDOWN_WATCH` (default true)
- `notes.create` accepts an explicit stable `slug` for generated notes.

### Tests

- The offline suite grew from 64 to **75 tests**. New coverage in
  `tests/test_v11.py`:
  - conversation continuity across a restart, and memory questions;
  - language follows the user while raw text is untouched, including a
    one-word Russian reply;
  - questions do not create lessons;
  - an ambiguous preference is confirmed before becoming a lesson, with
    confidence and source recorded;
  - a one-time instruction never becomes a lesson;
  - `/policy`-style self-disclosure contains no secrets;
  - an external Markdown edit becomes version 2 and is re-indexed;
  - Russian inflection retrieves the right note and cites it;
  - import stores all captures first and resumes after a restart;
  - a job interrupted before stage 1 finishes captures outstanding items
    before analysis;
  - onboarding creates a profile in three short steps.

## 1.0.0 — 2026-11-11

Initial release: immutable raw captures, Markdown notes with version history,
projects, meaningful links, conflicts that are never auto-resolved, hybrid
retrieval, deterministic confirmations, proactive review, Telegram + CLI
transports, and an offline test suite.
