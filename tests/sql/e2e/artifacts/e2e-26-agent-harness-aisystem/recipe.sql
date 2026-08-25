DECLARE INPUT coding_agent AISYSTEM
WITH interface = 'llm.chat'
DEFAULT 'aisystem.codex';

DECLARE INPUT codex_secret SECRET DEFAULT 'secret.codex';

CREATE BATCH TABLE prepared
SELECT
  'case-1' AS case_id,
  'Reply exactly: harness ok' AS issue_text,
  '{workspace_root}' AS workspace_root;

CREATE BATCH TABLE raw_harness_runs
SELECT
  case_id,
  envs.agent_harness.run_task(
    env = envs.agent_harness.spec(
      session_id = 'agent',
      aisystem = coding_agent,
      workdir = workspace_root,
      secret_id = codex_secret
    ),
    task = issue_text,
    timeout_seconds = 120
  ) AS result
FROM prepared;

CREATE BATCH TABLE harness_runs
SELECT
  case_id,
  result,
  CAST(try_variant_get(result, '$.status') AS STRING) AS status,
  CAST(try_variant_get(result, '$.final_output') AS STRING) AS final_output,
  CAST(try_variant_get(result, '$.metadata.harness') AS STRING) AS harness,
  CAST(try_variant_get(result, '$.metadata.returncode') AS INTEGER) AS returncode
FROM raw_harness_runs;
