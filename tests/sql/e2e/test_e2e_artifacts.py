from __future__ import annotations

import pytest

from .harness import artifact_pytest_params, run_e2e_artifact


@pytest.mark.parametrize("artifact", artifact_pytest_params())
def test_eval_sql_e2e_artifact(artifact, local_spark, tmp_path):
    run_e2e_artifact(artifact, spark=local_spark, tmp_path=tmp_path)
