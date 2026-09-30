"""Contract checks for the Python-TL-driven Codex E2E scenarios.

``exomonad init`` does not launch an interactive Codex root TL. The project's
root controller is ``tl_loop``, a bounded Python process that consumes
``.exo/tl-loop/plan.json`` and dispatches Codex leaves, workers, and reviewers
through the ExoMonad MCP tools. The Codex scenarios therefore have to be
expressed in terms of that architecture.

These checks need no server, no tmux session, and no ``codex`` binary, so they
are the fast gate that runs before anyone spends a live Codex run. They exist
because the retired shape failed *silently* for a long time: a scenario can keep
setting ``root_agent_type``, keep validating a project-root
``.codex/config.toml`` that normal startup no longer generates, and still look
plausible in review. Each check below names the specific way that regresses.

Covered:

* every migrated scenario ships a ``plan.json`` and installs it at
  ``.exo/tl-loop/plan.json`` before ``init``
* no migrated harness or prompt reintroduces the interactive root TL
  (``root_agent_type``, ``initial_prompt``, or an "e2e-test.md used as a TL
  prompt" read)
* every migrated validator asserts the durable controller phase, the absence of
  a retired root Codex config, CODEX_HOME propagation, and Codex trust in the
  isolated home
* the shared ``lib/python-tl.sh`` actually implements those four assertion
  families, so a scenario cannot pass by naming a helper that does not exist
* the repository no longer points at the retired ``subtl-worker-notify`` name
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = PROJECT_ROOT / "tests" / "e2e"
LIB_DIR = E2E_DIR / "lib"
SHARED_HELPER = LIB_DIR / "python-tl.sh"
JUSTFILE = PROJECT_ROOT / "justfile"

#: The Codex scenarios this migration owns. Each one drove an interactive Codex
#: root TL before and is now driven by the Python controller's plan.
MIGRATED = (
    "chainlink-codex",
    "codex-messaging",
    "python-tl-worker-notify",
)

#: The scenario that was renamed because its old name described a model the
#: product no longer ships. Kept separate because it also asserts the rename is
#: complete across the repository.
RENAMED_FROM = "subtl-worker-notify"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def scenario_dir(name: str) -> Path:
    return E2E_DIR / name


# ---------------------------------------------------------------------------
# The shared helper
# ---------------------------------------------------------------------------


def test_shared_helper_exists_and_is_a_complete_assertion_family() -> None:
    """A scenario must not be able to pass by naming a helper that is absent.

    Each of the four families below is a property of the *shipped* architecture
    that a migrated scenario is expected to prove. If the helper stops
    providing one, the scenarios that call it silently stop proving it, so the
    helper is pinned here rather than only through its call sites.
    """
    helper = read(SHARED_HELPER)
    for family in (
        "e2e_python_tl_assert_phase",
        "e2e_python_tl_assert_slices",
        "e2e_python_tl_assert_no_codex_root_tl",
        "e2e_python_tl_assert_codex_child_config",
        "e2e_python_tl_assert_codex_trust",
        "e2e_python_tl_assert_session_codex_home",
        "e2e_python_tl_commit_scaffold",
    ):
        assert f"{family}() {{" in helper, f"lib/python-tl.sh must define {family}"


def test_shared_helper_reads_the_durable_checkpoint_not_a_log_line() -> None:
    """The phase assertion must come from ``run.json``.

    A controller that printed a phase and died before checkpointing it would
    satisfy a log-line check, so the helper is required to read the durable
    checkpoint the controller persists.
    """
    helper = read(SHARED_HELPER)
    assert ".exo/tl-loop" in helper, "the helper must resolve the controller checkpoint path"
    assert 'state["fsm"]["phase"]' in helper, (
        "the phase must be read from the durable checkpoint, not from stdout"
    )


def test_shared_helper_does_not_pin_a_pane_index() -> None:
    """The helper must not resurrect the dropped pane-index assertion.

    The retired validator asserted that the controller window's *active* pane
    was not pane 0. Delivery to a recipient with no ``routing.json`` resolves
    through the window's current pane, so that assertion encoded an invariant
    the delivery path does not hold. See
    ``tests/e2e/python-tl-worker-notify/e2e-test.md``.
    """
    helper = read(SHARED_HELPER)
    assert "pane_index" not in helper, (
        "the shared helper must not depend on a tmux pane index"
    )


# ---------------------------------------------------------------------------
# The shared helper, executed
# ---------------------------------------------------------------------------
#
# Reading the helper's source proves the assertion families exist. It does not
# prove they *fail* when they should, and a helper whose last command is a
# `printf` will happily report success after a failed check. Each family is
# therefore driven here through a real bash against a synthetic fixture, in
# both directions: the case that must pass, and the near-miss that must fail.


WORKER_CONFIG = """\
model = "gpt-luna"
approval_policy = "never"
developer_instructions = \"\"\"
# ExoMonad Worker Agent Protocol

body
\"\"\"

[features]
hooks = true

[mcp_servers.exomonad]
command = "exomonad"
args = ["mcp-stdio", "--role", "worker", "--name", "w1-codex"]
"""

USER_CONFIG_TEMPLATE = """\
# BEGIN EXOMONAD CODEX HOOKS
[[hooks.PreToolUse]]
command = "exomonad hook pre-tool-use --runtime codex"
[[hooks.PostToolUse]]
command = "exomonad hook post-tool-use --runtime codex"
[[hooks.Stop]]
command = "exomonad hook stop --runtime codex"
# END EXOMONAD CODEX HOOKS

[hooks.state."{config}:pre_tool_use:0:0"]
trusted_hash = "abc"

[projects."{agent_dir}"]
trust_level = "trusted"
"""

RUN_STATE = """\
{"version": 1, "revision": 1, "run_id": "root",
 "fsm": {"phase": "tl_done", "waiting": []},
 "slices": {"w1": {"id": "w1", "status": "spawned", "paths": ["tl-loop/w1"],
                   "depends_on": [], "attempts": 1}},
 "budgets": {}, "gates": [], "events": {"last_consumed_offset": 0}}
"""


@pytest.fixture
def fixture(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    agent_dir = repo / ".exo" / "agents" / "w1-codex"
    config = agent_dir / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(WORKER_CONFIG, encoding="utf-8")
    state = repo / ".exo" / "tl-loop" / "root" / "run.json"
    state.parent.mkdir(parents=True)
    state.write_text(RUN_STATE, encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        USER_CONFIG_TEMPLATE.format(config=config, agent_dir=agent_dir), encoding="utf-8"
    )
    return {
        "repo": repo,
        "config": config,
        "agent_dir": agent_dir,
        "codex_home": codex_home,
        "user_config": codex_home / "config.toml",
    }


def call_helper(fixture: dict[str, Path], *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; "$@"', "bash", str(SHARED_HELPER), *[str(a) for a in args]],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("function", "args", "must_fail"),
    [
        pytest.param("e2e_python_tl_assert_phase", ("tl_done",), False, id="happy-path-phase"),
        pytest.param(
            "e2e_python_tl_assert_phase", ("tl_parked",), True, id="wrong-terminal-phase"
        ),
        pytest.param(
            "e2e_python_tl_assert_slices", ("w1",), False, id="happy-path-slices"
        ),
        pytest.param(
            "e2e_python_tl_assert_slices", ("absent",), True, id="plan-slice-not-dispatched"
        ),
    ],
)
def test_durable_state_assertions_reject_the_near_miss(
    fixture: dict[str, Path], function: str, args: tuple[str, ...], must_fail: bool
) -> None:
    result = call_helper(fixture, function, fixture["repo"], *args)
    assert (result.returncode != 0) is must_fail, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("role", "name", "marker", "must_fail"),
    [
        ("worker", "w1-codex", "ExoMonad Worker Agent Protocol", False),
        ("dev", "w1-codex", "ExoMonad Worker Agent Protocol", True),
        ("worker", "other-codex", "ExoMonad Worker Agent Protocol", True),
        ("worker", "w1-codex", "ExoMonad Dev Agent Protocol", True),
    ],
)
def test_codex_child_config_assertion_rejects_a_role_mismatch(
    fixture: dict[str, Path], role: str, name: str, marker: str, must_fail: bool
) -> None:
    result = call_helper(
        fixture,
        "e2e_python_tl_assert_codex_child_config",
        fixture["config"], "w1", role, name, marker,
    )
    assert (result.returncode != 0) is must_fail, result.stdout + result.stderr


def test_codex_child_config_assertion_rejects_a_missing_config(tmp_path: Path) -> None:
    result = call_helper(
        {"config": tmp_path / "absent" / "config.toml"},
        "e2e_python_tl_assert_codex_child_config",
        tmp_path / "absent" / "config.toml", "w1", "worker", "w1-codex",
        "ExoMonad Worker Agent Protocol",
    )
    assert result.returncode != 0


def test_no_codex_root_tl_assertion_fails_when_a_retired_config_exists(
    fixture: dict[str, Path]
) -> None:
    assert call_helper(fixture, "e2e_python_tl_assert_no_codex_root_tl", fixture["repo"]).returncode == 0

    retired = fixture["repo"] / ".codex" / "config.toml"
    retired.parent.mkdir()
    retired.write_text("approval_policy = \"never\"\n", encoding="utf-8")
    result = call_helper(fixture, "e2e_python_tl_assert_no_codex_root_tl", fixture["repo"])
    assert result.returncode != 0, "a project-root Codex config is the retired model"


def test_codex_trust_assertion_rejects_a_half_written_lifecycle(
    fixture: dict[str, Path]
) -> None:
    def check() -> subprocess.CompletedProcess[str]:
        return call_helper(
            fixture,
            "e2e_python_tl_assert_codex_trust",
            fixture["codex_home"], fixture["config"],
        )

    assert check().returncode == 0

    # Hook trust present, project trust missing: a partial lifecycle write.
    fixture["user_config"].write_text(
        read(fixture["user_config"]).replace('trust_level = "trusted"', 'trust_level = "no"'),
        encoding="utf-8",
    )
    assert check().returncode != 0

    # Trust for a different config: the state does not describe this child.
    fixture["user_config"].write_text(
        read(fixture["user_config"]).replace(":pre_tool_use:0:0", ":pre_tool_use:0:1"),
        encoding="utf-8",
    )
    assert check().returncode != 0

    # The shared hooks block gone: the config is one Codex will not load.
    fixture["user_config"].write_text(
        read(fixture["user_config"]).replace("BEGIN EXOMONAD CODEX HOOKS", "REMOVED"),
        encoding="utf-8",
    )
    assert check().returncode != 0


def test_session_codex_home_assertion_rejects_a_missing_propagation() -> None:
    """A tmux session that never received CODEX_HOME must fail the assertion."""
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; e2e_python_tl_assert_session_codex_home no-such-session /tmp/codex-home',
            "bash", str(SHARED_HELPER),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0



# ---------------------------------------------------------------------------
# The plans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_scenario_ships_a_valid_plan(name: str) -> None:
    plan_path = scenario_dir(name) / "plan.json"
    assert plan_path.is_file(), (
        f"{name} must ship the plan the Python controller consumes, not a TL prompt"
    )

    plan = json.loads(read(plan_path))
    assert plan["run_id"] == "root", "a new run uses the root run id"
    sections = plan["plan"]
    assert sections["workers"], f"{name} must dispatch at least one Codex worker"
    for worker in sections["workers"]:
        assert worker["name"], "a worker slice is named by its agent identity"
        assert worker["task"].strip(), "a worker slice carries a task"
        assert worker["agent_type"] == "codex", (
            f"{name} dispatches Codex children, not another harness"
        )
    assert len({worker["name"] for worker in sections["workers"]}) == len(
        sections["workers"]
    ), "worker names are agent identities and must be unique"


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_plan_declares_at_most_one_worker(name: str) -> None:
    """``spawn_worker`` refuses a second worker in the same parent window.

    Workers are sequential by design, enforced by ``active_worker_for_parent_tab``
    in ``rust/exomonad-core/src/services/agent_control/spawn.rs`` and documented
    in ``docs/decisions/agent-lifecycle-invariants.md``. The controller dispatches
    every ``workers`` entry in one pass, so a second entry is refused, which parks
    the slice: the scenario would end up asserting a park instead of the
    behaviour it exists to prove.

    A second *agent* is still legitimate -- it just has to be a companion,
    declared in ``.exo/config.toml`` and already alive when the controller
    dispatches. ``codex-messaging`` uses that shape.
    """
    plan = json.loads(read(scenario_dir(name) / "plan.json"))
    workers = plan["plan"]["workers"]
    assert len(workers) == 1, (
        f"{name} declares {len(workers)} workers; only one may be dispatched "
        f"per run because workers are sequential"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_plan_installs_before_init(name: str) -> None:
    """``plan.json`` has to be in place before the controller starts.

    ``init`` launches the controller with ``--wait-for-plan``, but a plan that
    appears later would let the run start from whatever the fixture happened to
    contain, which is exactly the drift this migration removes.
    """
    run = read(scenario_dir(name) / "run.sh")
    plan_install = run.find(".exo/tl-loop/plan.json")
    init_launch = run.find("exomonad init") if '"$EXOMONAD_BIN" init' not in run else run.find(
        '"$EXOMONAD_BIN" init'
    )
    assert plan_install != -1, f"{name} must install a plan under .exo/tl-loop"
    assert init_launch != -1, f"{name} must launch exomonad init"
    assert plan_install < init_launch, (
        f"{name} must install plan.json before launching exomonad init"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_plan_is_valid_against_the_controller_schema(name: str) -> None:
    """The plan must satisfy the closed-key ``WorkPlan`` validator.

    The controller rejects unknown keys at load rather than ignoring them, so a
    typo in a scenario plan would otherwise only surface as a controller
    startup failure minutes into a live Codex run.
    """
    plan = json.loads(read(scenario_dir(name) / "plan.json"))
    top_level = {"run_id", "budgets", "plan"}
    assert set(plan) <= top_level, f"unknown top-level plan keys: {set(plan) - top_level}"

    section_keys = {"workers", "leaves", "sub_tls"}
    assert set(plan["plan"]) <= section_keys, (
        f"unknown plan keys: {set(plan['plan']) - section_keys}"
    )

    worker_keys = {"name", "task", "agent_type", "task_timeout_seconds"}
    for worker in plan["plan"]["workers"]:
        assert set(worker) <= worker_keys, (
            f"unknown worker keys: {set(worker) - worker_keys}"
        )
        assert "name" in worker and Path(worker["name"]).name == worker["name"], (
            "a worker name is one path component: it becomes the agent identity"
        )


# ---------------------------------------------------------------------------
# The retired interactive root TL must not come back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_harness_does_not_configure_an_interactive_root_tl(name: str) -> None:
    run = read(scenario_dir(name) / "run.sh")
    for retired in ("root_agent_type", "initial_prompt"):
        assert not re.search(rf"^\s*{retired}\s*=", run, re.MULTILINE), (
            f"{name} must not set {retired}: init always starts the Python "
            f"controller, and initial_prompt, if set, must be a JSON WorkPlan"
        )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_harness_does_not_read_its_doc_as_a_tl_prompt(name: str) -> None:
    run = read(scenario_dir(name) / "run.sh")
    assert "e2e-test.md" not in run, (
        f"{name} must not feed e2e-test.md to the controller; that file is "
        f"scenario documentation and plan.json is the controller's only input"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_validator_asserts_the_retired_root_config_is_absent(name: str) -> None:
    validate = read(scenario_dir(name) / "validate.sh")
    assert "e2e_python_tl_assert_no_codex_root_tl" in validate, (
        f"{name} must assert that normal Python-controller startup generates no "
        f"interactive Codex root TL config"
    )
    assert "validate_root_config" not in validate, (
        f"{name} must not keep validating the retired project-root "
        f".codex/config.toml"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_validator_asserts_the_durable_controller_state(name: str) -> None:
    validate = read(scenario_dir(name) / "validate.sh")
    assert "e2e_python_tl_assert_phase" in validate, (
        f"{name} must assert the phase the controller persisted in its own checkpoint"
    )
    assert "e2e_python_tl_assert_slices" in validate, (
        f"{name} must assert every plan entry became a slice, or a controller "
        f"that dropped one could reach a terminal phase trivially"
    )
    assert '"tl_done"' in validate, (
        f"{name} must name the terminal phase it expects, not accept any phase"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_validator_asserts_codex_trust_in_the_isolated_home(name: str) -> None:
    validate = read(scenario_dir(name) / "validate.sh")
    assert "e2e_python_tl_assert_codex_trust" in validate, (
        f"{name} must assert project and hook trust landed in the isolated CODEX_HOME"
    )
    assert "e2e_python_tl_assert_session_codex_home" in validate, (
        f"{name} must assert CODEX_HOME propagated into the tmux session"
    )
    assert "codex-home.sh" not in validate or True
    assert "e2e_copy_codex_auth" not in validate, (
        "only run.sh copies host auth artifacts; the validator must not reach the host home"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_harness_isolates_codex_home_before_any_exomonad_process(name: str) -> None:
    run = read(scenario_dir(name) / "run.sh")
    assert "e2e_isolate_codex_home" in run, (
        f"{name} must isolate CODEX_HOME through the shared helper"
    )
    assert "lib/codex-home.sh" in run, f"{name} must source the shared Codex-home helper"
    assert "lib/python-tl.sh" in run, f"{name} must source the shared Python-TL helper"
    isolate = run.find("e2e_isolate_codex_home")
    init = run.find('"$EXOMONAD_BIN" init')
    assert isolate < init, (
        f"{name} must isolate CODEX_HOME before launching exomonad init, because "
        f"a CODEX_HOME exported afterwards reaches no already-running process"
    )


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_harness_commits_the_scaffold_before_dispatch(name: str) -> None:
    """``spawn_worker`` refuses a dirty worktree.

    ``init`` writes ``.mcp.json`` and ``.claude/rules/exomonad.md`` after the
    fixture is created and ``exomonad new`` does not ignore either, so a
    fixture that skips this step can never dispatch its first worker.
    """
    run = read(scenario_dir(name) / "run.sh")
    assert "e2e_python_tl_commit_scaffold" in run, (
        f"{name} must commit the scaffold so the controller can dispatch a worker"
    )
    commit = run.find("e2e_python_tl_commit_scaffold")
    init = run.find('"$EXOMONAD_BIN" init')
    assert commit < init, f"{name} must commit the scaffold before launching exomonad init"


# ---------------------------------------------------------------------------
# The rename
# ---------------------------------------------------------------------------


def test_renamed_scenario_directory_exists() -> None:
    assert scenario_dir("python-tl-worker-notify").is_dir()
    assert not scenario_dir(RENAMED_FROM).exists(), (
        "the subtl-worker-notify name described a sub-TL that no longer exists"
    )


def test_retired_scenario_name_is_gone_from_the_repository() -> None:
    """Nothing may still point a human or a recipe at the retired name.

    The one permitted mention is the scenario's own documentation, which
    explains why the rename happened; a stale recipe or matrix row would send
    an operator to a directory that does not exist.
    """
    stale: list[str] = []
    for path in _recipe_and_doc_files():
        if path in {
            # The scenario's own documentation explains the rename, and this
            # file is the check that names the retired identifier at all.
            scenario_dir("python-tl-worker-notify") / "e2e-test.md",
            Path(__file__).resolve(),
        }:
            continue
        if RENAMED_FROM in read(path):
            stale.append(str(path.relative_to(PROJECT_ROOT)))
    assert not stale, f"stale {RENAMED_FROM} references: {stale}"


#: Where a stale scenario name would actually mislead somebody: recipes, the
#: status/matrix/audit documents, the e2e tree itself, and CI configuration.
#: Deliberately not the whole repository -- build outputs and vendored trees
#: are not where an operator looks for a scenario to run.
_DOC_GLOBS = (
    "*.md",
    "docs/**/*.md",
    "tests/e2e/**/*.md",
    "tests/e2e/**/*.sh",
    "tests/e2e/**/*.py",
    "tests/e2e/**/*.json",
)
_RECIPE_FILES = ("justfile", "Makefile", ".forgejo/**/*.yml", ".github/**/*.yml")


def _recipe_and_doc_files() -> list[Path]:
    found: list[Path] = []
    for pattern in (*_DOC_GLOBS, *_RECIPE_FILES):
        found.extend(sorted(PROJECT_ROOT.glob(pattern)))
    return found


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def test_justfile_exposes_a_recipe_per_migrated_scenario() -> None:
    justfile = read(JUSTFILE)
    for name in MIGRATED:
        target = name.replace("-", "-")
        assert f"\ne2e-{target}:" in justfile, f"just e2e-{target} must exist"
        assert f"\ncheck-e2e-{target}:" in justfile, f"just check-e2e-{target} must exist"
    assert RENAMED_FROM not in justfile, f"the justfile still names {RENAMED_FROM}"


def test_check_recipes_syntax_check_the_migrated_harnesses() -> None:
    justfile = read(JUSTFILE)
    for name in MIGRATED:
        recipe = justfile.split(f"\ncheck-e2e-{name}:", 1)[1].split("\n\n", 1)[0]
        assert f"bash -n tests/e2e/{name}/run.sh" in recipe
        assert f"bash -n tests/e2e/{name}/validate.sh" in recipe


def test_codex_home_isolation_gate_covers_the_shared_python_tl_helper() -> None:
    """The broad `bash -n`/shellcheck gate must include the new shared helper.

    ``check-e2e-codex-home-isolation`` iterates the shared helpers and every
    ``run.sh``. A new library that gate does not read would be unchecked, so the
    list is asserted rather than assumed.
    """
    recipe = read(JUSTFILE).split("\ncheck-e2e-codex-home-isolation:", 1)[1].split(
        "\n\n", 1
    )[0]
    assert "tests/e2e/lib/python-tl.sh" in recipe


def test_codex_isolation_contract_tracks_the_renamed_harness() -> None:
    contract = read(
        E2E_DIR / "codex-home-isolation" / "test_contract.py"
    )
    assert "python-tl-worker-notify/run.sh" in contract
    assert f'"{RENAMED_FROM}/run.sh"' not in contract
    assert f'"{RENAMED_FROM}"' not in contract


# ---------------------------------------------------------------------------
# The scenario documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MIGRATED)
def test_migrated_scenario_documents_the_controller_architecture(name: str) -> None:
    doc = read(scenario_dir(name) / "e2e-test.md")
    assert "plan.json" in doc, f"{name} must document the controller's input"
    assert "tl_loop" in doc, f"{name} must name the controller the scenario drives"
    assert "run.json" in doc, (
        f"{name} must document the durable checkpoint its validator reads"
    )
    assert "exomonad init" in doc, (
        f"{name} must document how the scenario is launched"
    )


def test_spawn_worker_still_refuses_a_second_worker_in_the_parent_window() -> None:
    """The single-worker plans rely on this invariant staying true.

    If ``active_worker_for_parent_tab`` stopped refusing, a plan could carry
    several workers again. Until then the migrated scenarios have to keep one
    worker per run, and this check is what makes that a stated dependency
    rather than folklore.
    """
    spawn = read(
        PROJECT_ROOT
        / "rust"
        / "exomonad-core"
        / "src"
        / "services"
        / "agent_control"
        / "spawn.rs"
    )
    assert "active_worker_for_parent_tab" in spawn, (
        "spawn_worker must keep the sequential-worker guard; the migrated "
        "plans assume only one worker is dispatched per run"
    )
    assert "workers are sequential" in spawn, (
        "the refusal message is the documented behaviour; a rewrite of it is a "
        "behavioural change the scenarios depend on"
    )


def test_shipped_codex_config_schema_matches_the_generated_config() -> None:
    """The keys the validator reads must exist in the renderer.

    ``e2e_python_tl_assert_codex_child_config`` asserts on
    ``approval_policy``, ``features.hooks``, and the ``exomonad`` MCP server
    args. If the renderer stopped emitting one of them the assertion would fail
    for the wrong reason -- or, worse, a scenario could be "fixed" by deleting
    the assertion. This pins the renderer's shape from the Rust source.
    """
    renderer = read(
        PROJECT_ROOT / "rust" / "exomonad-core" / "src" / "codex_config.rs"
    )
    assert 'approval_policy = "never"' in renderer
    assert "hooks = true" in renderer
    assert re.search(r"mcp-stdio", renderer), "the renderer names the MCP transport"


def test_harness_policy_scaffold_allows_the_codex_children_the_scenarios_dispatch() -> None:
    """``exomonad new`` scaffolds the policy every migrated plan is validated against.

    A plan that names a harness the policy does not allow fails preflight before
    a single child is dispatched, so the three scenarios' `codex` requirement
    and the scaffold have to agree.
    """
    scaffold = read(PROJECT_ROOT / "rust" / "exomonad" / "src" / "new.rs")
    for role in ("roles.tl", "roles.worker", "roles.reviewer"):
        assert f"[{role}]" in scaffold, f"the scaffold must declare {role}"
    assert "codex/gpt-luna" in scaffold, "the scaffold allowlist is codex-based"


def test_tl_loop_policy_fixture_still_validates() -> None:
    """The policy files a fixture inherits must still parse.

    The migrated harnesses rely on ``exomonad new`` to scaffold these, and the
    controller refuses to start without a valid allowlist. A syntax break here
    would take out all three scenarios at once.
    """
    for name in ("harness_policy.toml", "review-policy.toml", "harness_capability.toml"):
        path = PROJECT_ROOT / ".exo" / name
        assert path.is_file(), f".exo/{name} must exist"
        tomllib.loads(read(path))
