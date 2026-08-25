from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentcicd.config import BackendName, ProjectConfig, load_project_config
from agentcicd.errors import ProjectLoadError
from agentcicd.inputs import CoercedInputs, load_inputs
from agentcicd.secrets import LocalSecretRecord, load_local_secrets


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    recipe_sql: Path
    run_root: Path


@dataclass(frozen=True)
class LocalRunSpec:
    paths: ProjectPaths
    recipe_sql: str
    config: ProjectConfig
    inputs: CoercedInputs
    secrets: tuple[LocalSecretRecord, ...]
    fixture_sources: tuple[Path, ...]

    @property
    def backend(self) -> BackendName:
        return self.config.run.backend


def load_project(project_dir: str | Path) -> LocalRunSpec:
    root = Path(project_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ProjectLoadError(f"Project directory does not exist: {root}")
    recipe_path = root / "recipe.sql"
    if not recipe_path.exists() or not recipe_path.is_file():
        raise ProjectLoadError(f"Project requires recipe.sql: {recipe_path}")
    recipe_sql = recipe_path.read_text(encoding="utf-8")
    config = load_project_config(root)
    inputs = load_inputs(root, recipe_sql)
    secrets = load_local_secrets(root)
    run_root = (root / config.run.working_dir).resolve()
    return LocalRunSpec(
        paths=ProjectPaths(root=root, recipe_sql=recipe_path, run_root=run_root),
        recipe_sql=recipe_sql,
        config=config,
        inputs=inputs,
        secrets=secrets,
        fixture_sources=_discover_fixture_sources(root, config),
    )


def _discover_fixture_sources(root: Path, config: ProjectConfig) -> tuple[Path, ...]:
    discovered: set[Path] = set()
    for path in sorted(root.glob("fixture*.py")):
        if path.is_file():
            discovered.add(path.resolve())
    fixtures_dir = root / "fixtures"
    if fixtures_dir.exists():
        for path in sorted(fixtures_dir.rglob("*.py")):
            if path.is_file():
                discovered.add(path.resolve())
    for group in config.fixture_groups:
        for raw_path in group.paths:
            candidate = (root / raw_path).resolve()
            if candidate.is_file() and candidate.suffix == ".py":
                discovered.add(candidate)
                continue
            if candidate.is_dir():
                for path in sorted(candidate.rglob("*.py")):
                    if path.is_file():
                        discovered.add(path.resolve())
    return tuple(sorted(discovered))
