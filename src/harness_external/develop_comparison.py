"""Pure selection of existing checks and declared auxiliary test inputs."""
from .errors import fields, require, relative_path
from .develop_requirements import resolve_references


def schema():
    return {"type": "object", "required": ["check_ids", "test_paths"], "additionalProperties": False,
            "properties": {key: {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128}
                           for key in ("check_ids", "test_paths")}}


def prepare(parameters, contract, *, checks=None):
    fields(parameters, {"check_ids", "test_paths"}, {"check_ids", "test_paths"})
    for key in ("check_ids", "test_paths"):
        require(isinstance(parameters[key], list) and 1 <= len(parameters[key]) <= 128
                and all(isinstance(item, str) for item in parameters[key])
                and len(set(parameters[key])) == len(parameters[key]), "COMPARISON_SELECTION", "Select bounded unique check IDs and test paths")
    declarations = contract["checks"] if checks is None else [
        {**item["spec"]["parameters"], "check_id": item["check_id"], "check_key": item.get("definition_key")}
        for item in checks]
    indexed = {item["check_id"]: item for item in declarations if item.get("check_id")}
    selected_checks = resolve_references(parameters["check_ids"], indexed)
    paths = [relative_path(path) for path in parameters["test_paths"]]
    require(set(paths) <= set(contract["inputs"]) - set(contract["artifacts"]), "COMPARISON_SCOPE",
            "Shared test inputs must be admitted non-artifact inputs; product artifacts cannot be overlaid")
    require(all(indexed[key]["kind"] == "command" for key in selected_checks), "COMPARISON_SELECTION", "Select execution checks for a test comparison")
    return {"check_ids": selected_checks, "shared_inputs": sorted(paths)}
