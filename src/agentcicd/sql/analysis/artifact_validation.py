from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .runtime_dependencies import RuntimeSqlDependencies, extract_runtime_dependencies_from_sql


@dataclass(frozen=True)
class RecipeArtifactReferences:
    fixture_ids: set[str] = field(default_factory=set)
    aisystem_ids: set[str] = field(default_factory=set)
    secret_ids: set[str] = field(default_factory=set)
    dataset_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ArtifactValidationIssue:
    code: str
    message: str
    reference_type: str | None = None
    reference_id: str | None = None


@dataclass(frozen=True)
class RecipeArtifactValidation:
    references: RecipeArtifactReferences
    errors: list[ArtifactValidationIssue] = field(default_factory=list)
    warnings: list[ArtifactValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def collect_recipe_artifact_references(
    source_text: str,
    *,
    default_macros: dict[str, Any] | None = None,
    fixture_ids: list[str] | None = None,
) -> RecipeArtifactReferences:
    macros = _string_macros(default_macros or {})
    expanded_source = _expand_macros(source_text, macros=macros)
    dependencies = extract_runtime_dependencies_from_sql(source_text, macros=macros)
    return RecipeArtifactReferences(
        fixture_ids=set(_clean_ids(fixture_ids or [])) | set(dependencies.fixture_ids) | _extract_entity_ids(expanded_source, "fixture"),
        aisystem_ids=set(dependencies.aisystem_ids) | _extract_entity_ids(expanded_source, "aisystem"),
        secret_ids=set(dependencies.secret_ids) | _extract_entity_ids(expanded_source, "secret"),
        dataset_ids=_extract_dataset_ids(expanded_source),
    )


def validate_recipe_artifact_references(
    source_text: str,
    *,
    default_macros: dict[str, Any] | None = None,
    fixture_ids: list[str] | None = None,
    available_fixture_ids: set[str] | None = None,
    available_aisystem_ids: set[str] | None = None,
    available_secret_ids: set[str] | None = None,
    available_dataset_ids: set[str] | None = None,
) -> RecipeArtifactValidation:
    references = collect_recipe_artifact_references(
        source_text,
        default_macros=default_macros,
        fixture_ids=fixture_ids,
    )
    errors: list[ArtifactValidationIssue] = []
    _append_missing(
        errors,
        reference_type="fixture",
        code="missing_fixture",
        ids=references.fixture_ids,
        available=available_fixture_ids,
    )
    _append_missing(
        errors,
        reference_type="aisystem",
        code="missing_aisystem",
        ids=references.aisystem_ids,
        available=available_aisystem_ids,
    )
    _append_missing(
        errors,
        reference_type="secret",
        code="missing_secret",
        ids=references.secret_ids,
        available=available_secret_ids,
    )
    _append_missing(
        errors,
        reference_type="dataset",
        code="missing_dataset",
        ids=references.dataset_ids,
        available=available_dataset_ids,
    )
    return RecipeArtifactValidation(references=references, errors=errors)


def _append_missing(
    errors: list[ArtifactValidationIssue],
    *,
    reference_type: str,
    code: str,
    ids: set[str],
    available: set[str] | None,
) -> None:
    if available is None:
        return
    for reference_id in sorted(ids):
        if reference_id in available:
            continue
        errors.append(
            ArtifactValidationIssue(
                code=code,
                message=f"Referenced {reference_type} '{reference_id}' was not found",
                reference_type=reference_type,
                reference_id=reference_id,
            )
        )


def _string_macros(macros: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in macros.items()}


def _clean_ids(values: list[str]) -> list[str]:
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def _extract_dataset_ids(source_text: str) -> set[str]:
    return set(re.findall(r"(?:agentcicd://)?(dataset\.[A-Za-z0-9_\\-]+)", source_text))


def _expand_macros(source_text: str, *, macros: dict[str, str]) -> str:
    expanded = source_text
    for key, value in macros.items():
        expanded = expanded.replace(f"${key}", value)
    return expanded


def _extract_entity_ids(source_text: str, prefix: str) -> set[str]:
    return set(re.findall(rf"({re.escape(prefix)}\.[A-Za-z0-9_\\-]+)", source_text))
