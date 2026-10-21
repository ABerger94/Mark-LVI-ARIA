"""Runs LVI's own providers self-test (providers/_tests.py) under pytest.

The self-test deliberately lives in providers/ — deleting that folder takes
its tests with it — and it has its own check()/runner harness built around
one rule: call the engine the way main.py actually calls it, with main.py's
own argument shapes. This wrapper does NOT re-express those checks as pytest
asserts (that would rewrite the philosophy the file exists to defend); it
runs the module's own runner end-to-end and fails if any check inside it
failed. The runner returns its exit code and records failures by name, so
the pytest failure message names the actual broken check.

One piece of stage-setting the upstream harness assumes: it was written
against the author's configured install, and its "a default install pays
nothing" check reads config/api_keys.json directly (then strips the
providers key itself to measure the unconfigured path). That file is
gitignored user data and does not exist in a clean clone, so the wrapper
seeds the default-install state — an empty {} config — when it is absent,
and removes it again afterwards. If a real config exists it is left exactly
as found; the check itself restores the providers key it borrowed.
"""
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_providers_selftest():
    from providers import _tests

    cfg = ROOT / "config" / "api_keys.json"
    seeded = False
    if not cfg.exists():
        cfg.write_text("{}", encoding="utf-8")
        seeded = True
    try:
        rc = asyncio.run(_tests.main())
    finally:
        if seeded:
            cfg.unlink(missing_ok=True)
    assert rc == 0, (
        f"{_tests._fail} provider self-test check(s) failed: "
        + ", ".join(_tests._failures)
    )
