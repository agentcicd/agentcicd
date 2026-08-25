from __future__ import annotations

from agentcicd.fixtures import Directory, Float, NamedStruct, Required, SecretId, Str, function


class Score(NamedStruct):
    value: Required[Float]
    rationale: Str


@function
def policy_score(answer: Str, policy: Str, judge_secret_id: SecretId = "secret.default") -> Score:
    return Score(value=1.0, rationale=f"{answer}:{policy}:{judge_secret_id}")


@function(name="artifact_echo")
def artifact_echo(entries: Directory) -> Directory:
    return entries
