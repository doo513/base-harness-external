"""Closed Run Markdown derived from the selected DB, never execution authority.

Only publication receipts are retained in that same DB. There is no shared Run
database or raw-transcript copy. File publication never holds a DB write lock.
"""
import hashlib
import os
import re
from collections import Counter
from pathlib import Path

from harness.common import canonical_bytes, canonical_hash, redact
from harness.json_codec import decode
from .artifact_io import atomic_write, locked, read_bytes
from .errors import HarnessError, integer, require
from .snapshots import no_links
from .store import identifier

SCHEMA = "harness-run-ledger-v1"
PREFIX = "<!-- " + SCHEMA + " "
MAX_DOCUMENT = 32768
MAX_REPORTS = 1024
METADATA_KEYS = {"schema_version","run_id","task_id","workspace","domain","source_database","storage_kind",
                 "record_hash","goal_excerpt","display_status","finished_at","body_sha256","detail_level","assurance","ready"}


def digest(body):
    return hashlib.sha256(body).hexdigest()


def line(text, maximum=240):
    value = " ".join(str(redact(str(text))).split())[:maximum]
    return value.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("<", "&lt;").replace(">", "&gt;")


def excerpt(text, maximum):
    value = str(redact(str(text)))
    raw=value.encode("utf-8")
    return {"text": raw[:maximum].decode("utf-8",errors="ignore"), "truncated": len(raw)>maximum}


class RunReports:
    def __init__(self, root):
        self.root = Path(root) / "run-ledger"
        no_links(self.root)

    def _read(self, run_id):
        identifier(run_id, "run")
        target=self.root / (run_id + ".md")
        no_links(target)
        require(target.is_file(),"RUN_REPORT_NOT_FOUND","Run document does not exist")
        raw = read_bytes(target, MAX_DOCUMENT)
        try:
            first, body = raw.decode("utf-8").split("\n", 1)
            require(first.startswith(PREFIX) and first.endswith(" -->"), "RUN_REPORT_CORRUPT", "Invalid Run ledger header")
            metadata = decode(first[len(PREFIX):-4])
            require(isinstance(metadata, dict) and set(metadata)==METADATA_KEYS and metadata.get("schema_version") == SCHEMA and metadata.get("run_id") == run_id
                    and metadata.get("body_sha256") == digest(body.encode("utf-8"))
                    and isinstance(metadata.get("workspace"), str) and Path(metadata["workspace"]).is_absolute()
                    and isinstance(metadata.get("domain"), str) and isinstance(metadata.get("goal_excerpt"), str)
                    and isinstance(metadata.get("source_database"),str) and Path(metadata["source_database"]).is_absolute()
                    and metadata.get("storage_kind") in ("project","legacy_external")
                    and metadata.get("detail_level") in ("summary","compact")
                    and isinstance(metadata.get("display_status"),str)
                    and type(metadata.get("finished_at")) in (int,float) and 0<=metadata["finished_at"]<10**12
                    and (metadata.get("task_id") is None or isinstance(metadata["task_id"],str) and re.fullmatch(r"task_[a-f0-9]{32}",metadata["task_id"]))
                    and metadata.get("assurance")=="local-advisory" and metadata.get("ready") is False
                    and isinstance(metadata.get("record_hash"), str) and re.fullmatch(r"[a-f0-9]{64}",metadata["record_hash"]),
                    "RUN_REPORT_CORRUPT", "Run ledger identity or body digest differs")
        except (ValueError, UnicodeError) as error:
            if isinstance(error, HarnessError):
                raise
            raise HarnessError("RUN_REPORT_CORRUPT", "Run ledger is not a complete UTF-8 document") from error
        return metadata, raw

    @staticmethod
    def render(state, database, task_id, *, compact=False):
        record = state["record"]
        require(all(k in record for k in ("resolution","lifecycle","outcome","outcome_meaning","run_id","record_hash",
            "contract_hash","finished_at","verifier","intent_ref","interpretation_ref","policy_ref")),
            "RUN_REPORT_UNSUPPORTED","Historical record lacks fields required by this report schema")
        resolution = record["resolution"]
        gates = record.get("gates", {})
        failed = [{"check_id": item["check_id"], "status": item["status"]} for item in gates.get("results", []) if item["status"] != "passed"]
        assessment = record.get("assessment") or {}
        uncertainty = assessment.get("uncertainties", [])
        scope = {key: value for key,value in resolution.get("measurement_scope", {}).items() if key != "observations"}
        requirements=resolution.get("requirements") or {}
        requirements={key:value for key,value in requirements.items() if key in {"schema_version","requirement_count","counts_by_kind","binding_status","meaning"}}
        for key in ("known_gap_ids","unlinked_requirement_ids","unlisted_scenario_requirement_ids"):
            values=(resolution.get("requirements") or {}).get(key,[])
            requirements[key]=values[:12]
            requirements["omitted_"+key]=max(0,len(values)-12)
        comparison=record.get("baseline_comparison") or {"status":"not_requested"}
        changes=comparison.get("changes",[])
        facts = {
            "lifecycle": record["lifecycle"], "requested_outcome": record["outcome"], "outcome_meaning": record["outcome_meaning"],
            "display_status": resolution["display_status"], "measurement_status": resolution["measurement_status"],
            "gate_status": resolution["gate_status"], "assessment_status": resolution["assessment_status"],
            "required_check_count": len(gates.get("results", [])), "unsatisfied_checks": failed[:20],
            "omitted_unsatisfied_checks": max(0,len(failed)-20), "measurement_scope": scope,
            "candidate_hash": record.get("candidate_hash"), "contract_hash": record["contract_hash"],
            "check_set_hash": record.get("check_set_hash"), "finished_at": record["finished_at"],
            "verifier": {k:record["verifier"].get(k) for k in ("id","revision","implementation_hash")},
            "requirements": requirements,
            "policy":{"mode":state["policy"]["mode"],"approval_status":resolution.get("policy_approval",{}).get("status","not_established")},
            "domain":{k:(record.get("domain_identity") or {}).get(k) for k in ("id","revision","identity_hash")},
            "baseline_comparison":{"status":comparison.get("status"),"counts":dict(Counter(item["status"] for item in changes)),
                "incomparable_reasons":sorted({reason for item in changes for reason in item.get("incomparable_reasons",[])})},
            "limitations":[excerpt(item,400) for item in record.get("limitations",[])[:8]],
            "omitted_limitations":max(0,len(record.get("limitations",[]))-8),
        }
        # Excerpts are labeled projections. Original text and all details remain
        # in the DB; neither statements nor transcript bodies are reinterpreted.
        claims = {"source":"caller_claim", "summary":excerpt(record.get("model_summary", ""),2000),
                  "assessment_summary":excerpt(assessment.get("summary", ""),2000),
                  "uncertainties":[excerpt(item,600) for item in uncertainty[:12]],
                  "omitted_uncertainties":max(0,len(uncertainty)-12)}
        if compact:
            facts["unsatisfied_checks"]=[]
            facts["omitted_unsatisfied_checks"]=len(failed)
            facts["limitations"]=[]
            facts["omitted_limitations"]=len(record.get("limitations",[]))
            facts["requirements"]={key:requirements.get(key) for key in ("schema_version","requirement_count","counts_by_kind","binding_status","meaning")}
            facts["requirement_details_omitted"]=True
            claims={"source":"caller_claim","summary":excerpt(record.get("model_summary",""),256),
                    "details_omitted":True,"uncertainty_count":len(uncertainty)}
        workspace=Path(state["run"]["workspace"])
        database_reference={"database_relative_to_workspace":Path(database).relative_to(workspace).as_posix()} if Path(database).is_relative_to(workspace) else {"database":str(database)}
        source = {"workspace":state["run"]["workspace"],"task_id":task_id,"run_id":record["run_id"],
                  **database_reference,"record_hash":record["record_hash"],"intent_ref":record["intent_ref"],
                  "interpretation_ref":record["interpretation_ref"],"policy_ref":record["policy_ref"]}
        body = "\n".join([
            "", "# Run " + record["run_id"], "",
            "Derived closed-Run ledger. The source database is authoritative; this document cannot authorize execution or change a verdict.",
            "Unsigned local-advisory record; ready=false. Historical results are not evidence for another Run.", "",
            "## Goal excerpt", "", "> " + line(state["intent"]["original_goal"],160 if compact else 800), "",
            "## Recorded outcome and measurement scope", "", "```json", canonical_bytes(facts).decode("utf-8"), "```", "",
            "## Caller interpretation, not measured fact", "", "```json", canonical_bytes(redact(claims)).decode("utf-8"), "```", "",
            "## Source database and immutable references", "", "```json", canonical_bytes(source).decode("utf-8"), "```", "",
            "Detailed observations, working notes and request history remain in the source DB. Use its exact task/Run binding to inspect them.",
            "No complete-goal proof or current-workspace freshness is established by this Markdown document.", "",
        ])
        metadata = {"schema_version":SCHEMA,"run_id":record["run_id"],"task_id":task_id,
            "workspace":source["workspace"],"domain":state["run"]["domain_id"],"source_database":str(database),
            "storage_kind":"project" if Path(database).is_relative_to(workspace) else "legacy_external",
            "record_hash":record["record_hash"],"goal_excerpt":line(state["intent"]["original_goal"],160),
            "display_status":resolution["display_status"],"finished_at":record["finished_at"],
            "body_sha256":digest(body.encode("utf-8")),"detail_level":"compact" if compact else "summary","assurance":"local-advisory","ready":False}
        header=canonical_bytes(metadata).decode("utf-8").replace("<","\\u003c").replace(">","\\u003e")
        raw=(PREFIX+header+" -->\n"+body).encode("utf-8")
        if len(raw)>MAX_DOCUMENT and not compact:
            return RunReports.render(state,database,task_id,compact=True)
        require(len(raw)<=MAX_DOCUMENT,"RUN_REPORT_LIMIT","Closed Run projection exceeds its document limit")
        return raw

    def _entries(self, allowed_workspace=None):
        no_links(self.root)
        paths=sorted(self.root.glob("run_*.md")) if self.root.exists() else []
        require(len(paths)<=MAX_REPORTS,"RUN_REPORT_LIMIT","Run ledger retention limit reached")
        entries, corrupt=[],[]
        for path in paths:
            try:
                entry,_=self._read(path.stem)
            except (OSError,ValueError) as error:
                if allowed_workspace is None:
                    corrupt.append({"entry_id":path.stem,"error":getattr(error,"code","RUN_REPORT_CORRUPT")})
                continue
            if allowed_workspace is None or allowed_workspace(Path(entry["workspace"])):
                entries.append(entry)
        return entries,corrupt

    def _reindex(self):
        entries,corrupt=self._entries()
        text=["# Run ledger index","","Derived historical documents, not runtime authority. Source databases remain authoritative.",""]
        for entry in entries:
            text.append("- ["+entry["run_id"]+"]("+entry["run_id"]+".md) — "+line(entry["goal_excerpt"])+" ["+line(entry["display_status"])+"]")
        if corrupt:
            text.extend(["", "Unindexed unreadable/corrupt documents: "+str(len(corrupt))])
        atomic_write(self.root/"INDEX.md",("\n".join(text)+"\n").encode("utf-8"))

    def publish(self, api, run_id, request_id, *, task_id=None):
        # This Core read validates record and observation bindings. No writes to
        # Run lifecycle, criteria, measurements or assessment occur here.
        state=api.resume(run_id)
        require(state["run"]["status"]=="finished" and state.get("record"),"RUN_REPORT_OPEN","Publish only a closed Run")
        record_hash=state["record"]["record_hash"]
        target=self.root/(run_id+".md")
        scope="run-report:"+canonical_hash({"root":str(self.root),"run_id":run_id})
        canonical_scope=scope+":document"
        fingerprint=canonical_hash({"record_hash":record_hash,"task_id":task_id})
        # The common library is private storage even when no legacy root DB has
        # ever initialized/hardened its parent directory.
        no_links(self.root.parent)
        self.root.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if os.name=="posix":
            require(self.root.parent.stat().st_uid==os.getuid(),"RUN_REPORT_OWNER","Shared library must be owned by this Harness user")
            self.root.parent.chmod(0o700)
        with locked(self.root,"publication"):
            with api.store.transaction(write=False) as connection:
                prior=api.store.replay(connection,scope,request_id,fingerprint)
                known=api.store.replay(connection,canonical_scope,run_id,fingerprint)
            raw=self.render(state,api.store.database,task_id)
            if target.exists() or target.is_symlink():
                entry,actual=self._read(run_id)
                require(entry["record_hash"]==record_hash and digest(actual)==(known["document_sha256"] if known else digest(raw)),
                        "RUN_REPORT_CORRUPT","Existing Run document differs; preserve it and inspect the source DB")
                raw=actual
            else:
                if known:
                    require(digest(raw)==known["document_sha256"],"RUN_REPORT_VERSION","Renderer differs from the historical publication; do not silently replace it")
                require(len(list(self.root.glob("run_*.md")))<MAX_REPORTS,"RUN_REPORT_LIMIT","Run ledger retention limit reached")
                atomic_write(target,raw)
            self._reindex()
            result={"status":"published","entry_id":run_id,"path":str(target),"record_hash":record_hash,
                    "document_sha256":digest(raw),"gate_eligible":False,"meaning":"derived_run_document_not_runtime_authority"}
            with api.store.transaction() as connection:
                current=api.store.run(connection,run_id)
                require(current.get("record",{}).get("record_hash")==record_hash and current["status"]=="finished",
                        "RESULT_BINDING","Source Run changed before publication receipt")
                if not known:
                    api.store.remember(connection,canonical_scope,run_id,fingerprint,result)
                if not prior:
                    api.store.remember(connection,scope,request_id,fingerprint,result)
            return prior or result

    def read(self, entry_id, *, allowed_workspace=None):
        entry,raw=self._read(entry_id)
        require(allowed_workspace is None or allowed_workspace(Path(entry["workspace"])),
                "WORKSPACE_NOT_ALLOWED","Run document belongs to another workspace")
        return {"entry":entry,"markdown":raw.decode("utf-8"),"document_sha256":digest(raw),"gate_eligible":False,
                "source_state_not_queried":True,"meaning":"historical_document_not_current_authority"}

    def list(self, *, workspace=None, domain=None, offset=0, limit=20, allowed_workspace=None):
        offset,limit=integer(offset,0,2**31-1,"offset"),integer(limit,1,100,"limit")
        require(workspace is None or isinstance(workspace,str) and Path(workspace).is_absolute(),"RUN_REPORT_QUERY","Workspace filter must be absolute")
        require(domain is None or isinstance(domain,str),"RUN_REPORT_QUERY","Domain filter must be a string")
        values,corrupt=self._entries(allowed_workspace)
        values=[v for v in values if (workspace is None or v["workspace"]==workspace) and (domain is None or v["domain"]==domain)]
        page=values[offset:offset+limit]
        return {"items":page,"total":len(values),"next_offset":offset+len(page) if offset+len(page)<len(values) else None,
                "corrupt":corrupt[:20],"corrupt_count":len(corrupt),"source_state_not_queried":True,"gate_eligible":False}
