"""Planned denominators and independent authoring samples must remain visible."""
import json

import pytest

from osicbench.report import (
    build_report, render_markdown, validate_evaluation_manifest, write_report,
)


def _plan(root, conditions=("a", "b"), samples=(1,), tasks=("t01", "t02"), seeds=(1, 2)):
    rows = [
        {"label": condition if sample == 1 else f"{condition}@s{sample}",
         "condition": condition, "sample": sample, "task": task, "seed": seed}
        for condition in conditions for sample in samples for task in tasks for seed in seeds
    ]
    manifest = {"schema_version": 1, "expected_runs": rows}
    (root / "evaluation_manifest.json").write_text(json.dumps(manifest))
    return rows


def _run(root, row, passed=True, grade=True, directory=None, metadata=None):
    path = directory or root / row["label"] / f"{row['task']}_s{row['seed']}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps(metadata or row))
    if grade:
        (path / "grade.json").write_text(json.dumps(
            {"pass": passed, "dfs": 80.0, "hss": 90.0, "transactions": 12}))
    return path


def test_absent_tasks_and_seeds_remain_in_planned_denominator(tmp_path):
    rows = _plan(tmp_path)
    for row in rows:
        if row["condition"] == "b" or row["task"] == "t01" and row["seed"] == 1:
            _run(tmp_path, row)
    report = build_report(tmp_path)
    a = report["conditions"]["a"]
    assert report["runs"] == 8
    assert report["coverage"]["verified"] is True
    assert report["coverage"]["missing_run"] == 3
    assert a["pass"]["passed"] == 1
    assert a["pass"]["total"] == 4
    assert a["task_pass"]["passed"] == 0
    assert a["task_pass"]["total"] == 2
    assert a["dfs_mean"] == 80.0
    assert a["coverage"]["graded_runs"] == 1
    assert report["paired_comparison"]["task_b_pass_a_fail"] == 2
    assert len(report["missing_results"]) == 3
    assert "missing run 3" in render_markdown(report)


def test_missing_grade_differs_from_observed_failure(tmp_path):
    rows = _plan(tmp_path, conditions=("a",), tasks=("t01",), seeds=(1, 2, 3))
    _run(tmp_path, rows[0], passed=False)
    path = _run(tmp_path, rows[1], grade=False)
    (path / "failure.json").write_text(json.dumps(
        {"stage": "grade", "reason": "grading_timeout"}))
    report = build_report(tmp_path)
    coverage = report["coverage"]
    assert coverage["graded_failed"] == 1
    assert coverage["missing_grade"] == 1
    assert coverage["missing_run"] == 1
    assert coverage["failure_reasons"] == {"grading_timeout": 1}
    assert report["conditions"]["a"]["pass"]["total"] == 3
    assert "grading_timeout" in render_markdown(report)


def test_failed_authoring_without_metadata_is_not_dropped(tmp_path):
    row = _plan(tmp_path, conditions=("a",), tasks=("t01",), seeds=(1,))[0]
    path = tmp_path / row["label"] / "t01_s1"
    path.mkdir(parents=True)
    (path / "failure.json").write_text(json.dumps(
        {"stage": "author", "reason": "missing_author_artifact"}))
    report = build_report(tmp_path)
    assert report["runs"] == 1
    assert report["coverage"]["failure_reasons"] == {"missing_author_artifact": 1}
    assert report["conditions"]["a"]["dfs_mean"] is None
    assert report["conditions"]["a"]["hss_mean"] is None


def test_completely_empty_planned_matrix_reports_zero_not_no_conditions(tmp_path):
    _plan(tmp_path)
    report = build_report(tmp_path)
    assert report["coverage"]["graded_runs"] == 0
    assert report["coverage"]["missing_run"] == 8
    assert set(report["conditions"]) == {"a", "b"}
    assert report["conditions"]["a"]["pass"]["rate"] == 0.0
    assert report["paired_comparison"]["paired_tasks"] == 2
    out = write_report(tmp_path, tmp_path / "_report")
    assert out.exists()
    assert json.loads(out.with_suffix(".json").read_text())["runs"] == 8


def test_repeated_authoring_samples_group_conditions_without_pooled_inference(tmp_path):
    rows = _plan(tmp_path, samples=(1, 2, 3))
    for row in rows:
        # a has task-pass rates [0, 0.5, 1], b has [1, 0.5, 0].
        if row["condition"] == "a":
            passed = row["sample"] == 3 or row["sample"] == 2 and row["task"] == "t01"
        else:
            passed = row["sample"] == 1 or row["sample"] == 2 and row["task"] == "t01"
        _run(tmp_path, row, passed=passed)
    report = build_report(tmp_path)
    assert set(report["conditions"]) == {"a", "b"}
    a = report["conditions"]["a"]
    assert a["sample_task_pass_rates"] == {
        "n": 3, "mean": 0.5, "stddev": 0.5, "min": 0.0, "max": 1.0}
    assert a["task_pass"]["passed"] == 3
    assert a["task_pass"]["total"] == 6
    assert a["task_pass"]["ci_lo"] is None
    assert a["pass"]["ci_lo"] is None
    pc = report["paired_comparison"]
    assert pc["status"] == "paired_by_sample"
    assert len(pc["per_sample"]) == 3
    assert pc["per_sample"]["1"]["task_b_pass_a_fail"] == 2
    assert pc["per_sample"]["3"]["task_a_pass_b_fail"] == 2
    assert pc["mcnemar_p_tasks"] is None
    assert pc["mcnemar_p_runs"] is None
    assert pc["task_pass_rate_delta_b_minus_a"]["mean"] == 0.0
    assert "No pooled p-value" in render_markdown(report)


def test_missing_one_seed_fails_only_its_own_task_sample(tmp_path):
    rows = _plan(tmp_path, conditions=("a",), samples=(1, 2))
    for row in rows:
        if not (row["sample"] == 2 and row["task"] == "t02" and row["seed"] == 2):
            _run(tmp_path, row)
    a = build_report(tmp_path)["conditions"]["a"]
    assert a["samples"]["1"]["task_pass"]["rate"] == 1.0
    assert a["samples"]["2"]["task_pass"]["rate"] == 0.5
    assert a["sample_task_pass_rates"]["mean"] == 0.75


@pytest.mark.parametrize("mutation,match", [
    (lambda m: m.update(schema_version=2), "schema_version"),
    (lambda m: m.update(schema_version=True), "schema_version"),
    (lambda m: m.update(expected_runs=[]), "nonempty"),
    (lambda m: m["expected_runs"].append(m["expected_runs"][0].copy()), "Duplicate expected"),
    (lambda m: m["expected_runs"][0].update(sample=0), "positive integer"),
    (lambda m: m["expected_runs"][0].update(sample=True), "positive integer"),
    (lambda m: m["expected_runs"][0].update(seed="1"), "integer seed"),
    (lambda m: m["expected_runs"][0].update(condition=""), "condition"),
    (lambda m: m["expected_runs"][0].update(label="../a"), "path components"),
    (lambda m: m["expected_runs"][0].update(condition="other"), "multiple condition"),
    (lambda m: m["expected_runs"][0].update(label="other"), "multiple labels"),
    (lambda m: m["expected_runs"].pop(), "unequal planned task/seed"),
])
def test_invalid_manifest_rejected(tmp_path, mutation, match):
    _plan(tmp_path)
    path = tmp_path / "evaluation_manifest.json"
    manifest = json.loads(path.read_text())
    mutation(manifest)
    with pytest.raises(ValueError, match=match):
        validate_evaluation_manifest(manifest)


def test_different_authoring_sample_ids_cannot_be_paired(tmp_path):
    rows = _plan(tmp_path)
    for row in rows:
        if row["condition"] == "b":
            row["sample"] = 2
    with pytest.raises(ValueError, match="unequal planned authoring sample"):
        validate_evaluation_manifest({"schema_version": 1, "expected_runs": rows})


@pytest.mark.parametrize("manifest", [False, True])
def test_duplicate_actual_identity_never_overwrites_pairing(tmp_path, manifest):
    row = {"label": "a", "task": "t01", "seed": 1}
    if manifest:
        row = _plan(tmp_path, conditions=("a",), tasks=("t01",), seeds=(1,))[0]
    _run(tmp_path, row)
    _run(tmp_path, row, directory=tmp_path / "duplicate")
    with pytest.raises(ValueError, match="Duplicate actual"):
        build_report(tmp_path)


def test_unexpected_actual_identity_rejected(tmp_path):
    _plan(tmp_path)
    _run(tmp_path, {"label": "extra", "task": "t01", "seed": 1})
    with pytest.raises(ValueError, match="Unexpected actual"):
        build_report(tmp_path)


def test_metadata_directory_label_mismatch_rejected(tmp_path):
    row = _plan(tmp_path)[0]
    _run(tmp_path, row, directory=tmp_path / "wrong" / "t01_s1")
    with pytest.raises(ValueError, match="directory/metadata"):
        build_report(tmp_path)


@pytest.mark.parametrize("field,value", [("condition", "wrong"), ("sample", 42)])
def test_metadata_manifest_group_mismatch_rejected(tmp_path, field, value):
    row = _plan(tmp_path)[0]
    _run(tmp_path, row, metadata={**row, field: value})
    with pytest.raises(ValueError, match="metadata/manifest"):
        build_report(tmp_path)


def test_grade_without_meta_is_not_silently_ignored(tmp_path):
    (tmp_path / "grade.json").write_text('{"pass": true}')
    with pytest.raises(ValueError, match="no metadata"):
        build_report(tmp_path)


@pytest.mark.parametrize("planned", [False, True])
def test_agent_json_fixtures_do_not_masquerade_as_run_metadata(tmp_path, planned):
    row = {"label": "a", "task": "t01", "seed": 1}
    if planned:
        row = _plan(tmp_path, conditions=("a",), tasks=("t01",), seeds=(1,))[0]
    path = _run(tmp_path, row)
    for directory in ("submission", "results", "farm", "io"):
        fixtures = path / directory / "fixtures"
        fixtures.mkdir(parents=True)
        for filename in ("meta.json", "grade.json", "failure.json"):
            (fixtures / filename).write_text('{"fixture": true}')
    report = build_report(tmp_path)
    assert report["runs"] == 1
    assert report["conditions"]["a"]["pass"]["passed"] == 1


def test_nested_fixtures_in_incomplete_planned_run_are_ignored(tmp_path):
    _plan(tmp_path, conditions=("a",), tasks=("t01",), seeds=(1,))
    fixtures = tmp_path / "a" / "t01_s1" / "submission"
    fixtures.mkdir(parents=True)
    (fixtures / "grade.json").write_text('{"pass": true}')
    report = build_report(tmp_path)
    assert report["coverage"]["missing_run"] == 1
    assert report["conditions"]["a"]["pass"]["passed"] == 0


def test_grade_plus_failure_is_ambiguous(tmp_path):
    row = _plan(tmp_path)[0]
    path = _run(tmp_path, row)
    (path / "failure.json").write_text('{"reason": "grading_error"}')
    with pytest.raises(ValueError, match="both grade and failure"):
        build_report(tmp_path)


def test_unexpected_failure_record_is_not_ignored(tmp_path):
    _plan(tmp_path)
    (tmp_path / "failure.json").write_text('{"reason": "grading_error"}')
    with pytest.raises(ValueError, match="Unexpected failure"):
        build_report(tmp_path)


@pytest.mark.parametrize("passed", ["false", 1, None])
def test_non_boolean_grade_is_rejected(tmp_path, passed):
    row = {"label": "a", "task": "t01", "seed": 1}
    _run(tmp_path, row, passed=passed)
    with pytest.raises(ValueError, match="boolean pass"):
        build_report(tmp_path)


def test_json_null_is_not_an_absent_grade(tmp_path):
    path = _run(tmp_path, {"label": "a", "task": "t01", "seed": 1})
    (path / "grade.json").write_text("null")
    with pytest.raises(ValueError, match="boolean pass"):
        build_report(tmp_path)


def test_legacy_reports_explicitly_warn_unverified_coverage(tmp_path):
    row = {"label": "a", "task": "t01", "seed": 1}
    _run(tmp_path, row, grade=False)
    report = build_report(tmp_path)
    assert report["coverage"]["status"] == "unverified_legacy"
    assert report["conditions"]["a"]["pass"]["total"] == 1
    assert report["conditions"]["a"]["pass"]["passed"] == 0
    assert "completely absent tasks/seeds cannot be detected" in render_markdown(report)


def test_legacy_pairing_does_not_compare_unequal_seed_sets(tmp_path):
    _run(tmp_path, {"label": "a", "task": "t01", "seed": 1})
    _run(tmp_path, {"label": "b", "task": "t01", "seed": 2})
    report = build_report(tmp_path)
    assert report["paired_comparison"]["status"] == "unavailable_coverage_mismatch"
    assert "Paired comparison unavailable" in render_markdown(report)
