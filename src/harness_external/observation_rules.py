"""Core evaluates explicit normalized rules over opaque case identities."""


def evaluate(summary, rules):
    cases = {item["case_id"]: item for item in summary["cases"]}
    selected = [item for item in cases.values() if item["selected"] is True]
    comparisons = []
    def compare(name, expected, actual):
        comparisons.append({"kind": "comparison", "name": name, "operator": "equals", "expected": expected,
                            "observed": actual, "result": "pass" if expected == actual else "fail"})
    compare("observation_session_complete", True, summary["collection_complete"] and summary["session_complete"])
    compare("minimum_selected_observations", True, len(selected) >= rules["minimum_selected"])
    compare("selected_observations_accepted", True,
            bool(selected) and all(item["finished"] and item["outcome"] in rules["allowed_outcomes"] for item in selected))
    for key in rules["required_case_ids"]:
        item = cases.get(key)
        compare("required_observation:" + key, True,
                bool(item and item["selected"] and item["executed"] and item["finished"] and item["outcome"] in rules["allowed_outcomes"]))
    return comparisons
