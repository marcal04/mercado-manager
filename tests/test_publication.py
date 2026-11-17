import os
from pathlib import Path
import shutil
import subprocess
import pytest
from tools.check_publication import scan
from tools.setup_local import setup


def test_private_files_are_rejected_and_empty_example_is_allowed(tmp_path):
    (tmp_path/".env.example").write_text("APP_ENCRYPTION_KEY=\n")
    (tmp_path/".env").write_text("PRIVATE=yes\n")
    (tmp_path/"backups").mkdir();(tmp_path/"backups"/"snapshot.mmbak").write_bytes(b"private")
    count,findings=scan(tmp_path)
    assert count==3 and len(findings)==2
    assert all(name!=".env.example" for name,_,_ in findings)


def test_tokens_and_operational_values_are_reported_without_contents(tmp_path):
    token="sk-"+"A"*40
    (tmp_path/"app.py").write_text("KEY="+repr(token))
    count,findings=scan(tmp_path)
    assert findings and all(token not in str(f) for f in findings)
    (tmp_path/"app.py").write_text("a private-value-"+"B"*24)
    reference=tmp_path.parent/(tmp_path.name+"-comparison.txt")
    reference.write_text("ML_CLIENT_SECRET=private-value-"+"B"*24)
    try:
        assert scan(tmp_path,compare_env=reference)[1][0][2]=="matches-operational-env"
    finally:reference.unlink()


def test_setup_generates_unique_credentials_and_never_overwrites(tmp_path):
    template=Path(__file__).resolve().parents[1]/".env.example"
    shutil.copy(template,tmp_path/".env.example")
    path=setup(tmp_path);before=path.read_bytes()
    assert "MARKETPLACE_WRITE_ENABLED=false" in before.decode()
    assert "AI_ENABLED=false" in before.decode()
    assert "APP_ENCRYPTION_KEY=\n" not in before.decode()
    with pytest.raises(FileExistsError):setup(tmp_path)
    assert path.read_bytes()==before


def test_git_ignore_and_staged_blob_are_checked(tmp_path,monkeypatch):
    if not shutil.which("git"):pytest.skip("Git not installed")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL",os.devnull);monkeypatch.setenv("GIT_CONFIG_NOSYSTEM","1")
    monkeypatch.delenv("GIT_DIR",raising=False);monkeypatch.delenv("GIT_WORK_TREE",raising=False)
    def command(*args):return subprocess.run(["git","-C",str(tmp_path),*args],capture_output=True)
    assert command("init","--quiet").returncode==0
    shutil.copy(Path(__file__).resolve().parents[1]/".gitignore",tmp_path/".gitignore")
    assert command("check-ignore","--no-index",".env").returncode==0
    assert command("check-ignore","--no-index",".env.example").returncode==1
    assert command("check-ignore","--no-index","backups/data/a.mmbak").returncode==0
    token="sk-"+"B"*40
    (tmp_path/"example.py").write_text("TOKEN="+repr(token))
    assert command("add","example.py").returncode==0
    (tmp_path/"example.py").write_text("TOKEN=None")
    assert scan(tmp_path,staged=True)[1][0][2]=="openai-token"


def test_only_reviewed_screenshot_hashes_are_allowed(tmp_path):
    import hashlib,json
    source=Path(__file__).resolve().parents[1]/"docs/images/01-visao-geral.jpg"
    folder=tmp_path/"docs/images";folder.mkdir(parents=True)
    target=folder/"example.jpg";shutil.copy(source,target)
    manifest={"images":{"docs/images/example.jpg":{"sha256":hashlib.sha256(target.read_bytes()).hexdigest(),"privacy_review":"synthetic_data_only"}}}
    (folder/"manifest.json").write_text(json.dumps(manifest))
    assert scan(tmp_path)[1]==[]
    target.write_bytes(target.read_bytes()+b"changed")
    assert scan(tmp_path)[1][0][2]=="unreviewed-binary"


def test_unreviewed_binary_is_not_accepted_just_because_it_is_an_image(tmp_path):
    (tmp_path/"unknown.jpg").write_bytes(bytes([255,216,255])+b"not reviewed")
    assert scan(tmp_path)[1][0][2]=="unreviewed-binary"
