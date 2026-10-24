import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("runtime_installer",ROOT/"scripts/install-runtime.py")
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_native_release_is_reusable_without_changing_storage_layout(tmp_path):
    prefix=tmp_path/"native"
    config=tmp_path/"config/client.json"
    result=installer.install(ROOT,prefix,sys.executable,config_path=config,state_dir=tmp_path/"shared",ephemeral=True)
    assert result["ok"] and not result["existing_runs_migrated"]
    body=json.loads(config.read_text(encoding="utf-8"))
    assert body["state_dir"]==str(tmp_path/"shared") and body["task_storage"]=="project"
    assert result["new_task_storage"]=="project"
    assert result["storage_layout_changed"] is False
    assert Path(body["python_runtime"]["identity"]["prefix"]).is_relative_to(prefix)
    assert Path(body["harness_repo"]).is_relative_to(prefix)
    repeated=installer.install(ROOT,prefix,sys.executable)
    assert repeated["runtime"]==result["runtime"] and repeated["python"]==result["python"]
    command=[result["python"],"-I",str(Path(result["runtime"])/"skills/harness-workflow/scripts/harness_client.py"),"describe","--operation","start"]
    process=subprocess.run(command,capture_output=True,text=True,encoding="utf-8",timeout=20)
    assert process.returncode==0 and json.loads(process.stdout)["ok"]
    assert not list(prefix.rglob("*.sqlite3"))
    assert not (tmp_path/"shared").exists()
    assert not (Path(result["runtime"])/".harness").exists()
    before=config.read_bytes()
    with pytest.raises(FileExistsError):
        installer.install(ROOT,prefix,sys.executable,config_path=config,state_dir=tmp_path/"shared",ephemeral=True)
    assert config.read_bytes()==before
    changed=Path(result["runtime"])/"src/harness_external/__init__.py"
    changed.chmod(0o644)
    changed.write_text("changed\n",encoding="utf-8")
    with pytest.raises(ValueError,match="release differs"):
        installer.install(ROOT,prefix,sys.executable)
    assert changed.read_text(encoding="utf-8")=="changed\n"


def test_runtime_install_cannot_overlap_development_checkout(tmp_path):
    with pytest.raises(ValueError,match="disjoint"):
        installer.install(ROOT,ROOT/".harness/runtime",sys.executable)


def test_runtime_inventory_rejects_source_links(tmp_path):
    root=tmp_path/"source"
    (root/"src/harness").mkdir(parents=True)
    (root/"src/harness/link.py").symlink_to(ROOT/"src/harness/common.py")
    with pytest.raises(ValueError,match="symbolic"):
        installer.inventory(root)
