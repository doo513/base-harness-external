"""Compatibility for explicitly requested legacy Need records, not discovery.

Preparation and ordinary problem solving do not call this validator. Keeping it
separate lets old clients retain their records without making the analysis index
depend on a persistence schema.
"""
import copy

from .develop_analysis import KINDS, KNOWLEDGE_MAP
from .errors import fields, require


def normalize(details, contract):
    fields(details, {"kind", "question", "reason", "resolution_criterion", "requirement_ids", "knowledge_refs"},
           {"kind", "question", "reason", "resolution_criterion"})
    require(isinstance(details["kind"], str) and details["kind"] in KINDS, "NEED_KIND", "Unknown Develop Need kind")
    for key in ("question", "reason", "resolution_criterion"):
        require(isinstance(details[key], str) and 0 < len(details[key].strip()) <= 2000, "NEED_DETAILS", key + " must be bounded nonblank text")
    result = copy.deepcopy(details)
    for key, known in (("requirement_ids", {r["id"] for r in contract.get("requirements", [])}),
                       ("knowledge_refs", {item["id"] for item in KNOWLEDGE_MAP})):
        refs = details.get(key, [])
        require(isinstance(refs, list) and len(refs) <= 32 and all(isinstance(ref, str) for ref in refs)
                and len(set(refs)) == len(refs) and set(refs) <= known, "NEED_REFERENCE", "Unknown or duplicate " + key)
        result[key] = list(refs)
    return result
