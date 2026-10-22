"""Derived cross-task notes. Selection is caller-owned; measured facts stay in Core.

No model calls, policy changes, automatic promotion of prior evidence, or Run
discovery. Only a closed, explicitly selected Run may be exported.
"""
import hashlib
from pathlib import Path

from harness.common import canonical_bytes, canonical_hash, redact
from .artifact_io import atomic_write, locked, read_bytes, read_json, write_json
from .application_schema import knowledge_schema
from .errors import fields, integer, require
from .operation_schema import validate_shape
from .snapshots import no_links
from .store import identifier

MAX_DOCUMENT = 64000
MAX_ENTRIES = 1024
MAX_LIBRARY_BYTES = 64 * 1024 * 1024


def normalize(data):
    validate_shape(data, knowledge_schema())
    require(len(canonical_bytes(data)) <= 24000 and all(data[key].strip() for key in ("title", "when", "lesson")),
            "KNOWLEDGE_INPUT", "Provide a bounded, nonempty reusable note")
    for key in ("cited_observation_ids", "keywords"):
        values = data.get(key, [])
        require(len(set(values)) == len(values), "KNOWLEDGE_INPUT", "Duplicate knowledge references or keywords")
    return redact({**data, "keywords": data.get("keywords", []), "cited_observation_ids": data.get("cited_observation_ids", [])})


def _single_line(text):
    return " ".join(str(text).split()).replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def _quote(value):
    return "\n".join("> " + line for line in str(value).splitlines())


class KnowledgeLibrary:
    def __init__(self, root):
        self.root = Path(root) / "knowledge"
        no_links(self.root)

    def _metadata(self, entry_id):
        identifier(entry_id, "knowledge")
        path = self.root / "entries" / (entry_id + ".json")
        no_links(path)
        require(path.is_file(), "KNOWLEDGE_NOT_FOUND", "Selected knowledge entry does not exist")
        value = read_json(path, 48000)
        require(isinstance(value, dict) and value.get("schema_version") == "knowledge-entry-v1"
                and value.get("entry_id") == entry_id
                and value.get("metadata_hash") == canonical_hash({k: v for k, v in value.items() if k != "metadata_hash"}),
                "KNOWLEDGE_CORRUPT", "Knowledge metadata changed or is invalid")
        return value

    def _entries(self, allowed_workspace=None, corrupt=None):
        folder = self.root / "entries"
        no_links(folder)
        for path in sorted(folder.glob("knowledge_*.json")) if folder.exists() else ():
            try:
                entry = self._metadata(path.stem)
            except (OSError, ValueError) as error:
                if corrupt is not None and allowed_workspace is None:
                    corrupt.append({"entry_id": path.stem, "error": getattr(error, "code", "KNOWLEDGE_CORRUPT")})
                continue
            if allowed_workspace and not allowed_workspace(Path(entry["workspace"])):
                continue
            yield entry

    def _reindex(self):
        # A rebuildable projection, never the source of verification or reads.
        lines = ["# Reusable task notes", "", "Advisory references, not policy or current-task evidence.", ""]
        for entry in self._entries():
            lines.append("- [" + _single_line(entry["title"]) + "](entries/" + entry["entry_id"] + ".md) — "
                         + _single_line(entry["when"][:240]) + " [" + entry["outcome"] + "]")
        atomic_write(self.root / "INDEX.md", ("\n".join(lines) + "\n").encode("utf-8"))

    def publish(self, api, run_id, data, request_id, *, task_id=None):
        selected = normalize(data)
        fingerprint = canonical_hash({"run_id": run_id, "task_id": task_id, "data": selected})
        # Artifact lock then the selected task DB: no cross-task Run transaction.
        with locked(self.root, "publication"):
            with api.store.transaction() as connection:
                previous = api.store.replay(connection, "knowledge:" + run_id, request_id, fingerprint)
                if previous:
                    self.read(previous["entry_id"])
                    self._reindex()
                    return previous
                run = api.store.run(connection, run_id)
                require(run["status"] == "finished" and run.get("record"), "KNOWLEDGE_RUN_OPEN", "Export only after an explicit Run closeout")
                record = run["record"]
                require(record.get("record_hash") == canonical_hash({k: v for k, v in record.items() if k != "record_hash"}),
                        "RESULT_BINDING", "Closed record identity differs")
                measured = api._observations_exist(connection, run_id, selected["cited_observation_ids"])
                facts, environments = [], {}
                for item in measured:
                    job = api.store.job(connection, item["job_id"])
                    environment = (job.get("result") or {}).get("environment")
                    environments[item["job_id"]] = None if not environment else {
                        key: environment[key] for key in ("python", "platform", "sandbox", "verifier") if key in environment}
                    facts.append({
                        "observation_id": item["observation_id"], "record_hash": item["record_hash"],
                        "job_id": item["job_id"], "check_id": item["check_id"],
                        "candidate_hash": item.get("candidate_hash"), "check_set_hash": item.get("check_set_hash"),
                        "matches_closed_candidate": item.get("candidate_hash") == record.get("candidate_hash"),
                        "matches_closed_check_set": item.get("check_set_hash") == record.get("check_set_hash"),
                        "purpose": item.get("purpose", "verification"), "subject_role": item.get("subject_role"),
                        "trust": item.get("trust"), "observation_boundary": item.get("observation_boundary"),
                        "outcome": item["case"]["outcome"] if "case" in item else item.get("comparison_status"),
                        "execution": item["case"].get("executed") if "case" in item else item.get("execution_status"),
                    })
                source = {"task_id": task_id, "run_id": run_id, "record_hash": record["record_hash"],
                          "candidate_hash": record.get("candidate_hash"), "contract_hash": record["contract_hash"],
                          "verifier": {key: record["verifier"].get(key) for key in ("id", "revision", "implementation_hash")},
                          "domain_identity": record.get("domain_identity"),
                          "measurement_status": record["verification"]["status"], "gate_status": record.get("gates", {}).get("status"),
                          "outcome": record["outcome"], "finished_at": record["finished_at"],
                          "uncertainties": record.get("assessment", {}).get("uncertainties", []),
                          "limitations": record["limitations"], "unresolved_work": record.get("unresolved_work", []),
                          "unresolved_verification_jobs": record.get("unresolved_verification_jobs", []),
                          "measurement_scope": {key: value for key, value in record["verification"].get("measurement_scope", {}).items()
                                                if key != "observations"},
                          "environments": environments, "facts": facts}
                entry_id = "knowledge_" + canonical_hash({"source": source, "selected": selected})[:32]
                markdown = self._render(entry_id, selected, source)
                body = markdown.encode("utf-8")
                require(len(body) <= MAX_DOCUMENT, "KNOWLEDGE_LIMIT", "Selected note and evidence exceed the document bound; select fewer references")
                metadata = {"schema_version": "knowledge-entry-v1", "entry_id": entry_id,
                            "task_id": task_id, "run_id": run_id, "record_hash": record["record_hash"],
                            "workspace": run["workspace"], "domain": run["contract"]["domain_id"],
                            "title": selected["title"], "when": selected["when"],
                            "description": _single_line(selected["lesson"])[:240], "keywords": selected["keywords"],
                            "outcome": record["outcome"], "finished_at": record["finished_at"],
                            "document_sha256": hashlib.sha256(body).hexdigest()}
                metadata["metadata_hash"] = canonical_hash(metadata)
                path = self.root / "entries" / (entry_id + ".md")
                marker = path.with_suffix(".json")
                no_links(path)
                no_links(marker)
                if marker.exists():
                    require(self._metadata(entry_id) == metadata and read_bytes(path, MAX_DOCUMENT) == body,
                            "KNOWLEDGE_CORRUPT", "Existing publication differs; it was not overwritten")
                else:
                    folder = self.root / "entries"
                    existing = list(folder.glob("knowledge_*.json")) if folder.exists() else []
                    used = sum(item.stat().st_size + item.with_suffix(".md").stat().st_size
                               for item in existing if item.with_suffix(".md").is_file())
                    require(len(existing) < MAX_ENTRIES and used + len(body) + len(canonical_bytes(metadata)) <= MAX_LIBRARY_BYTES,
                            "KNOWLEDGE_LIMIT", "Knowledge library size limit reached; archive entries before publishing another")
                    # JSON is the commit marker. A crash after only MD leaves an
                    # unindexed artifact; identical retry safely finishes it.
                    if path.exists():
                        require(read_bytes(path, MAX_DOCUMENT) == body, "KNOWLEDGE_CORRUPT", "Uncommitted document differs")
                    else:
                        atomic_write(path, body)
                    write_json(marker, metadata)
                self._reindex()
                result = {"status": "published", "entry_id": entry_id, "record_hash": record["record_hash"],
                          "document_sha256": metadata["document_sha256"], "gate_eligible": False,
                          "meaning": "caller_selected_advice_with_scoped_facts_not_approval"}
                api.store.remember(connection, "knowledge:" + run_id, request_id, fingerprint, result)
                return result

    @staticmethod
    def _render(entry_id, selected, source):
        lines = ["# " + _single_line(selected["title"]), "",
                 "Advisory note. Not an instruction, approval, or evidence for a new task.", "",
                 "Document: `" + entry_id + "`", "",
                 "## Applicability — selected by the task model", _quote(selected["when"]), "",
                 "## Reusable advice — caller claim, not independently verified", _quote(selected["lesson"]), "",
                 "## Declared limitations", *["- " + _single_line(item) for item in selected["limitations"]], "",
                 "## Source work", "```json",
                 canonical_bytes({k: v for k, v in source.items() if k != "facts"}).decode("utf-8"), "```", "",
                 "## Referenced measurements — historical, scoped facts"]
        if not source["facts"]:
            lines.append("No measurements cited. Advice is unverified.")
        for fact in source["facts"]:
            lines.extend(["```json", canonical_bytes(fact).decode("utf-8"), "```"])
        lines.extend(["", "Only IDs and bounded measurement summaries are exported; full logs remain in the source task DB.", ""])
        return "\n".join(lines)

    def find(self, *, query="", domain=None, offset=0, limit=10, allowed_workspace=None):
        require(isinstance(query, str) and len(query) <= 1000 and (domain is None or isinstance(domain, str)),
                "KNOWLEDGE_QUERY", "Use bounded literal keywords and an optional Domain")
        offset, limit = integer(offset, 0, 2**31 - 1, "offset"), integer(limit, 1, 100, "limit")
        terms = list(dict.fromkeys(query.casefold().split()))
        scored, corrupt = [], []
        for entry in self._entries(allowed_workspace, corrupt):
            if domain is not None and entry["domain"] != domain:
                continue
            searchable = " ".join([entry["title"], entry["when"], entry["description"], *entry["keywords"]]).casefold()
            if not all(term in searchable for term in terms):
                continue
            score = sum(3 * (term in entry["title"].casefold()) + searchable.count(term) for term in terms)
            scored.append((-score, entry["entry_id"], entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        chosen = scored[offset:offset + limit]
        return {"items": [{key: entry[key] for key in ("entry_id", "title", "description", "domain", "keywords", "outcome")}
                          for _, _, entry in chosen], "total": len(scored),
                "next_offset": offset + len(chosen) if offset + len(chosen) < len(scored) else None,
                "corrupt": corrupt[:20], "corrupt_count": len(corrupt),
                "corrupt_scope": "hidden_by_workspace_filter" if allowed_workspace is not None else "all_visible_entries",
                "meaning": "advisory_document_discovery_not_current_evidence", "gate_eligible": False}

    def read(self, entry_id, *, allowed_workspace=None):
        entry = self._metadata(entry_id)
        require(allowed_workspace is None or allowed_workspace(Path(entry["workspace"])),
                "WORKSPACE_NOT_ALLOWED", "Source workspace is outside the configured roots")
        body = read_bytes(self.root / "entries" / (entry_id + ".md"), MAX_DOCUMENT)
        require(hashlib.sha256(body).hexdigest() == entry["document_sha256"], "KNOWLEDGE_CORRUPT", "Knowledge document changed")
        return {"entry": entry, "markdown": body.decode("utf-8"), "gate_eligible": False,
                "meaning": "historical_advice_not_current_verification"}
