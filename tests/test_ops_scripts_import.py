"""Every ops/ script must actually start when invoked the way it documents.

This exists because `python ops/canary.py` failed on its first real run with
`ModuleNotFoundError: No module named 'src'`, while the whole suite was
green. Running a script puts its own directory on sys.path, not the repo
root; pytest never sees that, because conftest.py inserts the root before
any test imports anything. So the tests exercised code that could not be
launched.

Each script is executed in a subprocess with `run_name` set to something
other than `__main__`, so the module body -- imports included -- runs while
main() does not. Nothing here touches the network or the database.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path):
    """Import an ops script as a module, without running main()."""
    spec = importlib.util.spec_from_file_location(f"ops_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _imports_project_code(path: Path) -> bool:
    """Only scripts that import `src` can fail this way, and they are the
    only ones for which the repo root has to be on sys.path.

    It also excludes ops/spikes/, which is the right outcome for a second
    reason: those are finished one-offs that read sys.argv[1] at module
    scope, so importing them at all is meaningless.
    """
    text = path.read_text()
    return "from src" in text or "import src" in text


SCRIPTS = sorted(
    str(p.relative_to(ROOT))
    for p in ROOT.glob("ops/**/*.py")
    if _imports_project_code(p)
)


def test_there_are_scripts_to_check():
    """A filter that silently matches nothing would make every case below
    vacuous -- which is the exact shape of the bug this file exists for."""
    assert len(SCRIPTS) >= 4, SCRIPTS
    assert "ops/canary.py" in SCRIPTS


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_script_can_be_run_from_the_repo_root(script):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import runpy; runpy.run_path({script!r}, run_name='__imported__')",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"{script} cannot be launched as a script:\n{result.stderr}"
    )


def test_every_script_that_logs_in_uses_the_same_session_file():
    """A second, wrong path is invisible: launch_authenticated_page falls
    back to a cold login, which succeeds. The only symptom is that the one
    step in this system a human might have to babysit -- Compass's login
    form, behind reCAPTCHA -- gets driven every single time, while a
    perfectly good session sits next to it under a different name.

    Shipped exactly that in ops/reject.py, with `data/auth_state.json`
    against scrape.py's `data/.auth/compass_state.json`.
    """
    import scrape

    expected = scrape.AUTH_STATE_PATH
    checked = []
    for path in sorted(ROOT.glob("ops/*.py")):
        text = path.read_text()
        if "launch_authenticated_page" not in text:
            continue
        module = _load(path)
        assert module.AUTH_STATE_PATH == expected, (
            f"{path.name} logs in with {module.AUTH_STATE_PATH}, "
            f"but scrape.py uses {expected}"
        )
        checked.append(path.name)

    # A rename that made the glob match nothing would pass silently.
    assert checked, "no ops script was checked; has the login helper moved?"
