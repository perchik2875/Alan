# Alan

A private, context-aware knowledge assistant. Alan captures what you say, preserves it verbatim, turns it into durable Markdown notes, connects related records, and returns the right context when you ask — in your language, without asking you to organize anything.

Alan is not a notes app with an AI feature bolted on. The knowledge base is the product; the model is a replaceable reasoning component. Since **v1.2** it is intent-first: control commands, refusals, complaints and meta-questions are handled before anything is stored, setup can be interrupted at any point, behaviour lives in editable Markdown prompts, and the user-facing vault is separate from the database and code.

---

## What Alan guarantees

| Guarantee | How it is enforced |
| --- | --- |
| **Nothing important is lost** | Every message is written to an immutable raw capture (SQLite row + Markdown file) *before* the model sees it. Database triggers reject updates; deletion requires an explicit user action and is audited. |
| **Intent before action** | A deterministic classifier runs first: safety, flow control, complaints, refusals, meta-questions and note controls win over onboarding and capture. "Не записывай это", "останови настройку", "покажи системный промпт" and "удали последнюю заметку" never become knowledge. |
| **The conversation is remembered** | Working memory is the persisted `messages` table, so "what did I just say?" survives a restart. Questions about the conversation are answered from storage, not guessed by the model. |
| **You do not have to organize** | No folders, tags, or templates at capture time. Alan derives type, project, tags and links, and asks at most two questions only when a note would otherwise be wrong. |
| **Your language, not the internals'** | Alan replies in the language of your latest message. Prompts, tags and lesson text stay English; your words and note content keep their original language. |
| **Markdown is authoritative** | Notes live in `AlanVault/`. If you edit one by hand, Alan versions and re-indexes your edit instead of overwriting it, then asks about links, tags and status. |
| **Context survives a model change** | Notes, links, conflicts, lessons, preferences, reminders and history live in SQLite and Markdown. Providers implement one small interface; nothing provider-specific is stored. |
| **Answers are evidence-bound** | Answers cite note ids and distinguish fact, inference, assumption and unknown. Conflicting records are shown side by side, never silently resolved. |
| **Alan does not flatter** | The persona forbids praise, automatic agreement and hedging. Disagreement follows a fixed structure: position, reason, evidence, strongest counter-argument, and a question. |
| **Alan can explain itself** | `/policy` shows the rules it actually runs on, including model and memory configuration. Nothing is hidden from the administrator except credentials and private paths. |
| **Setup is interruptible** | First-run setup can be paused at any question and resumed later from the same question. Partial answers are preserved; a refusal, complaint or meta-question is never counted as an answer, and no profile is saved without an explicit confirmation. |
| **Behaviour is editable Markdown** | Persona, capture, clarification, retrieval, proactive, onboarding, conflict, learning and safety rules live in `AlanVault/.alan/prompts/*.md`. Edit them, then `/prompts` reloads them. The loader fails loudly on a missing file, section, or unknown placeholder. |
| **The vault is isolated** | Notes and prompts live in `AlanVault/`, which is Obsidian-compatible and contains no database, logs, code, or credentials. |
| **Private stays private** | Sensitive notes are marked, removed from both indexes, and excluded from search, summaries and proactive messages. |

---

## Quick start

Requires **Python 3.11+**.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements-dev.txt

cp .env.example .env              # then edit it
```

Minimum configuration in `.env`:

```dotenv
ALAN_TELEGRAM_TOKEN=123456:ABC...
ALAN_OWNER_IDS=123456789          # your Telegram user id

ALAN_LLM_PROVIDER=openai
ALAN_LLM_MODEL=gpt-4o-mini
ALAN_LLM_API_KEY=sk-...
```

Then:

```bash
python -m alan init               # create data dirs, vault, prompts, database
python -m alan doctor --ping      # verify config, storage, model, embeddings
python -m alan bot                # start the bot
```

Open Telegram, send `/start`, then send a thought. On the first run Alan asks three short setup questions — its main areas, active projects, and what it most often loses — one at a time. You can pause at any point with "останови настройку" / "stop setup", ask a question, or say "позже"/"later"; the setup resumes from the correct question with "продолжи настройку" / "continue setup". Nothing is saved to a profile until you confirm the summary.

### Switching models

Change three environment variables and restart. Nothing else changes.

```dotenv
# Anthropic
ALAN_LLM_PROVIDER=anthropic
ALAN_LLM_MODEL=claude-sonnet-4-5
ALAN_LLM_API_KEY=sk-ant-...

# Gemini
ALAN_LLM_PROVIDER=gemini
ALAN_LLM_MODEL=gemini-3.5-flash-lite
ALAN_LLM_API_KEY=...
ALAN_EMBED_PROVIDER=gemini
ALAN_EMBED_MODEL=gemini-embedding-001
ALAN_EMBED_DIMENSIONS=1536

# Any OpenAI-compatible endpoint (OpenRouter, DeepSeek, vLLM, LM Studio…)
ALAN_LLM_PROVIDER=openai
ALAN_LLM_BASE_URL=https://openrouter.ai/api/v1
ALAN_LLM_MODEL=deepseek/deepseek-chat
ALAN_LLM_API_KEY=sk-or-...
```

---

## Using Alan

Send anything. Alan decides what it is.

```
You:  I need to figure out my learning plan. Backend seems more important,
      but AI might give better growth. Not sure how to combine it with work.

Alan: Understood, sir. Two clarifications:
      1. Should backend remain the primary track, or should AI replace it?
      2. Is this a decision for the current learning plan or a new long-term direction?

You:  Backend primary, AI parallel. Current plan.

Alan: Recorded, sir.
      Decision: Keep backend as the primary track; add AI as a parallel track.
      Linked to: Learning plan.
      Source: raw capture from 11 November, 14:24.
```

Questions are answered from the base, with sources:

```
You:  What are my current directions?

Alan: Three active directions, sir:
      - Backend: in progress
      - AI: plan is forming
      - Finance: no activity for 12 days

      Sources:
      - Backend vs AI learning priorities · `backend-vs-ai-learning-priorities`
```

Corrections are first-class:

```
You:  No, that was not a decision. It was just an idea.

Alan: Updated, sir.
      Idea: Explore whether AI should replace backend.
      ...
```

The note is reclassified, the previous version is preserved, and a generalisable lesson is written so the same misclassification is not repeated.

The conversation itself is part of the memory. You can refer back to it:

```
You:  Add to that: AI is parallel, not a replacement.

Alan: Updated, sir.
      Decision: Backend stays primary; AI is a parallel track.
      ...

You:  What did I just say?

Alan: Your previous message: «Add to that: AI is parallel, not a replacement.»

You:  What do you remember?

Alan: In this conversation I remember 4 previous messages. The knowledge base
      holds 12 notes and 31 raw captures. ...
```

That works after a restart too: the turns are rows in SQLite, not an in-memory buffer.

### What Alan will not learn from

A question, a complaint, or a remark about how Alan works never becomes a rule. If something might be a lasting preference, Alan asks first:

```
You:  Your answers are too long.

Alan: Is that a one-time instruction, or a lasting preference I should apply
      from now on, sir?
```

Only an explicit, reusable preference becomes a lesson — with its source, scope and confidence recorded. `/lessons` shows them; `/forget <id>` deactivates one. "Answer this one in detail" is applied once and creates no rule.

### Commands

| Command | Purpose |
| --- | --- |
| `/ask <question>` | Answer from the knowledge base |
| `/find <terms>` | Hybrid search (exact + meaning) |
| `/notes [type]` | Recent notes |
| `/note <id\|slug\|title>` | Open a note |
| `/raw <note>` | The original captures behind a note |
| `/versions <note>` | Version history |
| `/projects`, `/project <name>` | Project overview |
| `/conflicts` | Unresolved contradictions |
| `/questions` | Open questions |
| `/status` | Knowledge base statistics |
| `/review` | Run a proactive review immediately |
| `/reflect` | Extract lessons from recent turns (explicit request only) |
| `/lessons`, `/forget <id>` | Inspect and revoke learned lessons |
| `/policy` | Explain the operating rules and current configuration |
| `/prompts` | Show the prompt folder and reload edited Markdown prompts |
| `/preferences`, `/pref <key> <value>`, `/unpref <key>` | Inspect and edit preferences |
| `/private <note>`, `/unprivate <note>` | Privacy controls |
| `/delete <note>` | Delete a note (confirmation required) |
| `/export` | Download a portable archive |
| `/import` | Import a Telegram export or notes folder |
| `/imports` | List import jobs and their progress |
| `/pause [job-id]`, `/resume [job-id]` | Pause or continue an import |
| `/cancel` | Cancel a pending clarification or import |

### Natural-language controls

You do not need commands for flow control. These are classified deterministically before onboarding or capture, so they are never stored as knowledge:

| You say | What happens |
| --- | --- |
| "покажи системный промпт", "какие у тебя правила?" | Explains the active rules, model, embedding model, locality, and where the prompts live. Secrets stay redacted. |
| "какая модель используется?" | Names the reasoning and embedding model, and whether each is local or cloud. |
| "останови настройку", "стоп", "позже" | Pauses setup, preserves partial answers, and does not count the message as an answer. |
| "продолжи настройку" | Resumes from the exact question that was pending. |
| "не записывай это", "просто ответь" | Answers without creating a note or a lesson. |
| "сделай заметку из этого" | Captures the previous substantive message instead of the instruction itself. |
| "удали последнюю заметку" | Asks for confirmation, then deletes the note; the raw capture remains. |
| "ты меня не понял", "я этого не говорил" | Acknowledges the mistake, corrects the state, and never creates a lesson. |

Precedence is fixed: safety/destructive → flow control → correction/complaint → confirmation answer → explicit preference → question/search → capture → small talk → unknown.

---

## Data layout

```
data/                         operations only
  alan.db                     SQLite: index, links, conflicts, lessons, history
  raw/2026/11/11/
    142400_r_01hqx8k3m2.md    immutable raw capture (frontmatter + exact text)
  exports/
    alan-export-20261111-142400.zip
  incoming/                   downloaded import files

AlanVault/                    the user-facing, Obsidian-compatible vault
  .alan/prompts/              editable behaviour: persona.md, capture.md, ...
  00_Inbox/                   new and unclear material
  01_Notes/                   durable notes
  02_Projects/<slug>/         project material
  03_Decisions/               decisions
  04_Questions/               open questions
  05_People/                  people
  06_References/              references
  07_Daily/                   daily notes
  08_Archive/                 archived, superseded, rejected
  09_Meta/                    Templates, Preferences, Lessons, System
  attachments/
```

* **Raw captures** are the safety layer. They are written before any reasoning, never edited, and only deleted at your explicit request. A database trigger enforces this.
* **Notes** are Markdown with YAML frontmatter in `AlanVault/` — readable and editable in Obsidian, VS Code, or `cat`. Every capture starts in `00_Inbox`; a clear, typed note is promoted to `01_Notes`, `03_Decisions`, `04_Questions`, `06_References`, or its project folder. The file is the authoritative copy of the content: hand edits are detected, versioned and re-indexed, never regenerated over.
* **Prompts** are Markdown in `AlanVault/.alan/prompts/`. The packaged defaults are copied there on first use and are never overwritten afterwards. A missing file, a missing required section, or an unknown `{placeholder}` makes loading fail loudly instead of silently degrading behaviour.
* **The vault is isolated**: it never contains the database, logs, code, environment values, or credentials. `ALAN_VAULT_DIR` changes its location.
* **SQLite** is the queryable index: metadata, versions, links, conflicts, lessons, history, and derived search structures. Losing it costs you search, not knowledge: notes and raw captures are plain files, and `python -m alan reindex` rebuilds the indexes.

A note looks like this:

```markdown
---
id: n_01hqx8k3m2
title: Backend vs AI learning priorities
type: decision
status: active
created: 2026-11-11
updated: 2026-11-11
language: ru
project: Learning plan
tags: [learning, priorities]
sensitive: false
source:
- raw/2026/11/11/142400_r_01hqx8k3m2.md
version: 1
---

# Backend vs AI learning priorities

## Summary
Keep backend as the primary learning track; add AI as a parallel track.

## Decision
Backend remains primary. AI is added in parallel, not as a replacement.

## Reasoning
...

## Constraints
- Available study time is uncertain.

## Open questions
- How much weekly time can realistically go to each track?

## Links
- [[learning-plan]] — Learning plan

## Source
- Raw capture: `raw/2026/11/11/142400_r_01hqx8k3m2.md` — 11 November, 14:24

<details><summary>Original message</summary>
> ...
</details>
```

### Editing a note by hand

Open the file, change the text, save. On the next turn (or the next bot start) Alan notices the new hash and modification time, parses the frontmatter and `## Section` blocks, creates a **new version** with your exact text, and re-indexes it. The previous version stays in the history. If the edit touches meaning that metadata depends on, Alan reports the new version and asks whether to refresh links, tags and status — it does not decide for you. Its own edits create a version with a reason and summary.

---

## How a message becomes knowledge

```
Telegram update
      │
      ▼
0. Raw capture ────────────► data/raw/...  (immutable transcript, before reasoning)
      │
      ▼
1. Intent classification (deterministic, no model)
      │  safety, flow control, complaint, refusal, disclosure,
      │  memory question, note control
      ├── control command ──► handled immediately; never stored as knowledge
      ▼
2. Retrieval ──────────────► FTS5 (normalised + roots) + embeddings fused by RRF
      │
      ▼
3. Assessment (one model call)
      │  intent, note draft, clarifying questions, links, conflicts,
      │  durable-intent / lesson / preference — with recent turns as context
      ├── needs clarification ──► ask ≤2 questions, wait, merge
      ▼
4. Deterministic application
      │  projects, versions, links, conflicts, lessons — no model involved
      │  inbox-first; clear notes are promoted to the right vault folder
      ▼
5. Confirmation or answer
      │  templates for actions, model text for answers, sources appended
      ▼
6. History, audit, embeddings, versioned Markdown
```

Two properties matter here:

* **The model proposes; code decides.** Version numbers, link thresholds, conflict records, project confirmation and privacy rules are implemented in Python, so they behave identically on every provider.
* **Degradation is graceful.** With embeddings disabled, search falls back to FTS5 plus model-selected notes. If the model fails, the raw capture is still stored and Alan says so plainly.

### Memory

Alan keeps four layers, all in SQLite:

| Layer | What it holds | Where |
| --- | --- | --- |
| Working | the current conversation, restarted safely | `messages`, `pending_states` |
| Episodic | specific past interactions and their outcomes | `messages`, raw captures |
| Semantic | facts, projects, preferences, note content | `notes`, `projects`, `preferences` |
| Procedural | behavioural rules learned from you | `lessons`, `audit_log` |

The rolling window is bounded (`ALAN_HISTORY_TURNS`, default 30) and nothing in it can overwrite a raw capture. Questions about the conversation itself are answered from these rows deterministically, so they do not depend on the model's memory or mood.

### Linking policy

A link is created automatically only when one of these holds: the note continues or revises a decision, names an existing project/person/tool/topic, changes another record's status, has a causal or dependency relation, or contradicts a record. Confidence below `0.75` produces a proposal with buttons instead of a link. Shared vocabulary is not a reason to link.

### Conflict policy

When a new record contradicts an older one, Alan shows both claims and asks which reflects your current position. Neither note is edited. Until you decide, the conflict stays `unresolved`, appears in `/conflicts`, and is attached to relevant search results. Resolution marks the superseded note and preserves its version history.

---

## Proactive assistance

Alan interrupts rarely and explains why. Candidates are generated deterministically from SQL:

* unresolved conflicts older than `ALAN_CONFLICT_DAYS`
* open questions untouched for `ALAN_OPEN_QUESTION_DAYS`
* active projects with no movement for `ALAN_STALE_PROJECT_DAYS`
* deadlines inside `ALAN_DUE_SOON_HOURS`
* reminders you created yourself (these bypass the model entirely)

The model then decides whether anything is worth raising *now* and writes at most three sentences in a fixed shape: what was forgotten or became relevant, why it matters now, the source note by name, and one concrete next action or question. Hard limits: one message per day, a minimum gap, quiet hours, no sensitive material, and no repeating an item surfaced in the last seven days.

```dotenv
ALAN_PROACTIVE_QUIET_START=22
ALAN_PROACTIVE_QUIET_END=9
ALAN_PROACTIVE_MAX_PER_DAY=1
```

`/review` runs the same logic on demand. `/pref proactive off` disables it.

---

## Learning and self-improvement

Alan improves by accumulating context and explicit rules, not by retraining.

* **Corrections** create a note version, a conflict record where relevant, and — only if it generalises — one lesson.
* **Feedback** ("too long", "that was an idea, not a decision") is classified by durable intent. A clear, reusable preference becomes a lesson. An ambiguous one triggers a question. A one-time instruction is applied once.
* **Questions and complaints never become lessons.** Asking "why did you classify that as a decision?" is a request for an explanation, not a rule.
* **`/reflect`** reviews recent turns on explicit request and extracts up to three durable lessons, following the same test.
* **`/lessons`** lists id, kind, scope, confidence and source; `/forget <id>` deactivates one. Every change is audited.
* **Preferences** (`/pref detail short`, `/pref language ru`, `/pref quiet_hours 23-8`) steer formatting and reminders and are stored as plain key/value rows, separate from lessons.

Lessons are injected into the assessment and answer prompts as short English instructions. They may adjust preferences, style, thresholds, link proposals, retrieval weights, clarification policy, naming and reminder relevance. They never overwrite raw captures, historical decisions, facts, version history, the audit log, privacy boundaries, confirmed projects, or the core safety rules.

---

## Privacy and control

* Every note, version, link and raw capture is visible and exportable.
* `/private <note>` sets `sensitive = 1`, removes the note from the FTS index and the vector index, and excludes it from proactive candidates. Search additionally filters on the flag.
* `/delete <note>` requires confirmation and keeps the raw capture.
* Raw captures are deleted only by explicit user action; the audit log records who asked and when.
* `/export` produces a zip with all Markdown, all raw captures and a full JSON dump. It is readable without Alan.
* Set `ALAN_OWNER_IDS` — otherwise the bot answers anyone who finds it.

### What Alan will tell you about itself

You are the administrator, so the operating rules are not treated as secret. Ask "what are your rules?" or send `/policy` and Alan explains, in the current language:

* how it classifies input, when it asks a question, and when it creates a lesson;
* the four memory layers and what lives in each;
* the active model, embedding model, whether each is local or cloud, the detail preference, and how many turns it remembers;
* which parts are provider-specific and which are model-independent;
* where the prompt text lives (`AlanVault/.alan/prompts/`), and which Markdown file governs which behaviour.

It will not reveal API keys, bot tokens, credentials, environment values, internal endpoints, or private absolute paths. Outgoing replies are redacted for key-shaped strings, the data directory, and any unresolved `{placeholder}` even if a model echoes them.

Alan never sends messages to third parties, touches calendars or email, makes purchases, or performs irreversible external actions. That is deliberate for v1.2.

---

## Importing existing material

```bash
python -m alan import "C:\path\to\ChatExport\result.json" --limit 40
python -m alan import "C:\path\to\ObsidianVault"
python -m alan import "C:\path\to\vault" --no-analyze   # stage 1 only
python -m alan import --resume imp_01hqx8k3m2           # continue a job
python -m alan import --cancel imp_01hqx8k3m2
python -m alan imports                                  # jobs and progress
```

Or in Telegram: send a `result.json` / folder archive, reply `/import`, then use `/imports`, `/pause` and `/resume`.

An import is a job with two stages:

1. **Capture.** Parse, deduplicate, and store every item as an immutable raw capture in batches. This happens before any model call, so a crash loses no user text.
2. **Analysis.** Turn captured items into notes later, in priority order (longer and more recent first), with progress reported per batch.

Jobs and items live in SQLite (`import_jobs`, `import_items`), so progress survives a restart. On startup an interrupted job is marked `paused`; `/resume` continues from exactly where it stopped, without re-capturing or re-analysing anything. Conflict findings are counted on the job.

Nothing is summarised away during import. Deduplication happens both within the import and against existing raw captures. `--no-notes` / `--no-analyze` store captures only. The one-shot CLI path (`python -m alan import <path>`, without `--no-analyze`) finishes with an inventory: themes, likely projects, active vs stale directions, duplicates, open questions, conflicts, decisions and a first step.

---

## Retrieval design

| Layer | What it is good at | Failure mode it covers |
| --- | --- | --- |
| FTS5 (`unicode61`, prefix matching) | names, numbers, exact phrases, inflected Russian | embedding outages, novel vocabulary |
| Russian normalisation (`alan/rus.py`) | lowercase, `ё→е`, stemming, 3–4 char roots | case and inflection changes between a query and a note |
| Embeddings (cosine over SQLite blobs) | paraphrase, cross-language recall | wording you no longer remember |
| Reciprocal Rank Fusion | combining ranks without score calibration | one retriever being confidently wrong |
| Deterministic filters | status, type, project, privacy | private material leaking into results |

Russian is normalised before indexing and before querying: lowercase, `ё` folded to `е`, punctuation removed, stopwords dropped, a compact Snowball-style stemmer applied, and 3/4-character root tokens added for longer words. Identifiers, URLs, code, dates and numbers are preserved verbatim, so `решение`, `решил` and `решаю` meet in the index while `ALAN_LLM_MODEL` and `2026-11-11` stay findable.

Privacy is enforced in SQL before ranking, and there is no `LIKE` fallback that could bypass it. When nothing matches, Alan says exactly "В базе знаний нет подходящих записей, сэр." (or the English equivalent) rather than inventing a result.

Vectors are stored as `float32` blobs in the same SQLite file, so there is no external vector service to keep alive.

---

## CLI

```bash
python -m alan init                 # create dirs and schema
python -m alan bot                  # run the Telegram bot
python -m alan doctor --ping        # check config, storage, model, embeddings
python -m alan import <path>        # import and inventory
python -m alan imports              # import jobs and progress
python -m alan reindex              # rebuild FTS + embeddings
python -m alan search "query"       # hybrid search from the terminal
python -m alan note <ref>           # print a note's Markdown
python -m alan raw <ref>            # print the original captures
python -m alan stats                # counts
python -m alan export               # portable zip
```

All commands accept `--data-dir` and `--env`.

---

## Project structure

```
alan/
  config.py            environment -> typed config, vault and prompt paths
  db.py                schema, migrations, immutability triggers
  models.py            records and NoteContent
  textutil.py          language detection, slugs, dates, hashing
  rus.py               Russian normalisation, stemming, root tokens
  llm/
    base.py            provider-neutral protocol + retries
    openai_compat.py   OpenAI / OpenRouter / DeepSeek / vLLM …
    anthropic.py       Messages API
    gemini.py          generateContent
    embeddings.py      optional embedding providers
    jsonx.py           lenient JSON recovery + validation
  store/
    raw.py             immutable captures
    notes.py           vault placement, inbox-first, external edits, versions
    search.py          FTS5 + roots + vectors + RRF
    imports.py         resumable import jobs and items
    links.py           meaningful relationships only
    conflicts.py       contradictions, never auto-resolved
    lessons.py         inspectable, reversible learning
    reminders.py       reminders and proactive bookkeeping
    export.py          portable archives
  core/
    intent.py          deterministic intent classifier (control before capture)
    prompt_loader.py   strict Markdown prompt loader, copy, reload, validation
    prompts.py         PromptSet composed from the Markdown library
    schemas.py         validated model output
    analyze.py         the single place a model is called
    apply.py           deterministic application
    persona.py         address, disclosure, confirmations, redaction
    service.py         the intent-first turn pipeline
    inventory.py       import report
  bot/                 aiogram transport only
  proactive/           candidate generation + background loop
  ingest/              Telegram export and Markdown importers
prompts/               packaged default Markdown prompts
tests/                 99 tests, no network required
```

---

## Design notes

A few choices are deliberate and worth stating.

**Raw capture before reasoning.** If the model misclassifies, mistranslates, or fails entirely, the original is already safe. This is why capture is step one, not step five.

**Structured content, authoritative Markdown.** The Markdown file is the authoritative copy of a note's content; SQLite keeps metadata, versions and derived indexes. That keeps the file genuinely editable by hand *and* keeps every change versioned: an external edit is parsed into a new version, never regenerated away. Alan only regenerates a file when the change came through conversation, and even then the previous version is preserved.

**One model call per capture.** Assessment returns the classification, the note, links, conflicts and questions in a single JSON object. Fewer calls means lower latency, lower cost, and fewer places for behaviour to diverge between providers.

**Structured output without provider-specific schemas.** Alan asks for JSON in the prompt and recovers it leniently, with one repair attempt. This works identically on OpenAI JSON mode, Anthropic, and Gemini — no vendor tool-schema dialects.

**Deterministic confirmations.** "Recorded, sir." is assembled from applied data, not generated. The same action reads the same way on every model, in English or Russian.

**Conflicts are a state, not an edit.** Alan never picks a winner. The conflict is a row with a status, attached to both notes and surfaced in search.

---

## Testing

```bash
python -m pytest
```

99 tests, all offline. They cover raw-capture immutability (including the SQLite triggers), note rendering and versioning, external Markdown edits, FTS5 and Russian-inflected retrieval, vector retrieval, RRF fusion, privacy exclusion, clarification flow, persisted conversation memory across a restart, language continuity for short replies, corrections and lesson discipline, one-time vs durable intent, self-disclosure without secrets, link proposals, conflict recording and resolution, proactive candidate generation and send-window limits, resumable import with pause/resume and crash repair, onboarding, export contents, JSON recovery, bot assembly, and the v1.2 acceptance criteria: interruptible onboarding with resume, no false profile from refusals or complaints, Russian control replies, no placeholder leaks, editable/reloadable Markdown prompts, and vault isolation (`tests/test_v12.py`).

The fake model in `tests/conftest.py` returns scripted responses, so tests exercise the real pipeline end to end without an API key.

---

## Troubleshooting

**"ALAN_TELEGRAM_TOKEN is not set"** — `.env` is missing or the bot is run from another directory. Pass `--env path/to/.env`.

**The bot replies "The reasoning model failed"** — run `python -m alan doctor --ping`. Check `ALAN_LLM_BASE_URL` for compatible providers (it must include `/v1` when the vendor uses one). The user's message is still saved as a raw capture.

**Search misses obvious notes** — embeddings are probably off. Set `ALAN_EMBED_PROVIDER=openai` (or `gemini`), then `python -m alan reindex`. The embedding key falls back to `ALAN_LLM_API_KEY` when both use the same provider.

**The bot answers strangers** — `ALAN_OWNER_IDS` is empty. Set it and restart.

**Proactive messages never arrive** — check quiet hours, `ALAN_PROACTIVE_ENABLED`, and whether `ALAN_OWNER_IDS` is set (the worker needs a chat to write to). `/review` shows whether candidates exist.

---

## Not in v1.2

By design, per the product scope: calendar and email access, voice transcription, phone interface, web widget, autonomous task execution, and spaced repetition. Voice and images are stored with their references, but their content is not interpreted.

## License

Private project. No license granted.
