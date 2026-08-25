from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


EXPECTED_FILENAMES = {
    "debug_sidecars.yaml",
    "fixture_calls.yaml",
    "manifests.yaml",
    "publishes.yaml",
    "reruns.yaml",
    "schemas.yaml",
    "tables.yaml",
    "validation.yaml",
}


def validate_expected_artifact_file(path: Path) -> list[str]:
    errors: list[str] = []
    if path.name not in EXPECTED_FILENAMES:
        errors.append(f"unexpected expected-artifact filename: {path.name}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{path}: invalid YAML: {exc}"]
    if payload is None:
        errors.append(f"{path}: expected artifact is empty")
        return errors
    if not isinstance(payload, (dict, list)):
        errors.append(f"{path}: expected artifact must be a mapping or list")
        return errors
    if isinstance(payload, dict) and not payload:
        errors.append(f"{path}: expected artifact mapping is empty")
    if isinstance(payload, list) and any(item is None for item in payload):
        errors.append(f"{path}: expected artifact list contains null entries")
    _validate_shape(path, payload, errors)
    return errors


def _validate_shape(path: Path, payload: Any, errors: list[str]) -> None:
    if path.name == "tables.yaml" and not isinstance(payload, dict):
        errors.append(f"{path}: tables expected artifact must be a mapping")
    if path.name == "schemas.yaml" and not isinstance(payload, dict):
        errors.append(f"{path}: schemas expected artifact must be a mapping")
    if path.name == "manifests.yaml" and not isinstance(payload, dict):
        errors.append(f"{path}: manifests expected artifact must be a mapping")
    if path.name in {"fixture_calls.yaml", "publishes.yaml", "debug_sidecars.yaml", "reruns.yaml"} and not isinstance(payload, (dict, list)):
        errors.append(f"{path}: expected artifact must be a mapping or list")


def validate_expected_artifacts(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(root.glob("e2e-*/expected/*.yaml")):
        errors.extend(validate_expected_artifact_file(path))
    return errors
