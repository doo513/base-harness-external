"""Pure Domain advice views over a separately versioned, data-only catalog.

Only this loader/selection code belongs to the Domain implementation identity.
Catalog text is identified by map_hash and retained in each Run; copy edits do
not change criteria, invalidate measurements, or rewrite existing advice.
"""
from importlib.resources import files
from types import MappingProxyType

from harness.common import canonical_bytes, canonical_hash
from harness.json_codec import decode


KINDS = ("knowledge", "observation", "decision", "verification")
PERSPECTIVE_IDS = ("outcome", "existing_behavior", "design", "change", "verification")
KNOWLEDGE_IDS = ("compatibility", "structure", "runtime", "recovery", "verification")


def _load_catalog():
    # Package data only, once per process. No workspace/source discovery here.
    raw = files("harness_external").joinpath("develop_analysis_map.json").read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > 32000:
        raise ValueError("Analysis catalog exceeds 32 KiB")
    body = decode(raw)
    expected = {"schema_version", "revision", "need_kinds", "meaning", "persistence_required",
                "minimum_need_count", "need_quality", "analysis_perspectives", "knowledge_map", "discovery", "boundary"}
    if (not isinstance(body, dict) or set(body) != expected or body["schema_version"] != "develop-analysis-v2"
            or body["need_kinds"] != list(KINDS) or body["persistence_required"] is not False
            or type(body["minimum_need_count"]) is not int or body["minimum_need_count"] != 0
            or not isinstance(body["revision"], str) or not 0 < len(body["revision"]) <= 128):
        raise ValueError("Invalid data-only analysis catalog")
    for name, identifiers, required in (
        ("analysis_perspectives", PERSPECTIVE_IDS, {"id", "ask", "knowledge_refs"}),
        ("knowledge_map", KNOWLEDGE_IDS, {"id", "helps_answer", "when", "source_hints", "enables", "limitation"}),
    ):
        items = body[name]
        if (not isinstance(items, list) or len(items) != len(identifiers)
                or any(not isinstance(item, dict) or not required <= set(item)
                       or not set(item) <= required | ({"investigation"} if name == "analysis_perspectives" else set()) for item in items)
                or [item["id"] for item in items] != list(identifiers)):
            raise ValueError("Invalid analysis catalog inventory")
        for item in items:
            for key in required - {"knowledge_refs", "source_hints"}:
                if not isinstance(item[key], str) or not 0 < len(item[key]) <= 4000:
                    raise ValueError("Invalid analysis catalog text")
            if name == "analysis_perspectives":
                refs = item["knowledge_refs"]
                if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in KNOWLEDGE_IDS for ref in refs)
                        or len(set(refs)) != len(refs)):
                    raise ValueError("Invalid analysis catalog knowledge references")
                route = item.get("investigation")
                if route is not None and (not isinstance(route, dict) or set(route) != {"inspect", "decide", "check"}
                        or any(not isinstance(text, str) or not 0 < len(text) <= 400 for text in route.values())):
                    raise ValueError("Invalid analysis investigation route")
            else:
                hints = item["source_hints"]
                if (not isinstance(hints, list) or not 1 <= len(hints) <= 8
                        or any(not isinstance(hint, str) or not 0 < len(hint) <= 4000 for hint in hints)):
                    raise ValueError("Invalid analysis catalog source hints")
    for name, keys in (("need_quality", {"unknown", "decision_relevant", "investigable", "sufficient"}),
                       ("discovery", {"select", "inspect", "apply", "revisit"})):
        value = body[name]
        if (not isinstance(value, dict) or set(value) != keys
                or any(not isinstance(text, str) or not 0 < len(text) <= 4000 for text in value.values())):
            raise ValueError("Invalid analysis catalog guidance")
    if any(not isinstance(body[name], str) or not 0 < len(body[name]) <= 4000 for name in ("meaning", "boundary")):
        raise ValueError("Invalid analysis catalog boundary")
    return body


# A damaged NEW catalog must not make an already stored map unreadable.
# guidance() reports the failure; select_guidance() depends only on its input.
_GUIDANCE_ERROR = None
try:
    _BODY = _load_catalog()
except (OSError, ValueError, TypeError, KeyError) as error:
    _GUIDANCE_ERROR = str(error)
    _BODY = {"need_quality": {}, "analysis_perspectives": [],
             "knowledge_map": [{"id": key} for key in KNOWLEDGE_IDS]}
def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


NEED_QUALITY = _freeze(_BODY["need_quality"])
PERSPECTIVES = _freeze(_BODY["analysis_perspectives"])
KNOWLEDGE_MAP = _freeze(_BODY["knowledge_map"])
PERSPECTIVE_INDEX = MappingProxyType({item["id"]: item for item in PERSPECTIVES})
KNOWLEDGE_INDEX = MappingProxyType({item["id"]: item for item in KNOWLEDGE_MAP})
_GUIDANCE_JSON = canonical_bytes({**_BODY, "map_hash": canonical_hash(_BODY)}).decode("utf-8")
del _BODY


def guidance(*, perspectives=None, knowledge=None):
    """Default stays complete; optional explicit IDs do not route by goal text."""
    if _GUIDANCE_ERROR is not None:
        raise ValueError("Analysis catalog unavailable: " + _GUIDANCE_ERROR)
    body = decode(_GUIDANCE_JSON)
    if perspectives is None and knowledge is None:
        return body
    return select_guidance(body, perspectives=perspectives, knowledge=knowledge)


def select_guidance(source, *, perspectives=None, knowledge=None):
    """Project a supplied map so resumed reads use their stored advice version."""
    body = decode(canonical_bytes(source).decode("utf-8"))
    if (not isinstance(body, dict) or body.get("schema_version") != "develop-analysis-v2"
            or body.get("map_hash") != canonical_hash({key: value for key, value in body.items() if key != "map_hash"})):
        raise ValueError("Invalid or unsupported analysis guidance source")
    if perspectives is None and knowledge is None:
        return body
    indexes = []
    for name in ("analysis_perspectives", "knowledge_map"):
        items = body.get(name)
        if (not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in items)
                or len({item["id"] for item in items}) != len(items)):
            raise ValueError("Invalid analysis guidance inventory")
        indexes.append({item["id"]: item for item in items})
    for selection, index in ((perspectives, indexes[0]), (knowledge, indexes[1])):
        if selection is not None and (not isinstance(selection, (list, tuple))
                or any(not isinstance(key, str) or key not in index for key in selection)
                or len(set(selection)) != len(selection)):
            raise ValueError("Unknown or duplicate advisory map ID")
    source_hash = body["map_hash"]
    if perspectives is not None:
        selected = set(perspectives)
        body["analysis_perspectives"] = [item for item in body["analysis_perspectives"] if item["id"] in selected]
    if knowledge is None and perspectives is not None:
        references = [item.get("knowledge_refs") for item in body["analysis_perspectives"]]
        if any(not isinstance(refs, list) or any(not isinstance(key, str) or key not in indexes[1] for key in refs) for refs in references):
            raise ValueError("Invalid analysis guidance knowledge references")
        knowledge = {key for refs in references for key in refs}
    if knowledge is not None:
        selected = set(knowledge)
        body["knowledge_map"] = [item for item in body["knowledge_map"] if item["id"] in selected]
    body["source_map_hash"] = source_hash
    body["selection"] = {"perspectives": [item["id"] for item in body["analysis_perspectives"]],
                         "knowledge": [item["id"] for item in body["knowledge_map"]]}
    body["map_hash"] = canonical_hash({key: value for key, value in body.items() if key != "map_hash"})
    return body
