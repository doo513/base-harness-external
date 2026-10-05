import pytest

from leasequeue import Queue


@pytest.mark.parametrize("dependencies", [None, []], ids=["none", "empty"])
def test_defaults_equivalent(tmp_path, dependencies):
    queue = Queue(tmp_path / "queue.sqlite3")
    assert queue.add("job", {"value": 1}, request_id="same") is True
    assert queue.add("job", {"value": 1}, depends_on=dependencies, request_id="same") is True


def test_replay_has_one_event(tmp_path):
    queue = Queue(tmp_path / "queue.sqlite3")
    queue.add("job", {}, request_id="same")
    queue.add("job", {}, request_id="same")
    assert len(queue.events()) == 1
