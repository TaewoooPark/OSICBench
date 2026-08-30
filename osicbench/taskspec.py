"""Task loading and validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

REQUIRED_KEYS = ("id", "wall_clock_limit_s", "farm")


@dataclass
class TaskSpec:
    task_dir: Path
    config: Dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.config["id"])

    @property
    def wall_clock_limit_s(self) -> float:
        return float(self.config["wall_clock_limit_s"])

    @property
    def post_exit_grace_s(self) -> float:
        return float(self.config.get("post_exit_grace_s", 2.0))

    @property
    def sigkill_at_s(self) -> float | None:
        v = self.config.get("sigkill_at_s")
        return None if v is None else float(v)

    @property
    def restart_after_kill_s(self) -> float | None:
        """When set (with sigkill_at_s), the runner restarts the killed
        submission once, this many seconds after the kill."""
        v = self.config.get("restart_after_kill_s")
        return None if v is None else float(v)

    @property
    def mode_b_resets(self) -> int:
        return int(self.config.get("mode_b_resets", 3))

    @property
    def brief_path(self) -> Path:
        return self.task_dir / "brief.md"

    @property
    def yaml_path(self) -> Path:
        return self.task_dir / "task.yaml"

    @property
    def oracle_path(self) -> Path:
        return self.task_dir / "oracle" / "grade.py"

    @property
    def grading_cfg(self) -> Dict[str, Any]:
        return dict(self.config.get("grading") or {})

    @property
    def hss_rules(self) -> List[Dict[str, Any]]:
        return list(self.config.get("hss") or [])

    @property
    def budgets(self) -> Dict[str, Any]:
        return dict(self.config.get("budgets") or {})

    @property
    def manual_paths(self) -> List[Path]:
        repo_root = self.task_dir.parent.parent
        out: List[Path] = []
        for name in self.config.get("manuals") or []:
            out.append(repo_root / "manuals" / name)
        return out

    def references(self) -> List[Path]:
        ref_dir = self.task_dir / "reference"
        return sorted(ref_dir.glob("*.py")) if ref_dir.is_dir() else []

    def mutants(self) -> List[Path]:
        mut_dir = self.task_dir / "mutants"
        return sorted(mut_dir.glob("*.py")) if mut_dir.is_dir() else []


def load_task(task_dir: Path) -> TaskSpec:
    task_dir = Path(task_dir).resolve()
    yaml_path = task_dir / "task.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"no task.yaml in {task_dir}")
    with open(yaml_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    for key in REQUIRED_KEYS:
        if key not in config:
            raise ValueError(f"{task_dir.name}: task.yaml missing {key!r}")
    if not (task_dir / "brief.md").exists():
        raise ValueError(f"{task_dir.name}: brief.md missing")
    if not (task_dir / "oracle" / "grade.py").exists():
        raise ValueError(f"{task_dir.name}: oracle/grade.py missing")
    return TaskSpec(task_dir=task_dir, config=config)


def discover_tasks(tasks_root: Path) -> List[TaskSpec]:
    out: List[TaskSpec] = []
    for child in sorted(Path(tasks_root).iterdir()):
        if child.is_dir() and (child / "task.yaml").exists():
            out.append(load_task(child))
    return out
