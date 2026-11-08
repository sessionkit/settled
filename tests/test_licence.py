"""settled is AGPL-3.0-or-later, like the kit.

Ruled, not a choice. A release under the wrong licence
cannot be taken back, so every place the licence reaches is held here:
pyproject, the LICENSE file, the wheel's METADATA and files, the sdist, the
public tree (git archive), and no claim of the old licence left anywhere that ships.
"""
import hashlib
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPDX = "AGPL-3.0-or-later"
# sha256 of the full GNU AGPL-3.0 text, the same bytes as Session Kit's LICENSE
# (copied verbatim; `cmp` against it was clean when this was written).
AGPL3_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
# A licence claim naming the old licence, either word order ("<name> License",
# a pyproject `license = "<name>"`). The name is split so this file's own source
# never matches the pattern it holds.
_OLD = "M" + "IT"
MIT_CLAIM = re.compile(rf"\b{_OLD}\b.{{0,20}}licen[cs]e|licen[cs]e\W{{0,20}}{_OLD}\b", re.I)


def test_pyproject_names_agpl_and_the_licence_file():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert project["license"] == SPDX
    assert project["license-files"] == ["LICENSE"]


def test_licence_file_is_the_full_agpl3_text():
    body = (REPO / "LICENSE").read_bytes()
    assert body.lstrip().startswith(b"GNU AFFERO GENERAL PUBLIC LICENSE")
    assert hashlib.sha256(body).hexdigest() == AGPL3_SHA256


def test_readme_states_the_licence():
    readme = (REPO / "README.md").read_text()
    assert SPDX in readme
    assert not MIT_CLAIM.search(readme)


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    uv = shutil.which("uv")
    if not uv:
        pytest.fail("needs `uv` on PATH to build the wheel and sdist")
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run([uv, "build", "--out-dir", str(out), str(REPO)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def test_the_wheel_metadata_says_agpl_and_carries_licence(dist):
    whl = zipfile.ZipFile(next(dist.glob("settled-*.whl")))
    names = whl.namelist()
    meta = whl.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
    assert f"License-Expression: {SPDX}" in meta.splitlines()
    assert "License-File: LICENSE" in meta.splitlines()
    assert not MIT_CLAIM.search(meta)
    lic = [n for n in names if n.endswith(".dist-info/licenses/LICENSE")]
    assert lic, sorted(names)
    assert hashlib.sha256(whl.read(lic[0])).hexdigest() == AGPL3_SHA256


def test_the_sdist_carries_licence(dist):
    with tarfile.open(next(dist.glob("settled-*.tar.gz"))) as t:
        members = {m.name.split("/", 1)[1]: m for m in t.getmembers() if "/" in m.name}
        assert "LICENSE" in members, sorted(members)
        body = t.extractfile(members["LICENSE"]).read()
    assert hashlib.sha256(body).hexdigest() == AGPL3_SHA256


def test_the_public_tree_carries_licence_and_no_mit_claim(tmp_path):
    """git archive HEAD (export-ignore honoured) is what the public repo is cut from."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (an export has no .git)")
    tar = tmp_path / "public.tar"
    r = subprocess.run(["git", "archive", "--format=tar", "-o", str(tar), "HEAD"], cwd=REPO,
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    claims = []
    with tarfile.open(tar) as t:
        names = t.getnames()
        assert "LICENSE" in names
        for m in t.getmembers():
            if not m.isfile() or m.name == "LICENSE":
                continue
            text = t.extractfile(m).read().decode("utf-8", "replace")
            claims += [f"{m.name}: {line.strip()}" for line in text.splitlines() if MIT_CLAIM.search(line)]
    assert claims == []
