"""Pinned in-Sandbox pytest observer. Reports facts, never gate decisions."""
import json
import os
from pathlib import Path
import sys


def main():
    runtime, report_path, settings_json = sys.argv[1:]
    token = json.loads(Path("/opt/harness-runtime/capture.json").read_text())["token"]
    sys.path.insert(0, runtime)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest
    settings = json.loads(settings_json)
    output = open(report_path, "x", encoding="utf-8", buffering=1)
    sequence = 0
    def emit(event, **data):
        nonlocal sequence
        sequence += 1
        output.write(json.dumps({"version": 1, "token": token, "seq": sequence, "event": event, **data}, ensure_ascii=False) + "\n")
        output.flush()

    class Observer:
        def pytest_sessionstart(self, session):
            emit("session_start", framework="pytest", framework_version=pytest.__version__,
                 collection_paths=settings["paths"], args=settings["args"])

        def pytest_itemcollected(self, item):
            emit("discovered", case_id=item.nodeid, path=Path(item.path).relative_to(Path.cwd()).as_posix())

        def pytest_collectreport(self, report):
            if report.failed or report.skipped:
                emit("collection_issue", outcome=report.outcome, node_id=report.nodeid, message=str(report.longrepr)[:8000])

        def pytest_deselected(self, items):
            for item in items:
                emit("deselected", case_id=item.nodeid)

        def pytest_collection_finish(self, session):
            emit("collection_finish", selected=[item.nodeid for item in session.items], errors=session.testsfailed)

        def pytest_runtest_logstart(self, nodeid, location):
            emit("started", case_id=nodeid)

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_call(self, item):
            emit("call_started", case_id=item.nodeid)

        def pytest_runtest_logreport(self, report):
            emit("phase", case_id=report.nodeid, phase=report.when, outcome=report.outcome,
                 duration=report.duration, wasxfail=getattr(report, "wasxfail", None), message=str(report.longrepr)[:8000] if report.longrepr else None)

        def pytest_runtest_logfinish(self, nodeid, location):
            emit("finished", case_id=nodeid)

        def pytest_sessionfinish(self, session, exitstatus):
            emit("session_finish", exit_status=int(exitstatus), collected=session.testscollected)

    # Import the trusted runner before making Candidate modules importable.
    sys.path.insert(1, str(Path.cwd()))
    status = pytest.main([*settings["paths"], *settings["args"], "--rootdir=.", "-p", "no:cacheprovider"], plugins=[Observer()])
    output.close()
    raise SystemExit(int(status))


if __name__ == "__main__":
    main()
