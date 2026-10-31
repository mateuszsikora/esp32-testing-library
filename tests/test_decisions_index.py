"""scripts/decisions-index.py, which generates docs/decisions/README.md."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "decisions-index.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("decisions_index", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


decisions_index = load()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A repository with a decision file for every domain."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    decisions = tmp_path / "docs" / "decisions"
    decisions.mkdir(parents=True)
    for domain in decisions_index.DOMAINS:
        (decisions / f"{domain}.md").write_text(f"# {domain}\n")
    return tmp_path


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=root, capture_output=True, text=True
    )


def test_the_index_lists_every_decision_with_its_anchor_and_status(root: Path) -> None:
    (root / "docs/decisions/ci.md").write_text(
        "# CI\n\n## Builds go through the `cache`, not artifacts\n\nStatus: decided (#48)\n\n"
        "```markdown\n## Not a decision\n```\n\n"
        "## Builds go through the `cache`, not artifacts\n\nStatus: superseded (#60)\n\n"
        "## Windows | macOS later\n\nStatus: postponed (#17)\n"
    )
    assert run(root).returncode == 0
    index = (root / "docs/decisions/README.md").read_text()
    assert (
        "| [Builds go through the `cache`, not artifacts]"
        "(ci.md#builds-go-through-the-cache-not-artifacts) | `ci.md` | decided (#48) |"
    ) in index
    assert "(ci.md#builds-go-through-the-cache-not-artifacts-1) | `ci.md` | superseded" in index
    assert "| [Windows \\| macOS later](ci.md#windows--macos-later) |" in index
    assert "Not a decision" not in index
    assert "[`docs.md`](docs.md) holds no decision yet." in index
    assert run(root, "--check").returncode == 0


def test_check_fails_on_a_stale_index(root: Path) -> None:
    assert run(root).returncode == 0
    (root / "docs/decisions/ci.md").write_text("# CI\n\n## New\n\nStatus: decided\n")
    result = run(root, "--check")
    assert result.returncode == 1
    assert "docs/decisions/README.md is stale" in result.stderr


def test_check_fails_on_a_file_it_does_not_know(root: Path) -> None:
    assert run(root).returncode == 0
    (root / "docs/decisions/security.md").write_text("# Security\n")
    result = run(root, "--check")
    assert "docs/decisions/security.md is not in DOMAINS" in result.stderr
    assert run(root).returncode == 1, "nor does it write an index that leaves the file out"


def test_check_fails_on_a_missing_file(root: Path) -> None:
    (root / "docs/decisions/ci.md").unlink()
    assert "docs/decisions/ci.md is missing" in run(root, "--check").stderr


def test_a_decision_needs_a_status(root: Path) -> None:
    (root / "docs/decisions/ci.md").write_text("# CI\n\n## Something\n\nWe chose it.\n")
    result = run(root)
    assert result.returncode == 1
    assert '"Something" has no status' in result.stderr


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Link protocol: runner ⇄ harness", "link-protocol-runner--harness"),
        ("5.1 `hello`", "51-hello"),
        ("5.3 `uart_cfg` *ext*", "53-uart_cfg-ext"),
        ("Decisions: `docs/decisions/<domain>.md`", "decisions-docsdecisionsdomainmd"),
        ("See [the plan](plan.md)", "see-the-plan"),
    ],
)
def test_github_slug(heading: str, slug: str) -> None:
    assert decisions_index.github_slug(heading) == slug
