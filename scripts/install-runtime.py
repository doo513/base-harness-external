#!/usr/bin/env python3
"""Install a versioned Linux runtime, not another project or task database.

No network/package installation, model call, existing-config replacement or Run
migration. Native Python environments are created at their final stable paths.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "harness-runtime-install-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def durable_json(path, value):
    with path.open("xb") as stream:
        stream.write(canonical(value)+b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def no_links(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Installation paths must not contain symbolic links")


def read_regular(path, limit=2*1024*1024):
    no_links(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("Runtime inputs must be bounded single-link regular files")
        value = stream.read(limit+1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode)
    if len(value)>limit or identity(before)!=identity(after) or identity(after)!=identity(path.stat()):
        raise ValueError("Runtime input changed while reading")
    return value, bool(before.st_mode & 0o111)


def inventory(source):
    names = {"README.md","INSTALL.md","POLICIES.md","LICENSE","NOTICE","pyproject.toml",
             "runtime/script/external-harness-sandbox.ts","runtime/packages/security/src/sandbox.ts",
             "scripts/harness-tool","scripts/harness-explore","scripts/install-skill.py","scripts/install-runtime.py"}
    for directory in ("src/harness", "src/harness_external", "src/change_lens", "skills/harness-workflow"):
        root = source / directory
        no_links(root)
        for path in root.rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            no_links(path)
            if path.is_file():
                names.add(path.relative_to(source).as_posix())
    if len(names)>512:
        raise ValueError("Runtime exceeds the bounded installation inventory")
    data, manifest, total = {}, {}, 0
    for name in sorted(names):
        value, executable = read_regular(source/name)
        total += len(value)
        if total > 16*1024*1024:
            raise ValueError("Runtime exceeds the installation byte limit")
        data[name] = value
        manifest[name] = {"sha256":digest(value),"size":len(value),"executable":executable}
    return data, manifest


def checked(argv, timeout=30):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode or len(result.stdout)>16384 or len(result.stderr)>16384:
        raise ValueError("Runtime setup command failed: " + result.stderr.decode("utf-8", errors="replace")[:1000])
    return json.loads(result.stdout) if result.stdout else None


def install(source, prefix, python_executable, *, config_path=None, state_dir=None, ephemeral=False):
    if sys.platform != "linux":
        raise ValueError("This installer runs inside Linux/WSL; it is not a Windows-native Sandbox")
    source, prefix = Path(source).absolute(), Path(prefix).expanduser().absolute()
    no_links(source)
    no_links(prefix)
    source, prefix = source.resolve(), prefix.resolve()
    if source.is_relative_to(prefix) or prefix.is_relative_to(source):
        raise ValueError("Runtime installation and development checkout must be disjoint")
    config_path = Path(config_path).expanduser().absolute() if config_path is not None else None
    if config_path is not None:
        no_links(config_path)
        if config_path.exists():
            raise FileExistsError("Existing configuration was preserved; select a new config path")
        if state_dir is None:
            raise ValueError("Config creation requires an explicit existing-layout state directory; this installer does not choose a storage architecture")
    selected = Path(python_executable).expanduser()
    if not selected.is_absolute() or not selected.is_file() or not os.access(selected,os.X_OK):
        raise ValueError("Select an absolute working Python executable")
    # Recreate a standard-library runtime; do not copy a venv with stale paths.
    selected = selected.resolve()
    probe = checked([str(selected),"-I","-c","import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),'implementation':sys.implementation.name}))"])
    if tuple(probe["version"]) < (3,11):
        raise ValueError("Python 3.11+ is required")
    binary = file_digest(selected)
    environment_id = digest(canonical({**probe,"executable":str(selected),"binary_sha256":binary}))
    data, files = inventory(source)
    source_id = digest(canonical(files))
    release_id = digest(canonical({"source_id":source_id,"environment_id":environment_id}))
    release = prefix / "releases" / release_id
    environment = prefix / "venvs" / environment_id
    manifest = {"schema_version":SCHEMA,"source_id":source_id,"files":files,"environment_id":environment_id}
    prefix.mkdir(parents=True,exist_ok=True,mode=0o700)
    if prefix.stat().st_uid != os.getuid() or prefix.stat().st_mode & 0o022:
        raise ValueError("Use a caller-owned installation directory without group/other write permission")
    import fcntl
    descriptor = os.open(prefix/".install.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,"rb+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        no_links(environment)
        marker = environment / "harness-python.json"
        expected_environment = {**probe,"executable":str(selected),"binary_sha256":binary}
        if environment.exists():
            if not marker.is_file() or json.loads(read_regular(marker)[0]) != expected_environment:
                raise ValueError("Existing Python environment is incomplete or differs; it was not overwritten")
        else:
            environment.parent.mkdir(exist_ok=True,mode=0o700)
            checked([str(selected),"-I","-m","venv","--without-pip",str(environment)],timeout=60)
            durable_json(marker,expected_environment)
        interpreter = environment / "bin/python"
        actual = checked([str(interpreter),"-I","-c","import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),'implementation':sys.implementation.name}))"])
        if actual != probe or file_digest(selected) != binary:
            raise ValueError("Selected Python changed during installation")
        no_links(release)
        if release.exists():
            current = json.loads(read_regular(release/"runtime-manifest.json")[0])
            if current != manifest or inventory(release)[1] != files:
                raise ValueError("Existing release differs; never repair a version in place")
        else:
            release.parent.mkdir(exist_ok=True,mode=0o700)
            with tempfile.TemporaryDirectory(prefix=".staging-",dir=release.parent) as directory:
                staged = Path(directory)
                for name,value in data.items():
                    target=staged/name
                    target.parent.mkdir(parents=True,exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(value)
                        stream.flush()
                        os.fsync(stream.fileno())
                    target.chmod(0o555 if files[name]["executable"] else 0o444)
                if inventory(source)[1] != files or inventory(staged)[1] != files:
                    raise ValueError("Source/copy changed during publication")
                durable_json(staged/"runtime-manifest.json",manifest)
                (staged/".venv").symlink_to(environment,target_is_directory=True)
                os.rename(staged,release)
            directory_fd=os.open(release.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        if not (release/".venv").is_symlink() or (release/".venv").resolve()!=environment:
            raise ValueError("Runtime Python reference changed; installation was not reconfigured")
        config_result = None
        if config_path is not None:
            argv=[str(interpreter),"-I",str(release/"skills/harness-workflow/scripts/harness_client.py"),"--config",str(config_path),"configure",
                  "--harness-repo",str(release),"--state-dir",str(Path(state_dir).expanduser().absolute()),
                  "--python-executable",str(interpreter)]
            if ephemeral:
                argv.append("--ephemeral")
            config_result=checked(argv)
            if not config_result.get("ok"):
                raise ValueError("Runtime installed but config creation failed: "+str(config_result.get("error")))
    return {"ok":True,"schema_version":SCHEMA,"runtime":str(release),"source_id":source_id,"release_id":release_id,"python":str(interpreter),
            "file_count":len(files),"config_path":str(config_path) if config_path else None,
            "storage_layout_changed":False,"existing_runs_migrated":False,"default_config_replaced":False,
            "new_task_storage":config_result["config"].get("task_storage","external") if config_result else None,
            "limitations":["No optional packages or Bun were downloaded; use the existing operator-provided Bun.",
                           "A new venv changes runtime identity. Preserve old runtimes/bindings for existing Runs; never rewrite their evidence.",
                           "Same-user installation is not a tamper-proof verifier."]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=ROOT)
    parser.add_argument("--prefix",type=Path,default=Path(os.environ.get("XDG_DATA_HOME",Path.home()/".local/share"))/"base-harness-external")
    parser.add_argument("--python-executable",default=sys.executable)
    parser.add_argument("--config",type=Path,help="create this NEW caller config; never overwrite an existing config")
    parser.add_argument("--state-dir",type=Path,help="explicit existing-layout state directory; required only when creating a config")
    parser.add_argument("--ephemeral",action="store_true",help="disposable installation tests only")
    args=parser.parse_args()
    try:
        result=install(args.source,args.prefix,args.python_executable,config_path=args.config,state_dir=args.state_dir,ephemeral=args.ephemeral)
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print(json.dumps({"ok":False,"error":str(error)},ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
