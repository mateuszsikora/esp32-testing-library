"""scripts/check-docs.py, the documentation contract, run against temporary git repositories.

Each test builds a repository whose `main` holds a few owned files and documentation that passes
the integrity checks, then changes it on a branch the way a contributor would. They need git, not
Docker or the runner.
"""

import importlib.util
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-docs.py"


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_docs = load("check_docs", SCRIPT)
decisions_index = load("decisions_index", ROOT / "scripts" / "decisions-index.py")

# Git with no user or system configuration: no signing, no hooks, a fixed identity. CI and
# SKIP_DOCS_CHECK are taken out, so the check behaves as it does on a contributor's machine.
ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}
ENV.pop("CI", None)
ENV.pop("SKIP_DOCS_CHECK", None)

WORKFLOW = """\
jobs:
  test:
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
"""

PYPROJECT = """\
[project]
name = "x"
dependencies = [
    "pytest>=8",
    "ruff==0.17.3",
]
"""


def features_index() -> str:
    """docs/features/README.md as the check expects it: one table row per rule."""
    rows = [
        f"| {r.id} | `{r.source.replace('|', chr(92) + '|')}` "
        f"| `{r.docs.replace('|', chr(92) + '|')}` | {r.message} |"
        for r in check_docs.RULES
    ]
    return "# Features\n\n| # | source | docs | message |\n|---|---|---|---|\n" + "\n".join(rows)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=ENV, capture_output=True, text=True, check=True
    ).stdout


def write(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)


def commit(repo: Path, message: str = "change") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)


def run(repo: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        env={**ENV, **env},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    """`main` with owned files and their documentation, checked out on branch `work`."""
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "esp32tl/backends/esp32sim.py", "x = 1\n")
    write(tmp_path, "esp32tl/py.typed", "")
    write(tmp_path, "runner/src/main.rs", "fn main() {}\n")
    write(tmp_path, "tests/test_x.py", "def test_x() -> None: ...\n")
    write(tmp_path, ".github/workflows/ci.yml", WORKFLOW)
    write(tmp_path, "pyproject.toml", PYPROJECT)
    write(tmp_path, "docs/features/README.md", features_index())
    write(tmp_path, "docs/features/esp32sim-backend-user.md", "# Backend\n\n## Use\n")
    # The decisions index links the guide's section on decisions.
    write(tmp_path, "docs/guides/documentation.md", "## Decisions: `docs/decisions/<domain>.md`\n")
    write(tmp_path, "docs/implementation-plan.md", "# Plan\n\n## 3. Architecture\n\n### M0. Go\n")
    for domain in decisions_index.DOMAINS:
        write(tmp_path, f"docs/decisions/{domain}.md", f"# {domain}\n\n## A\n\nStatus: decided\n")
    index, errors = decisions_index.generate(tmp_path)
    assert not errors
    write(tmp_path, "docs/decisions/README.md", index)
    commit(tmp_path, "base")
    git(tmp_path, "switch", "-q", "-c", "work")
    yield tmp_path


def passes(result: subprocess.CompletedProcess[str]) -> bool:
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def test_the_fixture_passes(repo: Path) -> None:
    result = run(repo)
    assert passes(result), result.stdout
    assert "0 changed files since main" in result.stdout


def test_an_owned_path_without_docs_fails(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "x = 2\n")
    commit(repo)
    result = run(repo)
    assert not passes(result)
    assert "✗ esp32sim backend changed" in result.stdout
    assert "[F3]" in result.stdout
    assert "AGENTS.md" in result.stdout


def test_an_owned_path_with_docs_passes(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "x = 2\n")
    write(repo, "docs/features/esp32sim-backend-user.md", "# Backend\n\n## Use\n\nMore.\n")
    commit(repo)
    assert passes(run(repo))


def test_tests_only_pass(repo: Path) -> None:
    write(repo, "tests/test_x.py", "def test_y() -> None: ...\n")
    write(repo, "runner/src/tests.rs", "#[test]\nfn t() {}\n")
    write(repo, "runner/src/cli/tests.rs", "#[test]\nfn t() {}\n")
    commit(repo)
    assert passes(run(repo))


def test_an_untracked_doc_counts(repo: Path) -> None:
    write(repo, "runner/src/main.rs", "fn main() { }\n")
    commit(repo)
    assert not passes(run(repo))
    write(repo, "docs/features/runner-user.md", "# Runner\n")
    assert passes(run(repo))


def test_staged_and_unstaged_changes_count(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "x = 2\n")
    assert not passes(run(repo)), "unstaged"
    git(repo, "add", "-A")
    assert not passes(run(repo)), "staged"


def test_an_empty_diff_passes_on_the_branch_and_on_main(repo: Path) -> None:
    assert passes(run(repo))
    git(repo, "switch", "-q", "main")
    assert passes(run(repo))


def test_a_deleted_owned_file_needs_its_docs(repo: Path) -> None:
    (repo / "esp32tl/backends/esp32sim.py").unlink()
    commit(repo)
    assert not passes(run(repo))


def test_a_rename_counts_for_the_old_path(repo: Path) -> None:
    (repo / "tools").mkdir()
    git(repo, "mv", "esp32tl/backends/esp32sim.py", "tools/esp32sim.py")
    write(repo, "docs/stage0-findings.md", "# Stage 0\n")
    commit(repo)
    result = run(repo)
    assert not passes(result)
    assert "[F3]" in result.stdout


def test_version_only_bumps_pass(repo: Path) -> None:
    write(repo, ".github/workflows/ci.yml", WORKFLOW.replace("@v6", "@v7"))
    write(repo, "pyproject.toml", PYPROJECT.replace("0.17.3", "0.18.0").replace(">=8", ">=9"))
    commit(repo)
    assert passes(run(repo))


def test_a_bump_with_another_change_needs_docs(repo: Path) -> None:
    write(repo, ".github/workflows/ci.yml", WORKFLOW.replace("@v6", "@v7") + "      - run: x\n")
    commit(repo)
    result = run(repo)
    assert not passes(result)
    assert "[C1]" in result.stdout


def test_a_new_dependency_is_no_bump(repo: Path) -> None:
    write(repo, "pyproject.toml", PYPROJECT.replace('    "ruff', '    "mypy==2.4.1",\n    "ruff'))
    commit(repo)
    assert "[C7]" in run(repo).stdout


def test_a_crashing_bump_filter_fails_the_check(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, ".github/workflows/ci.yml", WORKFLOW.replace("@v6", "@v7"))
    commit(repo)

    def crash(*_: object) -> bool:
        raise RuntimeError("boom")

    for key in ("CI", "SKIP_DOCS_CHECK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(check_docs, "version_bump_only", crash)
    monkeypatch.chdir(repo)
    assert check_docs.main([]) == 1
    out = capsys.readouterr().out
    assert "✗ The version-bump filter failed on .github/workflows/ci.yml" in out
    # The path stays in, so its own rule fails too.
    assert "[C1]" in out


def test_no_base_exits_2(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "trunk")
    commit(tmp_path, "base")
    result = run(tmp_path)
    assert result.returncode == 2
    assert "no origin/main or main to compare with" in result.stderr


def test_an_unknown_base_exits_2(repo: Path) -> None:
    result = run(repo, "--base", "release/9")
    assert result.returncode == 2
    assert "no release/9 to compare with" in result.stderr


def test_base_names_the_branch_to_compare_with(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "x = 2\n")
    commit(repo)
    git(repo, "branch", "release")
    assert passes(run(repo, "--base", "release"))
    assert not passes(run(repo, "--base", "main"))


def test_origin_main_comes_before_main(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "x = 2\n")
    commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    result = run(repo)
    assert passes(result)
    assert "since origin/main" in result.stdout


def test_the_protocol_files_belong_to_the_protocol(repo: Path) -> None:
    write(repo, "runner/src/protocol.rs", "\n")
    commit(repo)
    result = run(repo)
    assert "[F9]" in result.stdout
    assert "[F6]" not in result.stdout
    write(repo, "docs/protocol.md", "# Protocol\n")
    assert passes(run(repo))


def test_board_profiles_belong_to_their_documents(repo: Path) -> None:
    write(repo, "esp32tl/boards/esp32-devkitc.toml", 'name = "esp32-devkitc"\n')
    commit(repo)
    result = run(repo)
    assert "[F13]" in result.stdout
    assert "[F12]" not in result.stdout
    write(repo, "docs/features/board-profiles-technical.md", "# Boards\n")
    assert passes(run(repo))


def test_the_recording_format_and_replay_belong_to_their_documents(repo: Path) -> None:
    write(repo, "esp32tl/recording.py", '"""Recordings."""\n')
    write(repo, "esp32tl/sources/replay.py", '"""Replay."""\n')
    commit(repo)
    result = run(repo)
    assert "[F14]" in result.stdout and "[F15]" in result.stdout
    assert "[F12]" not in result.stdout
    write(repo, "docs/recording-format.md", "# Recording format\n")
    write(repo, "docs/features/replay-user.md", "# Replay\n")
    assert passes(run(repo))


def test_the_units_and_the_models_belong_to_their_documents(repo: Path) -> None:
    write(repo, "esp32tl/units.py", '"""Units."""\n')
    write(repo, "esp32tl/sources/model/gnss.py", '"""GNSS."""\n')
    commit(repo)
    result = run(repo)
    assert result.stdout.count("[F17]") == 1
    assert "[F12]" not in result.stdout
    write(repo, "docs/features/models-technical.md", "# Models\n")
    assert passes(run(repo))


def test_the_bluetooth_peer_belongs_to_its_documents(repo: Path) -> None:
    write(repo, "esp32tl/bluetooth/peer.py", '"""Peer."""\n')
    commit(repo)
    result = run(repo)
    assert result.stdout.count("[F18]") == 1
    assert "[F12]" not in result.stdout
    write(repo, "docs/features/bluetooth-peer-user.md", "# Bluetooth peer\n")
    assert passes(run(repo))


def test_matching_belongs_to_its_documents(repo: Path) -> None:
    write(repo, "esp32tl/matching.py", '"""Matching."""\n')
    commit(repo)
    result = run(repo)
    assert result.stdout.count("[F21]") == 1
    assert "[F12]" not in result.stdout
    write(repo, "docs/features/matching-user.md", "# Matching\n")
    assert passes(run(repo))


def test_an_unowned_package_path_needs_a_feature_doc(repo: Path) -> None:
    write(repo, "esp32tl/new_feature.py", '"""New feature."""\n')
    write(repo, "esp32tl/py.typed", "\n")
    commit(repo)
    result = run(repo)
    assert "[F12]" in result.stdout
    assert result.stdout.count("✗") == 1, "py.typed is nobody's"
    write(repo, "docs/features/esp32sim-backend-user.md", "# Backend\n\n## Use\n\nMore.\n")
    assert "[F12]" in run(repo).stdout, "an edit to an existing feature doc is not a new one"
    write(repo, "docs/features/new-feature-user.md", "# New feature\n")
    assert passes(run(repo))
    commit(repo)
    assert passes(run(repo)), "committed, the new doc is still new since the merge base"


def test_a_moved_feature_doc_is_no_new_one(repo: Path) -> None:
    write(repo, "esp32tl/new_feature.py", '"""New feature."""\n')
    git(repo, "mv", "docs/features/esp32sim-backend-user.md", "docs/features/new-feature-user.md")
    commit(repo)
    assert "[F12]" in run(repo).stdout


def test_skip_is_local_only(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "x = 2\n")
    commit(repo)
    assert passes(run(repo, SKIP_DOCS_CHECK="1"))
    result = run(repo, SKIP_DOCS_CHECK="1", CI="true")
    assert not passes(result)
    assert "for local work only" in result.stdout
    assert passes(run(repo, SKIP_DOCS_CHECK="1", CI="false"))


def test_a_decision_linked_by_line_number_fails(repo: Path) -> None:
    write(repo, "docs/guides/x.md", "See [this](../decisions/ci.md#L3).\n")
    result = run(repo)
    assert not passes(result)
    assert "links a decision by line number" in result.stdout


def test_a_link_to_a_missing_heading_fails(repo: Path) -> None:
    write(repo, "docs/guides/x.md", "[a](../decisions/ci.md#a) [b](../decisions/ci.md#b)\n")
    result = run(repo)
    assert "links to docs/decisions/ci.md#b, no heading there" in result.stdout
    assert "#a," not in result.stdout


def test_a_link_to_a_missing_file_fails(repo: Path) -> None:
    write(repo, "docs/guides/x.md", "[a](gone.md) and, in code, `[b](also-gone.md)`\n")
    result = run(repo)
    assert "links to docs/guides/gone.md, which does not exist" in result.stdout
    assert "also-gone" not in result.stdout


def test_a_code_reference_to_a_missing_anchor_fails(repo: Path) -> None:
    write(repo, "esp32tl/backends/esp32sim.py", "# docs/decisions/ci.md#nope\nx = 1\n")
    write(repo, "docs/features/esp32sim-backend-user.md", "# Backend\n\n## Use\n\nMore.\n")
    result = run(repo)
    assert "links to docs/decisions/ci.md#nope, no heading there" in result.stdout


def test_a_plan_reference_must_name_a_heading(repo: Path) -> None:
    write(repo, "tools/x.py", "# plan §3, M0\n# plan §4.2\n# plan M9, §6\n")
    write(repo, "docs/stage0-findings.md", "# Stage 0\n")
    result = run(repo)
    assert "plan §3" not in result.stdout and "plan M0" not in result.stdout
    for ref in ("§4.2", "M9", "§6"):
        assert f"tools/x.py: plan {ref} is no heading" in result.stdout


def test_the_rules_table_must_match_the_script(repo: Path) -> None:
    index = features_index().replace("| F3 | `^esp32tl/backends/", "| F3 | `^esp32tl/backend/")
    write(repo, "docs/features/README.md", index + "\n| F99 | `^x` | `^y` | z |\n")
    result = run(repo)
    assert "Rule F3 differs between" in result.stdout
    assert "Rule F99 of docs/features/README.md is not in scripts/check-docs.py" in result.stdout


def test_a_stale_decisions_index_fails(repo: Path) -> None:
    write(
        repo,
        "docs/decisions/ci.md",
        "# ci\n\n## A\n\nStatus: decided\n\n## B\n\nStatus: postponed\n",
    )
    result = run(repo)
    assert "docs/decisions/README.md is stale" in result.stdout


def test_this_repository_passes_its_integrity_checks() -> None:
    assert check_docs.integrity(ROOT) == []


def test_the_modem_owns_its_feature_documents(repo: Path) -> None:
    write(repo, "esp32tl/sources/model/modem.py", '"""Modem."""\n')
    result = run(repo)
    assert "[F19]" in result.stdout
    assert "[F12]" not in result.stdout and "[F17]" not in result.stdout
    write(repo, "docs/features/modem-technical.md", "# Modem\n")
    assert run(repo).returncode == 0


def test_example_firmware_needs_its_documentation(repo: Path) -> None:
    write(repo, "examples/alarm/main/main.c", "void app_main(void) {}\n")
    assert "[F24]" in run(repo).stdout
    write(repo, "docs/features/examples-user.md", "# Examples\n\nAn alarm.\n")
    assert passes(run(repo))


def test_example_tests_only_need_no_documentation(repo: Path) -> None:
    write(repo, "examples/alarm/tests/test_alarm.py", "def test_alarm(): pass\n")
    assert passes(run(repo))
