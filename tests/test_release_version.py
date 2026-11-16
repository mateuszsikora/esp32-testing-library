"""Release bumps and the CI shortcut operate on exact git trees, not PR labels."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/release-version.py"
spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
assert spec and spec.loader
release_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_version)


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], text=True
    ).strip()


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name in release_version.FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    (tmp_path / "README.md").write_text("Unchanged code and docs\n")
    monkeypatch.chdir(tmp_path)
    git("init", "--quiet")
    git("add", ".")
    git("commit", "--quiet", "-m", "base")
    git("tag", "base")
    return release_version


def bump(version: str = "99.0.0") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "bump", version], capture_output=True, text=True
    )


def commit() -> None:
    git("add", ".")
    git("commit", "--quiet", "-m", "change")


def test_version_only_bump_preserves_every_other_byte(repository: ModuleType) -> None:
    assert bump().returncode == 0
    commit()
    assert repository.classify("base", "HEAD") == "99.0.0"
    assert git("diff", "--numstat", "base", "HEAD").splitlines() == [
        f"1\t1\t{name}" for name in sorted(repository.FILES)
    ]


@pytest.mark.parametrize("version", ["v1.0.0", "01.2.3", "1.2", "1.2.3rc1", "1.2.3\n", "0.0.0"])
def test_invalid_or_lower_version_writes_nothing(repository: ModuleType, version: str) -> None:
    result = bump(version)
    assert result.returncode != 0
    assert git("status", "--porcelain") == ""


def test_same_version_writes_nothing(repository: ModuleType) -> None:
    current = repository.versions({name: Path(name).read_text() for name in repository.FILES})
    assert bump(current).returncode != 0
    assert git("status", "--porcelain") == ""


def test_inconsistent_versions_fail_before_writing(repository: ModuleType) -> None:
    lock = Path("runner/Cargo.lock")
    lock.write_text(lock.read_text().replace('name = "esp32tl-runner"', 'name = "other-runner"'))
    before = {name: Path(name).read_bytes() for name in repository.FILES}
    assert bump().returncode != 0
    assert before == {name: Path(name).read_bytes() for name in repository.FILES}


@pytest.mark.parametrize("extra", ["code", "dependency", "comment", "mode", "missing", "mismatch"])
def test_mixed_changes_cannot_skip_ci(repository: ModuleType, extra: str) -> None:
    assert bump().returncode == 0
    if extra == "code":
        Path("README.md").write_text("Another change\n")
    elif extra == "dependency":
        path = Path("runner/Cargo.toml")
        path.write_text(
            path.read_text().replace(
                'signal-hook = { version = "0.4.5"', 'signal-hook = { version = "0.4.6"'
            )
        )
    elif extra == "comment":
        with Path("pyproject.toml").open("a") as stream:
            stream.write("\n# Extra change\n")
    elif extra == "mode":
        Path("pyproject.toml").chmod(0o755)
    elif extra == "missing":
        git("checkout", "base", "--", "runner/Cargo.lock")
    else:
        path = Path("runner/Cargo.toml")
        path.write_text(path.read_text().replace('version = "99.0.0"', 'version = "98.0.0"'))
    commit()
    assert repository.classify("base", "HEAD") is None


def test_unchanged_and_reversed_diff_use_full_ci(repository: ModuleType) -> None:
    assert repository.classify("base", "HEAD") is None
    assert bump().returncode == 0
    commit()
    assert repository.classify("HEAD", "base") is None


def test_classifier_outputs_are_consumed_by_workflows(
    repository: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert bump().returncode == 0
    commit()
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "classify", "base"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "version_only=true\nversion=99.0.0\n"
    assert result.stdout == output.read_text()


def test_unknown_base_is_a_failure_not_a_ci_shortcut(repository: ModuleType) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "classify", "missing-ref"], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "version_only=true" not in result.stdout
