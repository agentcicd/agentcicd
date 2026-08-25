from __future__ import annotations

import subprocess
import sys

import pytest


pytestmark = pytest.mark.smoke


def test_import_boundary_checker_runs_in_warning_mode():
    result = subprocess.run(
        [sys.executable, "scripts/check_eval_sql_boundaries.py"],
        check=False,
        cwd=".",
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "agentcicd_eval_sql import" in result.stdout


def test_budget_report_runs_in_warning_mode():
    result = subprocess.run(
        [sys.executable, "scripts/report_eval_sql_budgets.py"],
        check=False,
        cwd=".",
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "agentcicd_eval_sql" in result.stdout
