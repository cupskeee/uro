"""Run the whole S1-S8 stress battery. Each script is a subprocess (isolated event loop,
isolated fresh world); each writes its evidence to out/stress/<name>.txt and exits non-zero on
a broken expectation, so this runner doubles as the battery's self-check.

    uv run python examples/games/seventh-vault/stress/run_all.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = [
    "s1_arbiter.py",
    "s2_vantage.py",
    "s3_race.py",
    "s4_management.py",
    "s5_lifecycle.py",
    "s6_ruleset.py",
    "s7_counters.py",
    "s8_time.py",
]


def main() -> None:
    here = Path(__file__).resolve().parent
    failures: list[str] = []
    for script in SCRIPTS:
        print(f"\n===== {script} =====")
        result = subprocess.run([sys.executable, str(here / script)], check=False)
        if result.returncode != 0:
            failures.append(script)
    if failures:
        print(f"\nFAILED: {failures}")
        raise SystemExit(1)
    print(f"\nall {len(SCRIPTS)} stress probes ran; evidence under out/stress/")


if __name__ == "__main__":
    main()
