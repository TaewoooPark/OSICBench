"""Replay publication keeps evidence bytes and excludes private execution state."""
import hashlib
import json

import pytest

from experiments.model_matrix import publish


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == "t01.json" and path.parent.parent.name == "authoring":
        unsigned = {key: item for key, item in value.items() if key != "record_sha256"}
        value["record_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                                                           allow_nan=False).encode()).hexdigest()
    path.write_text(json.dumps(value))


def _fixture(tmp_path, *, graded=True, failure=False):
    root, tasks, output = (tmp_path / name for name in ("runs", "tasks", "public"))
    row = {"condition": "model-low", "label": "model-low", "sample": 1, "task": "t01", "seed": 19}
    manifest = {"schema_version": 1, "benchmark_commit": "a" * 40, "expected_runs": [row],
                "conditions": [{"id": "model-low", "model": "model", "provider": "anthropic", "effort": "low"}],
                "contrasts": [], "environment": {"os": "test", "python": "3.11", "secret": "never-export"},
                "preflight": {"private": "/Users/private/runtime"}, "account_id": "never-export"}
    _write(root / "evaluation_manifest.json", manifest)
    _write(tasks / "t01/task.yaml", {"id": "t01", "hss": []})
    run = root / "model-low/t01_s19"
    if graded:
        _write(run / "grade.json", {"pass": True, "dfs": 100, "hss": 100, "transactions": 5})
        _write(run / "meta.json", {**row, "mode": "a", "wall_s": 2,
                                  "private": "/Users/private/workspace"})
        (run / "farm").mkdir()
        (run / "farm/recorder.jsonl").write_bytes(b'{"kind":"query","command":"*IDN?"}\n')
        _write(run / "farm/endpoints.json", {"host": "private-endpoint"})
        _write(run / "results/result.json", {"value": 1})
        (run / "agent.out").write_text("never-export")
        original = root / "private/artifacts/model-low/t01"
        original.mkdir(parents=True)
        (original / "main.py").write_bytes(b'import json\nprint("original")\n')
        (original / "helper.py").write_text("VALUE = 1\n")
        (original / "agent.out").write_text("never-export")
        fingerprint, _ = publish._artifact(original)
        _write(run / "adapter.json", {"artifact_sha256": fingerprint, "private_log": "/Users/private/log"})
        _write(root / "authoring/model-low/t01.json", {
            **row, "artifact_sha256": fingerprint, "wall_s": 10, "status": "completed", "eligible_for_grading": True,
            "attempts": [{"timed_out": False, "stderr": "never-export"}],
            "provider_audit": {"valid": True, "requested_model": "model", "resolved_model": "model",
                               "requested_effort": "low", "effort_verification": "launch_configuration_only",
                               "primary_models": ["model"], "auxiliary_models": ["aux"],
                               "errors": ["never-export"], "usage": {"input_tokens": 10, "api_key": "never-export"}}})
        _write(run / "submission/main.py", {"changed_after_execution": True})
    if failure:
        _write(run / "failure.json", {**row, "reason": "grading_timeout", "error": "/Users/private/error"})
    return root, tasks, output, run


def _export(tmp_path, **kwargs):
    root, tasks, output, run = _fixture(tmp_path, **kwargs)
    summary = publish.export_bundle(root, tasks, output)
    return root, tasks, output, run, summary


def test_complete_bundle_preserves_replay_bytes_and_hashes(tmp_path):
    root, tasks, output, run, result = _export(tmp_path)
    assert result["status"] == "complete" and result["fully_graded"]
    assert result["quality_ranking_claim"] is False
    for relative in ("grade.json", "farm/recorder.jsonl", "results/result.json"):
        assert (output / "model-low/t01_s19" / relative).read_bytes() == (run / relative).read_bytes()
    assert (output / "model-low/t01_s19/submission/main.py").read_bytes() == b'import json\nprint("original")\n'
    records = json.loads((output / "files.sha256.json").read_text())["files"]
    for record in records:
        assert hashlib.sha256((output / record["path"]).read_bytes()).hexdigest() == record["sha256"]
    meta = next(item for item in records if item["path"].endswith("/meta.json"))
    assert meta["original_sha256"] == hashlib.sha256((run / "meta.json").read_bytes()).hexdigest()
    encoded = "\n".join(path.read_text() for path in output.rglob("*") if path.is_file())
    assert "never-export" not in encoded and "/Users/" not in encoded
    assert not list(output.rglob("agent.out")) and not list(output.rglob("endpoints.json"))
    analysis = json.loads((output / "analysis/analysis.json").read_text())
    assert analysis["conditions"]["model-low"]["authoring"]["provider_model_observations"]["observed_primary_models"] == ["model"]
    author = json.loads((output / "authoring/model-low/t01.json").read_text())
    assert author["provider_audit"]["effort_verification"] == "launch_configuration_only"
    assert author["provider_audit"]["auxiliary_models"] == ["aux"]


@pytest.mark.parametrize("failure,status,accounted", [(False, "incomplete", False), (True, "complete", True)])
def test_missing_and_recorded_failure_accounting(tmp_path, failure, status, accounted):
    _, _, output, _, result = _export(tmp_path, graded=False, failure=failure)
    assert result["status"] == status and result["all_planned_runs_accounted"] is accounted
    assert not result["fully_graded"] and not result["quality_ranking_claim"]
    assert "not a fully observed model-quality ranking" in (output / "analysis/analysis.md").read_text()


@pytest.mark.parametrize("payload", [
    b'{"note":"/Users/private/secret"}', b'{"note":"/home/private/secret"}',
    b'{"note":"sk-ant-' + b'x' * 24 + b'"}',
    b'{"note":"\\u002fUsers\\u002fprivate"}',
    b'{"Authorization":"Bearer ' + b'x' * 24 + b'"}',
    b'{"access_token":"private-value"}', b'\x80\x81',
    b'{"\\u0061ccess_token":"private-value"}',
    b'{"ok":true}\n{"note":"\\u002fUsers\\u002fprivate"}\n',
])
def test_private_or_unknown_binary_results_abort_without_partial_publication(tmp_path, payload):
    root, tasks, output, run = _fixture(tmp_path)
    (run / "results/result.json").write_bytes(payload)
    with pytest.raises(publish.ExportError) as error:
        publish.export_bundle(root, tasks, output)
    assert "private-value" not in str(error.value) and "/Users/" not in str(error.value)
    assert not output.exists() and not list(tmp_path.glob(".replay-export-*"))


@pytest.mark.parametrize("target", ["grade.json", "results/result.json", "farm/recorder.jsonl"])
def test_selected_symlinks_are_rejected(tmp_path, target):
    root, tasks, output, run = _fixture(tmp_path)
    path = run / target
    path.unlink()
    path.symlink_to(tasks / "t01/task.yaml")
    with pytest.raises(publish.ExportError):
        publish.export_bundle(root, tasks, output)
    assert not output.exists()


def test_unsafe_manifest_paths_are_rejected_without_echo(tmp_path):
    root, tasks, output, _ = _fixture(tmp_path)
    path = root / "evaluation_manifest.json"
    value = json.loads(path.read_text())
    value["expected_runs"][0]["label"] = "../../secret"
    _write(path, value)
    with pytest.raises(publish.ExportError) as error:
        publish.export_bundle(root, tasks, output)
    assert "secret" not in str(error.value)


def test_existing_output_and_overlap_are_rejected(tmp_path):
    root, tasks, output, _ = _fixture(tmp_path)
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    with pytest.raises(publish.ExportError):
        publish.export_bundle(root, tasks, output)
    assert (output / "keep.txt").read_text() == "keep"
    with pytest.raises(publish.ExportError):
        publish.export_bundle(root, tasks, root / "export")


def test_invalid_program_remains_evidence(tmp_path):
    root, tasks, output, run = _fixture(tmp_path)
    original = root / "private/artifacts/model-low/t01"
    source = b'import external_package\n__import__("dynamic")\nthis is invalid syntax !!!\n'
    (original / "main.py").write_bytes(source)
    author = root / "authoring/model-low/t01.json"
    value = json.loads(author.read_text())
    value["artifact_sha256"] = publish._artifact(original)[0]
    _write(author, value)
    _write(run / "adapter.json", {"artifact_sha256": value["artifact_sha256"]})
    publish.export_bundle(root, tasks, output)
    assert (output / "model-low/t01_s19/submission/main.py").read_bytes() == source


def test_frozen_artifact_changes_are_rejected(tmp_path):
    root, tasks, output, _ = _fixture(tmp_path)
    (root / "private/artifacts/model-low/t01/main.py").write_text("changed")
    with pytest.raises(publish.ExportError, match="original_artifact_hash_mismatch"):
        publish.export_bundle(root, tasks, output)


def test_unknown_binary_is_rejected_even_when_artifact_hash_matches(tmp_path):
    root, tasks, output, _ = _fixture(tmp_path)
    original = root / "private/artifacts/model-low/t01"
    (original / "unknown.bin").write_bytes(b'\x80\x81')
    author = root / "authoring/model-low/t01.json"
    value = json.loads(author.read_text())
    value["artifact_sha256"] = publish._artifact(original)[0]
    _write(author, value)
    with pytest.raises(publish.ExportError, match="unknown_file_requires_review"):
        publish.export_bundle(root, tasks, output)


def test_repository_rig_binary_requires_exact_name_and_bytes(tmp_path):
    root, tasks, output, run = _fixture(tmp_path)
    original = root / "private/artifacts/model-low/t01"
    (original / "rig").mkdir()
    (tasks / "t01/rig").mkdir()
    binary = b'\x80\x81\x00'
    (original / "rig/calibration.dat").write_bytes(binary)
    (tasks / "t01/rig/calibration.dat").write_bytes(binary)
    author = root / "authoring/model-low/t01.json"
    value = json.loads(author.read_text())
    value["artifact_sha256"] = publish._artifact(original)[0]
    _write(author, value)
    _write(run / "adapter.json", {"artifact_sha256": value["artifact_sha256"]})
    result = publish.export_bundle(root, tasks, output)
    assert (output / "model-low/t01_s19/submission/rig/calibration.dat").read_bytes() == binary
    assert set(result["postprocessing_sha256"]) == {"publish.py", "analyze.py"}


def test_grade_and_original_artifact_must_be_linked(tmp_path):
    root, tasks, output, run = _fixture(tmp_path)
    _write(run / "adapter.json", {"artifact_sha256": "0" * 64})
    with pytest.raises(publish.ExportError, match="grade_artifact_hash_mismatch"):
        publish.export_bundle(root, tasks, output)
    assert not output.exists() and not list(tmp_path.glob(".replay-export-*"))


def test_ungraded_author_provenance_is_retained_without_errors(tmp_path):
    root, tasks, output, _ = _fixture(tmp_path, graded=False)
    row = json.loads((root / "evaluation_manifest.json").read_text())["expected_runs"][0]
    _write(root / "authoring/model-low/t01.json", {
        **row, "status": "blocked", "attempts": [{"timed_out": True, "error": "/Users/private/error"}],
        "provider_audit": {"valid": False, "requested_model": "model", "errors": ["private-error"]}})
    publish.export_bundle(root, tasks, output)
    author = json.loads((output / "authoring/model-low/t01.json").read_text())
    assert author["status"] == "blocked" and author["attempts"] == [{"timed_out": True}]
    assert "errors" not in author["provider_audit"]


@pytest.mark.parametrize("mutation,reason", [
    ({"status": "blocked"}, "ineligible_author_has_grade"),
    ({"eligible_for_grading": False}, "ineligible_author_has_grade"),
    ({"attempts": [{"cleanup_error": "private-cleanup-error"}]}, "unresolved_author_cleanup"),
    ({"provider_audit": {"valid": False}}, "invalid_author_audit_has_grade"),
])
def test_ineligible_or_invalid_authors_cannot_publish_grades(tmp_path, mutation, reason):
    root, tasks, output, _ = _fixture(tmp_path)
    path = root / "authoring/model-low/t01.json"
    author = json.loads(path.read_text())
    author.update(mutation)
    _write(path, author)
    with pytest.raises(publish.ExportError, match=reason):
        publish.export_bundle(root, tasks, output)


def test_changed_author_record_hash_is_rejected(tmp_path):
    root, tasks, output, _ = _fixture(tmp_path)
    path = root / "authoring/model-low/t01.json"
    author = json.loads(path.read_text())
    author["wall_s"] = 500
    path.write_text(json.dumps(author))
    with pytest.raises(publish.ExportError, match="author_record_hash_mismatch"):
        publish.export_bundle(root, tasks, output)


@pytest.mark.parametrize("field", ["cleanup_error", "timed_out", "error"])
def test_grading_infrastructure_failures_reject_stale_grades(tmp_path, field):
    root, tasks, output, run = _fixture(tmp_path)
    path = run / "adapter.json"
    adapter = json.loads(path.read_text())
    adapter[field] = True
    _write(path, adapter)
    with pytest.raises(publish.ExportError, match="unresolved_grading_failure"):
        publish.export_bundle(root, tasks, output)
