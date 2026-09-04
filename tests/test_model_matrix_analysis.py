"""Publication statistics preserve pairing, coverage and private-data boundaries."""
import copy
import json

import pytest

from experiments.model_matrix.analyze import (
    build_analysis, holm_adjust, render_markdown, write_analysis,
)


def _fixture(tmp_path, conditions=("a", "b", "c"), samples=(1, 2, 3),
             tasks=("t01", "t02"), seeds=(1, 2), contrasts=None):
    root, task_root = tmp_path / "runs", tmp_path / "tasks"
    root.mkdir()
    for task in tasks:
        directory = task_root / task
        directory.mkdir(parents=True)
        config = {"id": task, "hss": ([{"check": "end_false", "required": True}]
                                      if task == tasks[0] else [])}
        (directory / "task.yaml").write_text(json.dumps(config))
    expected = [
        {"condition": condition, "sample": sample, "label": condition if sample == 1 else f"{condition}@s{sample}",
         "task": task, "seed": seed}
        for condition in conditions for sample in samples for task in tasks for seed in seeds
    ]
    manifest = {"schema_version": 1, "expected_runs": expected,
                "contrasts": contrasts if contrasts is not None else [
                    {"id": "a-vs-b", "a": "a", "b": "b"},
                    {"id": "a-vs-c", "a": "a", "b": "c"}],
                "workdir": "/private/hidden-workspace", "account_id": "secret-account-marker"}
    (root / "evaluation_manifest.json").write_text(json.dumps(manifest))
    return root, task_root, expected


def _grade(root, row, passed=True, rs=None, failed_rule=False):
    path = root / row["label"] / f"{row['task']}_s{row['seed']}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps(row))
    grade = {"pass": passed, "dfs": 100.0 if passed else 0.0,
             "hss": 40.0 if failed_rule else 100.0,
             "transactions": 10, "fabricated": False,
             "hss_findings": ([{"ok": not failed_rule, "required": True,
                                "detail": "/private/hidden-safety-detail"}]
                              if row["task"] == "t01" else [])}
    if rs is not None:
        grade["rs"] = rs
    (path / "grade.json").write_text(json.dumps(grade))
    return path


def _manifest(root):
    return json.loads((root / "evaluation_manifest.json").read_text())


def test_holm_adjustment_is_step_down_and_monotone():
    assert holm_adjust({"one": 0.01, "two": 0.04, "three": 0.03}) == {
        "one": 0.03, "three": 0.06, "two": 0.06}
    assert holm_adjust({"one": 0.8, "two": 0.9}) == {"one": 1.0, "two": 1.0}
    assert holm_adjust({}) == {}


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_holm_rejects_invalid_probabilities(value):
    with pytest.raises(ValueError, match="p-values"):
        holm_adjust({"bad": value})


def test_all_conditions_and_samples_remain_separate(tmp_path):
    root, tasks, rows = _fixture(tmp_path)
    for row in rows:
        passed = row["condition"] != "a" or row["sample"] == 3 or (
            row["sample"] == 2 and row["task"] == "t01")
        _grade(root, row, passed=passed)
    result = build_analysis(root, tasks)
    assert set(result["conditions"]) == {"a", "b", "c"}
    assert result["design"]["planned_artifacts"] == 18
    assert result["design"]["planned_runs"] == 36
    a = result["conditions"]["a"]
    assert a["sample_task_pass_rates"] == {
        "n": 3, "mean": 0.5, "stddev": 0.5, "min": 0.0, "max": 1.0}
    assert a["task_pass"]["ci_lo"] is None
    assert a["run_pass"]["ci_lo"] is None
    assert len(result["contrasts"]) == 2
    assert result["contrasts"][0]["sample_task_pass_rate_deltas"]["mean"] == 0.5
    assert result["contrasts"][0]["pooled_p_value"] is None
    assert "model-plus-native-harness" in render_markdown(result)


def test_seed_repeats_do_not_inflate_task_discordance(tmp_path):
    root, tasks, rows = _fixture(tmp_path, samples=(1,))
    for row in rows:
        passed = not (row["condition"] == "a" and row["task"] == "t02" and row["seed"] == 2)
        _grade(root, row, passed=passed)
    result = build_analysis(root, tasks)
    pc = result["contrasts"][0]["samples"]["1"]
    assert pc["paired_tasks"] == 2
    assert pc["b_pass_a_fail"] == 1
    assert pc["task_pass_rate_delta_b_minus_a"] == 0.5
    assert result["design"]["preliminary_single_sample"] is True
    assert any("one authoring sample" in note for note in result["caveats"])


def test_holm_family_is_predeclared_contrasts_within_each_sample(tmp_path):
    root, tasks, rows = _fixture(tmp_path, tasks=("t01", "t02", "t03", "t04"))
    for row in rows:
        passed = row["sample"] != 1 or row["condition"] != "a"
        _grade(root, row, passed=passed)
    result = build_analysis(root, tasks)
    for contrast in result["contrasts"]:
        first, second = contrast["samples"]["1"], contrast["samples"]["2"]
        assert first["mcnemar_p_tasks"] == 0.125
        assert first["holm_p_tasks"] == 0.25
        assert first["holm_family_size"] == 2
        assert second["holm_p_tasks"] == 1.0
        assert contrast["pooled_p_value"] is None


def test_missing_grades_are_operational_failures_not_zero_subscores(tmp_path):
    root, tasks, rows = _fixture(tmp_path)
    for row in rows:
        if row["condition"] != "a":
            _grade(root, row, rs=75.0)
    result = build_analysis(root, tasks)
    a = result["conditions"]["a"]
    assert a["coverage"]["planned_runs"] == 12
    assert a["coverage"]["observed_failures"] == 0
    assert a["coverage"]["missing_runs_or_grades"] == 12
    assert a["sample_task_pass_rates"]["mean"] == 0.0
    assert a["metrics"]["dfs"]["mean"] is None
    assert a["metrics"]["rs"]["ungraded_runs"] == 12
    assert all(row["rs"] is None for row in result["runs"] if row["condition"] == "a")
    assert any("matrix is incomplete" in note for note in result["caveats"])


def test_rs_coverage_and_hss_applicable_subset_are_explicit(tmp_path):
    root, tasks, rows = _fixture(tmp_path)
    for row in rows:
        _grade(root, row, rs=60.0 if row["task"] == "t01" else None,
               failed_rule=row["task"] == "t01")
    result = build_analysis(root, tasks)
    a = result["conditions"]["a"]
    assert a["metrics"]["rs"]["mean"] == 60.0
    assert a["metrics"]["rs"]["observed_runs"] == 6
    assert a["metrics"]["rs"]["graded_without_metric"] == 6
    assert a["metrics"]["hss"]["mean"] == 70.0
    safety = a["hss_applicable"]
    assert safety["applicable_tasks"] == ["t01"]
    assert safety["hss"]["mean"] == 40.0
    assert safety["hss"]["planned_runs"] == 6
    assert safety["runs_with_required_rule_failure"] == 6


def test_public_output_uses_allowlisted_fields_only(tmp_path):
    root, tasks, rows = _fixture(tmp_path)
    absent = rows[0]
    for row in rows[1:]:
        _grade(root, row)
    failure = root / absent["label"] / f"{absent['task']}_s{absent['seed']}" / "failure.json"
    failure.parent.mkdir(parents=True, exist_ok=True)
    failure.write_text(json.dumps({"reason": "/private/failure-secret", "error": "secret-error-token"}))
    author = root / "authoring" / "a" / "t01.json"
    author.parent.mkdir(parents=True)
    author.write_text(json.dumps({
        "label": "a", "task": "t01", "wall_s": 12.5,
        "attempts": [{"timed_out": True, "error": "secret-attempt-token"}],
        "provider_audit": {"valid": False, "errors": ["secret-provider-error"],
                           "resolved_model": "/private/model-secret", "account": "secret-user-account",
                           "requested_model": "public-model", "requested_effort": "high",
                           "observed_primary_models": ["public-model", "/private/hidden-model"],
                           "usage": {"input_tokens": 123, "output_tokens": 45, "api_key": "secret-key"}}}))
    result = build_analysis(root, tasks)
    encoded = json.dumps(result) + render_markdown(result)
    assert "/private/" not in encoded
    assert "secret-" not in encoded
    assert str(tmp_path) not in encoded
    assert "other_recorded_failure" in encoded
    authoring = result["conditions"]["a"]["authoring"]
    assert authoring["wall_s_total"] == 12.5
    assert authoring["provider_audit_invalid_records"] == 1
    assert authoring["token_usage"]["input_tokens"] == {"total": 123, "observed_artifacts": 1}
    assert authoring["billing"] is None
    assert authoring["provider_model_observations"]["observed_primary_models"] == ["public-model"]


@pytest.mark.parametrize("mutation", [
    lambda m: m["contrasts"].append(copy.deepcopy(m["contrasts"][0])),
    lambda m: m["contrasts"].append({"id": "reverse", "a": "b", "b": "a"}),
    lambda m: m["contrasts"][0].update(a="not-planned"),
    lambda m: m["contrasts"][0].update(a="b"),
    lambda m: m["contrasts"][0].update(id="/private/contrast"),
])
def test_bad_contrasts_rejected_before_publication(tmp_path, mutation):
    root, tasks, _ = _fixture(tmp_path)
    manifest = _manifest(root)
    mutation(manifest)
    (root / "evaluation_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        build_analysis(root, tasks)


def test_no_unplanned_pairwise_tests(tmp_path):
    root, tasks, _ = _fixture(tmp_path, contrasts=[])
    result = build_analysis(root, tasks)
    assert result["contrasts"] == []
    assert any("No contrasts were predeclared" in note for note in result["caveats"])


def test_task_yaml_identity_must_match_plan(tmp_path):
    root, tasks, _ = _fixture(tmp_path)
    (tasks / "t01" / "task.yaml").write_text('{"id": "wrong", "hss": []}')
    with pytest.raises(ValueError, match="Task metadata"):
        build_analysis(root, tasks)


def test_existing_publication_is_never_overwritten(tmp_path):
    root, tasks, _ = _fixture(tmp_path)
    output = tmp_path / "publication"
    write_analysis(root, tasks, output)
    before = (output / "analysis.json").read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        write_analysis(root, tasks, output)
    assert (output / "analysis.json").read_bytes() == before
    assert (output / "analysis.md").exists()


def test_partial_existing_publication_is_never_overwritten(tmp_path):
    root, tasks, _ = _fixture(tmp_path)
    output = tmp_path / "publication"
    output.mkdir()
    (output / "analysis.md").write_text("existing report")
    with pytest.raises(FileExistsError):
        write_analysis(root, tasks, output)
    assert (output / "analysis.md").read_text() == "existing report"
    assert not (output / "analysis.json").exists()


@pytest.mark.parametrize("token_value", [-1, 0.25, True, float("inf")])
def test_invalid_provider_token_counts_are_rejected(tmp_path, token_value):
    root, tasks, _ = _fixture(tmp_path)
    path = root / "authoring" / "a" / "t01.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"provider_audit": {"usage": {"input_tokens": token_value}}}))
    with pytest.raises(ValueError):
        build_analysis(root, tasks)


def test_existing_report_integrity_checks_are_reused(tmp_path):
    root, tasks, rows = _fixture(tmp_path)
    _grade(root, rows[0])
    duplicate = root / "duplicate"
    duplicate.mkdir()
    (duplicate / "meta.json").write_text(json.dumps(rows[0]))
    (duplicate / "grade.json").write_text('{"pass": true}')
    with pytest.raises(ValueError, match="Duplicate actual"):
        build_analysis(root, tasks)
