"""Equivalence and work-count regression for request-local Domain lookup."""
import copy
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from harness_external.develop_requirements import resolve_references, validate_bindings
from harness_external.errors import HarnessError, require

# Pinned pre-change algorithms, e35df0f. They are an equivalence oracle, not runtime code.
def legacy_resolve(references, check_index, *, path="/check_ids", eligible_ids=None):
    allowed = set(check_index) if eligible_ids is None else set(eligible_ids)
    aliases = {}
    for key, check in check_index.items():
        definition = check.get("check_key", check.get("definition_key")) or ""
        if definition.startswith("named:"):
            aliases.setdefault(definition[6:], set()).add(key)
    resolved, diagnostics = [], []
    for index, ref in enumerate(references):
        # Canonical IDs retain their original meaning. Aliases are a fallback,
        # never another candidate when the caller already supplied an exact ID.
        options = {ref} if ref in check_index else aliases.get(ref, set())
        if len(options) != 1 or not options <= allowed:
            diagnostics.append({"path": path + "/" + str(index), "code": "REQUIREMENT_REFERENCE",
                                "message": "Use one admitted logical Check ID: " + ", ".join(sorted(allowed)[:16])})
        else:
            resolved.append(next(iter(options)))
    if diagnostics:
        raise HarnessError("REQUIREMENT_REFERENCE", "Unknown, ambiguous or inadmissible Check reference", diagnostics=diagnostics[:64], diagnostic_count=len(diagnostics))
    require(len(set(resolved)) == len(resolved), "REQUIREMENT_REFERENCE", "References resolve to the same Check more than once")
    return resolved



def legacy_validate(requirements, check_index, *, eligible_ids=None, allow_unmapped=True):
    allowed = set(check_index) if eligible_ids is None else set(eligible_ids)
    mapped = set()
    for index, item in enumerate(requirements):
        references = legacy_resolve(item["check_ids"], check_index, path="/requirements/" + str(index) + "/check_ids", eligible_ids=allowed)
        item["check_ids"] = references
        require(allow_unmapped or bool(references), "REQUIREMENT_UNMAPPED",
                "Acceptance requirement " + item["id"] + " must reference at least one admitted Check")
        require(set(references) <= allowed and set(references) <= set(check_index),
                "REQUIREMENT_REFERENCE", "Requirements must reference admitted checks with explicit logical IDs")
        minimum = item["minimum_evidence"]
        if references or not allow_unmapped:
            require(minimum not in {"command", "testcase"} or any(check_index[key]["kind"] == "command" for key in references),
                    "REQUIREMENT_EVIDENCE", "A command requirement cannot be represented by file-only checks")
            require(minimum != "testcase" or any(check_index[key].get("adapter") for key in references),
                    "REQUIREMENT_EVIDENCE", "A testcase requirement needs a case-aware check")
        mapped.update(references)
    return mapped



class CountingIndex(dict):
    def __init__(self, *args):
        super().__init__(*args)
        self.scans = 0

    def items(self):
        self.scans += 1
        return super().items()


def outcome(fn, requirements, index, **options):
    values = copy.deepcopy(requirements)
    try:
        return ("ok", fn(values, index, **options), values)
    except HarnessError as error:
        return ("error", error.as_dict(), values)


class ReferenceIndexTests(unittest.TestCase):
    def test_one_index_build_for_hundred_requirements(self):
        index = CountingIndex({f"domain.c{i}": {"check_key": f"named:c{i}", "kind": "file"} for i in range(128)})
        requirements = [{"id": f"R{i}", "check_ids": [f"c{i}"], "minimum_evidence": "file"} for i in range(100)]
        self.assertEqual(len(validate_bindings(requirements, index)), 100)
        self.assertEqual(index.scans, 1)

    def test_empty_requirements(self):
        self.assertEqual(validate_bindings([], {}), set())

    def test_fresh_index_after_check_and_eligibility_change(self):
        index = {"a": {"check_key": "named:x"}}
        self.assertEqual(resolve_references(["x"], index), ["a"])
        index["b"] = {"check_key": "named:x"}
        with self.assertRaises(HarnessError):
            resolve_references(["x"], index)
        self.assertEqual(resolve_references(["a"], index, eligible_ids=["a"]), ["a"])
        with self.assertRaises(HarnessError):
            resolve_references(["a"], index, eligible_ids=["b"])

    def test_diagnostics_cap_and_total_unchanged(self):
        args = [f"missing{i}" for i in range(128)]
        results = []
        for fn in (legacy_resolve, resolve_references):
            with self.assertRaises(HarnessError) as caught:
                fn(args, {"a": {}}, path="/r/0/check_ids")
            results.append(caught.exception.as_dict())
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1]["diagnostic_count"], 128)
        self.assertEqual(len(results[1]["diagnostics"]), 64)

    def test_exact_id_cannot_be_replaced_by_an_eligible_alias(self):
        index = {"domain.probe": {"check_key": "named:probe"}, "second": {"check_key": "named:domain.probe"}}
        self.assertEqual(resolve_references(["domain.probe"], index), ["domain.probe"])
        with self.assertRaises(HarnessError):
            resolve_references(["domain.probe"], index, eligible_ids=["second"])

    def test_duplicate_resolved_reference_unchanged(self):
        index = {"a": {"check_key": "named:alias", "kind": "file"}}
        r = [{"id": "R1", "check_ids": ["a", "alias"], "minimum_evidence": "file"}]
        self.assertEqual(outcome(legacy_validate, r, index), outcome(validate_bindings, r, index))

    def test_randomized_results_errors_and_partial_mutations_match(self):
        randomizer = random.Random(4711)
        for _ in range(500):
            index = {f"domain.c{i}": {"check_key": f"named:c{randomizer.randrange(12)}",
                      "kind": randomizer.choice(["command", "file"]),
                      "adapter": randomizer.choice([None, {"id": "fixture"}])} for i in range(16)}
            pool = list(index) + [f"c{i}" for i in range(16)] + ["unknown"]
            requirements = [{"id": f"R{i}", "check_ids": randomizer.sample(pool, randomizer.randrange(4)),
                             "minimum_evidence": randomizer.choice(["file", "command", "testcase"])}
                            for i in range(randomizer.randrange(12))]
            options = {"eligible_ids": randomizer.sample(list(index), randomizer.randrange(17)),
                       "allow_unmapped": randomizer.choice([True, False])}
            self.assertEqual(outcome(legacy_validate, requirements, index, **options),
                             outcome(validate_bindings, requirements, index, **options))

    def test_successful_full_declaration_matches(self):
        index = {f"domain.c{i}": {"definition_key": f"named:c{i}", "kind": "command", "adapter": {"id": "probe"}} for i in range(144)}
        requirements = [{"id": f"R{i}", "check_ids": [f"c{i}"], "minimum_evidence": "testcase"} for i in range(100)]
        self.assertEqual(outcome(legacy_validate, requirements, index), outcome(validate_bindings, requirements, index))


if __name__ == "__main__":
    unittest.main()
