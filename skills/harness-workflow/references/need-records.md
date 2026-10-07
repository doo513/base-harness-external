# Optional legacy Need records

Read this only when explicitly maintaining durable Need records or integrating a
legacy client. It is not part of ordinary Need discovery. Develop analysis
perspectives and the Knowledge Map support Host reasoning without registration,
status updates or a requirement to resolve stored questions before acting.

Use the existing `observe` envelope with the following `data`:

```json
{
  "kind": "need",
  "note": "Identify the compatibility decision before changing the transformation",
  "interpretation_revision": 1,
  "need": {
    "id": "collision-policy",
    "expected_revision": 0,
    "state": "open",
    "details": {
      "kind": "decision",
      "question": "Which value should survive a legacy/nested field collision?",
      "reason": "Avoid silently discarding existing configuration",
      "resolution_criterion": "Locate an applicable policy or get the user's decision",
      "requirement_ids": [],
      "knowledge_refs": ["compatibility"]
    }
  }
}
```

`details.kind` is knowledge/observation/decision/verification. Requirement IDs must
refer to declared requirements; leave them empty during initial discovery. Knowledge
refs select map IDs, not required reading. They may be empty for questions outside
the map. Core does not interpret either Domain vocabulary.

Updates send the same Need ID, its returned `ref.revision` as `expected_revision`,
and the complete current details. States are open/addressed/deferred; addressed and
deferred require a nonblank `conclusion`. Reopening defaults the conclusion to empty
so an old answer is not silently retained. A distinct request ID records a new
update; replay the same envelope/ID after response loss. Concurrent stale updates
are rejected without replacing the newer version.

When answering the same question, reuse its returned `details`; do not replace the
question with a generic closeout instruction. Given a saved observe response, this
constructs the `data` object for a new update (the request still needs its own ID):

```python
current = saved_response["need"]  # Or the selected item from records kind=needs.
data = {
    "kind": "need", "note": "Record the source-grounded conclusion",
    "need": {
        "id": current["need_id"], "expected_revision": current["ref"]["revision"],
        "details": current["details"], "state": "addressed",
        "conclusion": "The supplied schema specifies which value survives."
    },
    "references": ["schema.txt#collision-policy"]
}
```

Keep these fields at their distinct levels:

| Field in `data` | Value |
|---|---|
| `need.details` | Domain question, reason, criterion and Domain references |
| `need.conclusion` | Answer or deferral explanation, not inside `details` |
| `references` | Locator strings, not path/symbol objects or a field inside `need` |
| `need.activity_ids` | Optional complete returned `activity.ref.id` values; do not invent a UUID or use a Need ID |
| `observation_ids` | Returned Verifier observation IDs, not caller notes |

Top-level `references` are bounded source locators (file/symbol/section or URL),
never fetched by Harness and never authenticated facts. Top-level `observation_ids`
must be real Verifier observations from this Run; older Candidates remain historical
context, not current proof. Inside `need`, optional `activity_ids` cite earlier
decisions/activities and `check_ids` cite active Checks. Their exact revisions are
recorded. Cite sources and state why the conclusion is sufficient, not just what
was read. Do not use caller note IDs as measurement IDs.

`records kind=needs` returns paginated current Need records; `activity` retains
update references and context pages retain immutable snapshot versions. `resume`
and `context.critical` include counts, open/deferred IDs and `context_changed_ids`.
The latter only means the recorded Interpretation or Candidate differs. It does
not inspect files for freshness or decide whether an answer is still applicable.
Use a full context transfer after lost Host context, then deltas as usual.

Need records are caller claims, not GoalContract parameters. They do not change
the contract, Candidate, Measurement, Assessment, gates or completion policy.
Use `revise`/`check` only when reasoning actually changes those declarations, and
preserve existing approval boundaries. Unresolved Needs are visible advice, not a
new mandatory finish rule. Hosts without Need support and Domains without this
optional analysis port retain the existing workflow.
