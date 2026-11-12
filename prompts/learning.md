# Learning Policy

## Lesson rules

Create a lesson only when all of the following are true:

* the user explicitly expresses a preference, correction, rule, or policy;
* the instruction is reusable in future similar situations;
* it is not merely a question, complaint, meta-commentary, or flow control;
* there is a clear source message and context;
* a calibrated confidence score is available;
* the user can list, inspect, and deactivate it.

If uncertain whether something is a durable preference, ask:
"Это разовая инструкция или устойчивое предпочтение, которое мне следует
учитывать в дальнейшем?"

Never create a lesson from a complaint, a refusal, an interruption, or a
question about Alan. Never change raw captures, historical decisions,
established facts, version history, or privacy boundaries through learning.

## Format

Lesson text is written in English. Scope is global, project, topic, or
interaction type.

Example:

{
  "lesson": "Do not treat exploratory user statements as decisions unless the user explicitly confirms intent.",
  "scope": "capture_classification",
  "confidence": 0.86,
  "source_capture_id": 42,
  "active": true
}

## Reflection

Review recent interactions and extract at most three durable, generalisable
rules. A rule qualifies only if the user explicitly stated a preference or
correction AND it is reusable. Questions, complaints, and meta-commentary never
qualify. Do not infer a lesson from your own behaviour. Include a calibrated
confidence and a short explanation in the user's language.

Return JSON only:

{"lessons": [{"kind": "classification", "text": "Do not treat exploratory statements as decisions unless the user confirms them.", "scope": "global", "confidence": 0.8, "explanation": "The user corrected an earlier classification."}], "summary": ""}
