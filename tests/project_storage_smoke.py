"""Standard-library integration probe for the frozen control Harness Sandbox.

The outer observer measures this command, not the independence of its assertions.
Live namespace/socket regressions are executed separately on the operator path.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from harness_external.service import Harness
from harness_external.dispatch import invoke
from harness_external.task_storage import task_id_for
from harness_external.errors import HarnessError
from harness_external import worker
from change_lens import analyze

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

helper=load("project_smoke_client",ROOT/"skills/harness-workflow/scripts/harness_client.py")
installer=load("project_smoke_installer",ROOT/"scripts/install-runtime.py")


class ProjectStorageSmoke(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="project-store-smoke-")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.work=self.root/"작업 space"
        self.work.mkdir()
        (self.work/"value.txt").write_text("ready",encoding="utf-8")
        self.api=Harness(self.root/"shared")
        self.request={"operation":"start","request_id":"begin","project_workspace":str(self.work),
            "task_id":task_id_for("begin",str(self.work),"develop"),"arguments":{
            "domain":"develop","goal":"작업의 상세 기록은 프로젝트에, 선정한 교훈만 공용으로.","workspace":str(self.work),"mode":"strict",
            "parameters":{"profile":"structural","inputs":["value.txt"],"artifacts":["value.txt"],
                          "expectations":[{"id":"value","path":"value.txt","operator":"equals","expected":"ready"}]}}}
        self.started=invoke(self.api,self.request)

    def call(self,operation,arguments=None,key=None,**extra):
        return invoke(self.api,{"operation":operation,"arguments":{"run_id":self.started["run_id"],**(arguments or {})},
            "context":self.started["context"],"task_id":self.request["task_id"],"project_workspace":str(self.work),
            **({"request_id":key} if key else {}),**extra})

    def measure(self):
        with patch.object(worker,"spawn_worker",side_effect=lambda root,job,**kw:worker.execute_job(root,job)):
            return self.call("checkpoint",{"view":"brief","wait_seconds":0},"measure")

    def test_single_database_and_restart(self):
        self.assertTrue((self.work/".harness/tasks"/self.request["task_id"]/"runs.sqlite3").is_file())
        self.assertFalse((self.api.store.root/"tasks").exists())
        self.assertFalse((self.api.store.root/"runs.sqlite3").exists())
        self.assertEqual(invoke(Harness(self.api.store.root),self.request)["run_id"],self.started["run_id"])

    def test_control_files_are_not_sources(self):
        with self.assertRaises(HarnessError) as caught:
            self.call("revise",{"data":{"expected_revision":1,"parameters":{"inputs":["value.txt",".harness/private"]}}},"bad")
        self.assertEqual(caught.exception.code,"WORKSPACE_CONTROL_PATH")
        (self.work/".harness/private.py").write_text("raise RuntimeError('not source')\n",encoding="utf-8")
        self.assertEqual(analyze(self.work).modules,[])

    def test_optional_fields_are_untrusted_in_the_project_database(self):
        value={"fields":{"unknown":None,"empty":""},"extensions":{"exception":{"flag":False,"count":0}}}
        self.call("observe",{"data":{"note":"","payload":value}},"note")
        item=self.call("records",{"kind":"notes"})["items"][0]
        self.assertEqual(json.dumps(item["payload"],sort_keys=True),json.dumps({**value,"parents":[]},sort_keys=True))
        self.assertEqual(item["trust"],"untrusted")
        self.assertFalse((self.api.store.root/"knowledge").exists())

    def test_measured_facts_remain_local_and_selected_advice_is_shared(self):
        self.assertEqual(self.measure()["gates"]["status"],"passed")
        ref=self.call("records",{"kind":"measurements"})["items"][0]["observation_id"]
        note={"title":"A selected lesson","when":"A later task","lesson":"Keep source and control records separate.",
              "limitations":["One file probe."],"cited_observation_ids":[ref]}
        closed=self.call("finish",{"outcome":"completed"},"finish",knowledge=note)
        self.assertEqual(closed["knowledge_publication"]["status"],"published")
        self.assertEqual(closed["run_report"]["status"],"published")
        document=invoke(self.api,{"operation":"report-read","arguments":{"entry_id":self.started["run_id"]}})
        self.assertEqual(document["entry"]["record_hash"],closed["record"]["record_hash"])
        self.assertTrue(Path(closed["run_report"]["path"]).is_relative_to(self.api.store.root))
        self.assertFalse(document["gate_eligible"])
        self.assertFalse(closed["record"]["ready"])
        self.assertTrue((self.api.store.root/"knowledge/INDEX.md").is_file())
        self.assertEqual(list(self.api.store.root.rglob("*.sqlite3")),[])

    def test_portable_client_has_explicit_project_binding(self):
        config=helper.configure(self.root/"client.json",str(ROOT),str(self.api.store.root),ephemeral=True,python_executable=sys.executable)["config"]
        client=helper.Client(config)
        path=self.root/"binding.json"
        result=client.bind(self.started["run_id"],str(self.work),"develop",path,task_id=self.request["task_id"],project_workspace=str(self.work))
        self.assertTrue(result["ok"])
        binding=helper.read_json(str(path))
        self.assertEqual(binding["schema_version"],"harness-caller-binding-v3")
        self.assertEqual(helper.Client(config).validate_binding(binding)["run"]["run_id"],self.started["run_id"])

    def test_runtime_release_and_new_python_are_native_and_separate(self):
        result=installer.install(ROOT,self.root/"runtime",sys.executable,config_path=self.root/"native-client.json",
                                 state_dir=self.root/"new-shared",ephemeral=True)
        self.assertTrue(result["ok"])
        config=helper.read_json(str(self.root/"native-client.json"))
        self.assertEqual(config["task_storage"],"project")
        self.assertTrue(Path(config["python_runtime"]["identity"]["prefix"]).is_relative_to(self.root/"runtime"))
        self.assertFalse((self.root/"new-shared").exists())


if __name__=="__main__":
    result=unittest.TextTestRunner(verbosity=1,stream=sys.stderr).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProjectStorageSmoke))
    print(json.dumps({"tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"skipped":len(result.skipped)}))
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun==6 else 1)
