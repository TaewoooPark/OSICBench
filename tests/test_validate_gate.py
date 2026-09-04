"""Release-gate accounting without starting farms or invoking agents."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osicbench import cli


class FakeTask:
    id = "probe"
    task_dir = Path("/unused/probe")

    def references(self):
        return [self.task_dir / "reference" / "ref.py"]

    def mutants(self):
        return [self.task_dir / "mutants" / "mut.py"]


def args_for(tmp_path, **overrides):
    values = dict(tasks="/unused", task=None, seed_list=None, seeds=2,
                  jobs=1, mutants_only=False, refs_only=False,
                  out=str(tmp_path), json_out=str(tmp_path / "gate.json"))
    values.update(overrides)
    return SimpleNamespace(**values)


def fake_validation(monkeypatch, dfs_by_seed=None, escaped=False, tasks=None):
    """Keep scheduling and aggregation real, replacing only farm jobs."""
    dfs_by_seed = dfs_by_seed or {}
    task_list = [FakeTask()] if tasks is None else tasks
    monkeypatch.setattr(cli, "discover_tasks", lambda root: task_list)

    class Future:
        def __init__(self, value):
            self.value = value

        def result(self):
            return self.value

    class Pool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def submit(self, fn, td, sub, seed, expected, out):
            passed = expected or escaped
            return Future((Path(td).name, Path(sub).stem, seed, passed,
                           passed == expected, dfs_by_seed.get(seed, 100.0)))

    monkeypatch.setattr(cli.cf, "ProcessPoolExecutor", Pool)
    monkeypatch.setattr(cli.cf, "as_completed", lambda futures: futures)


def test_cv_above_limit_fails_and_is_persisted(monkeypatch, tmp_path):
    fake_validation(monkeypatch, {1: 70.0, 2: 100.0})
    assert cli._cmd_validate(args_for(tmp_path)) == 1
    record = json.loads((tmp_path / "gate.json").read_text())
    assert record["behaved"] == record["runs"] == 4
    assert record["escapes"] == []
    assert record["validation_passed"] is False
    stability = record["reference_stability"]
    assert stability["evaluated"] is True
    assert stability["per_task"]["probe"]["cv"] == pytest.approx(15 / 85)
    assert stability["per_task"]["probe"]["pass"] is False


def test_explicit_seeds_override_default_count_for_cv(monkeypatch, tmp_path):
    fake_validation(monkeypatch, {41: 70.0, 42: 100.0})
    assert cli._cmd_validate(args_for(tmp_path, seeds=1, seed_list="41,42")) == 1
    record = json.loads((tmp_path / "gate.json").read_text())
    assert record["seeds"] == [41, 42]
    assert record["reference_stability"]["seed_count"] == 2


def test_stable_references_pass_and_keep_escape_schema(monkeypatch, tmp_path):
    fake_validation(monkeypatch, {1: 95.0, 2: 100.0})
    assert cli._cmd_validate(args_for(tmp_path)) == 0
    record = json.loads((tmp_path / "gate.json").read_text())
    assert record["validation_passed"] is True
    assert record["reference_stability"]["per_task"]["probe"]["pass"] is True
    assert record["programs"] == 2 and record["escapes"] == []
    assert set(record["results"][0]) == {"task", "program", "seed", "pass", "ok", "dfs"}


def test_mutant_escape_still_fails(monkeypatch, tmp_path):
    fake_validation(monkeypatch, escaped=True)
    assert cli._cmd_validate(args_for(tmp_path)) == 1
    record = json.loads((tmp_path / "gate.json").read_text())
    assert record["escapes"] == [
        {"task": "probe", "program": "mut", "seeds": [1, 2], "attempts": 2}
    ]
    assert record["validation_passed"] is False


@pytest.mark.parametrize("options, reason", [
    ({"seed_list": "7", "seeds": 10}, "fewer_than_two_seeds"),
    ({"mutants_only": True}, "no_reference_runs"),
])
def test_partial_validation_marks_stability_unevaluated(
        monkeypatch, tmp_path, options, reason):
    fake_validation(monkeypatch)
    assert cli._cmd_validate(args_for(tmp_path, **options)) == 0
    record = json.loads((tmp_path / "gate.json").read_text())
    stability = record["reference_stability"]
    assert stability["evaluated"] is False
    assert stability["per_task"] == {}
    assert stability["not_evaluated_reason"] == reason


@pytest.mark.parametrize("options", [
    {"seeds": 0}, {"seeds": -1}, {"jobs": 0}, {"jobs": -1},
    {"seed_list": ""}, {"seed_list": "  "}, {"seed_list": "1,"},
    {"seed_list": "1,,2"}, {"seed_list": "bad"}, {"seed_list": "1,1"},
    {"refs_only": True, "mutants_only": True},
])
def test_invalid_validation_request_is_rejected(monkeypatch, tmp_path, options):
    fake_validation(monkeypatch)
    assert cli._cmd_validate(args_for(tmp_path, **options)) == 2
    assert not (tmp_path / "gate.json").exists()


def test_empty_task_set_is_not_success(monkeypatch, tmp_path):
    fake_validation(monkeypatch, tasks=[])
    assert cli._cmd_validate(args_for(tmp_path)) == 2


def test_no_selected_programs_is_not_success(monkeypatch, tmp_path):
    task = FakeTask()
    task.references = lambda: []
    fake_validation(monkeypatch, tasks=[task])
    assert cli._cmd_validate(args_for(tmp_path, refs_only=True)) == 2


def test_zero_mean_has_no_infinite_json_cv(monkeypatch, tmp_path):
    fake_validation(monkeypatch, {1: 0.0, 2: 0.0})
    assert cli._cmd_validate(args_for(tmp_path)) == 1
    text = (tmp_path / "gate.json").read_text()
    assert "Infinity" not in text
    assert json.loads(text)["reference_stability"]["per_task"]["probe"]["cv"] is None
