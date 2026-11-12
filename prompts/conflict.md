# Conflict Policy

## Rules

Record a conflict only when two claims about the same subject cannot both be
true at the same time.

* Added detail is not a conflict.
* Changed emphasis is not a conflict.
* A plan changed with a stated reason is an update, not a conflict.
* A correction by the user is not a conflict.

When a conflict exists:

* keep both records;
* mark the newer note status as conflict until the user decides;
* present both claims, their notes, and the reason they conflict;
* ask which version reflects the current position;
* never silently choose a winner;
* when resolved, keep the superseded version and update the affected notes.

## Output

Return a conflict entry only when the rule above is satisfied:

{"note_id": "n_new", "other_note_id": "n_old", "new_claim": "Backend is obsolete.", "existing_claim": "Backend is the primary track.", "explanation": "The two claims cannot both be current."}
