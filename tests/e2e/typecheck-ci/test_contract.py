"""Contract checks for the typecheck CI acceptance (#1332).

These run without Docker, a forge, or a runner. They prove the pieces the run
depends on before the slow acceptance is attempted: the real workflow job and
recipe are what gets extracted, Phase 0 names a missing image instead of
pulling it, the docker helper refuses registry commands, the seeded image recipe
still contains the offline wheelhouse and checkout mirror, and no other
project's content has leaked into the harness.

Run them with the same interpreter the e2e recipes use:

    tl_loop/.venv/bin/python -m pytest -q tests/e2e/typecheck-ci/test_contract.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import tomllib

HARNESS = Path(__file__).resolve().parent
PROJECT_ROOT = HARNESS.parents[2]
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "e2e" / "lib"))

import agnostic
import dockerops
import harness
import images
import scratch


def _dev_specifiers() -> list[str]:
    payload = tomllib.loads(
        (PROJECT_ROOT / "tl_loop" / "pyproject.toml").read_text(encoding="utf-8")
    )
    return list(payload["project"]["optional-dependencies"]["dev"])


def test_the_real_typecheck_job_is_extracted_verbatim() -> None:
    ci_text = (PROJECT_ROOT / scratch.CI_WORKFLOW).read_text(encoding="utf-8")
    job = scratch.typecheck_job(ci_text)

    assert "actions/checkout@v4" in job
    assert "python3 -m venv .venv-typecheck" in job
    assert "just tl-loop-typecheck" in job
    assert "EXOMONAD_PY: .venv-typecheck/bin/python" in job
    assert "pip install './tl_loop[dev]'" not in job, (
        "the job must not build the empty wheel"
    )

    for specifier in _dev_specifiers():
        assert specifier in job, f"the job does not install the dev pin {specifier!r}"

    workflow = scratch.render_workflow(ci_text)
    assert job in workflow
    assert workflow.startswith("name: typecheck-ci-e2e\n")
    assert "\non: push\n" in workflow
    assert workflow.count("typecheck:") == 1


def test_the_real_recipe_is_extracted_verbatim() -> None:
    justfile_text = (PROJECT_ROOT / "justfile").read_text(encoding="utf-8")
    rendered = scratch.render_justfile(justfile_text)

    assert "py := env_var_or_default(\"EXOMONAD_PY\"" in rendered
    assert "tl-loop-typecheck:" in rendered
    assert "{{py}} scripts/check_typecheck_ratchet.py --project-root ." in rendered


def test_the_scratch_repository_copies_the_project(tmp_path: Path) -> None:
    destination = scratch.write_scratch(PROJECT_ROOT, tmp_path / "scratch")

    assert (destination / "tl_loop" / "__init__.py").is_file()
    assert (destination / "tl_loop" / "pyproject.toml").is_file()
    assert (destination / "tl_loop" / "typecheck-baseline.json").is_file()
    for script in scratch.SCRIPTS_NEEDED:
        assert (destination / "scripts" / script).is_file()
    assert (destination / "justfile").is_file()
    assert (destination / scratch.CI_WORKFLOW).is_file()
    assert not (destination / scratch.PROBE_RELATIVE_PATH).exists()
    assert not list(destination.rglob("__pycache__"))


def test_the_injected_error_is_a_new_strict_error(tmp_path: Path) -> None:
    destination = scratch.write_scratch(PROJECT_ROOT, tmp_path / "scratch")
    path = scratch.inject_strict_error(destination)

    assert path == destination / scratch.PROBE_RELATIVE_PATH
    source = path.read_text(encoding="utf-8")
    assert "def ratchet_probe(value: int) -> str:" in source
    assert "return value" in source


def test_phase0_names_the_missing_image_and_seed_command() -> None:
    with pytest.raises(images.MissingImage) as raised:
        images.require_cached_images(
            ("absent:1", images.JOB_IMAGE), exists=lambda image: image == images.JOB_IMAGE
        )
    message = str(raised.value)
    missing_list = message.split("This harness never", 1)[0]
    assert "absent:1" in missing_list
    assert images.JOB_IMAGE not in missing_list
    assert f"docker build -t {images.JOB_IMAGE}" in message
    assert images.DOCKERFILE in message

    images.require_cached_images(("present:1",), exists=lambda _image: True)


def test_the_docker_helper_refuses_registry_commands() -> None:
    for arguments in (
        ("pull", "an-image"),
        ("build", "-t", "an-image", "."),
        ("image", "pull", "an-image"),
        ("compose", "pull"),
        ("compose", "build"),
    ):
        with pytest.raises(dockerops.DockerError):
            dockerops.docker(*arguments)


def test_the_seeded_image_recipe_matches_the_offline_contract() -> None:
    dockerfile = (PROJECT_ROOT / images.DOCKERFILE).read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM node:lts\n")
    assert "COPY just /usr/local/bin/just" in dockerfile
    assert "python3-venv" in dockerfile
    assert "/opt/wheelhouse" in dockerfile
    assert "/opt/mirror/actions-checkout.git" in dockerfile
    assert "no-index = true" in dockerfile


def test_the_harness_is_project_agnostic(tmp_path: Path) -> None:
    assert agnostic.find_project_specific(
        [HARNESS, PROJECT_ROOT / images.DOCKERFILE]
    ) == []

    leaked = tmp_path / "leaked.txt"
    leaked.write_text(f"image: {agnostic.FORBIDDEN_TERMS[0]} example\n", encoding="utf-8")
    findings = agnostic.find_project_specific([tmp_path])
    assert findings and "leaked.txt" in findings[0]


def test_the_check_classifiers_accept_the_expected_logs() -> None:
    ok, _ = harness.green_log_is_acceptable(
        "typecheck ratchet passed: 0 errors in 1 source files\nJob succeeded"
    )
    assert ok
    assert not harness.green_log_is_acceptable("Job failed")[0]

    ok, _ = harness.red_log_is_acceptable(
        "tl_loop/ratchet_probe.py:5: error: Incompatible return value type "
        '[return-value]\ntypecheck ratchet failed: 1 baseline violation(s)\nJob failed'
    )
    assert ok
    assert not harness.red_log_is_acceptable(
        "typecheck ratchet passed\nJob succeeded"
    )[0]

    ok, _ = harness.missing_image_log_is_acceptable(
        f"docker pull image={images.JOB_IMAGE}\n"
        "Error response from daemon: dial tcp\nJob failed",
        images.JOB_IMAGE,
    )
    assert ok
    assert not harness.missing_image_log_is_acceptable(
        "Job failed without a name", images.JOB_IMAGE
    )[0]
