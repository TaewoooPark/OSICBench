"""Composed validation retains full coverage and every unaffected blocker."""
import hashlib
import json
import math
import subprocess
from pathlib import Path

import pytest

from experiments.model_matrix.gate import (
    CORRECTION, GateInvalid, GateNotReady, compose_gate, write_composed_gate,
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _summary(rows, references):
    grouped, escapes, stability = {}, [], {}
    for row in rows:
        grouped.setdefault((row["task"], row["program"]), []).append(row)
    for (task, program), items in sorted(grouped.items()):
        failed = [row["seed"] for row in items if not row["ok"]]
        if failed:
            escapes.append({"task": task, "program": program, "seeds": sorted(failed), "attempts": len(items)})
        if (task, program) in references:
            stability.setdefault(task, []).extend(row["dfs"] for row in items)
    stable = True
    for values in stability.values():
        mean = sum(values) / len(values)
        cv = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values)) / mean if mean else math.inf
        stable = stable and cv <= 0.05
    return {"seeds": list(range(1, 11)), "programs": len(grouped), "runs": len(rows),
            "behaved": sum(row["ok"] for row in rows), "escapes": escapes,
            "validation_passed": not escapes and stable,
            "reference_stability": {"evaluated": True}, "results": rows}


def _fixture(tmp_path, extra_reference_failure=False, variable_reference=False, replacement_escape=False,
             include_repeat=False):
    repo, original, replacement = tmp_path / "repo", tmp_path / "original", tmp_path / "replacement"
    repo.mkdir()
    sources = {
        "osicbench/core.py": b"# stable harness\n", "osicsim/core.py": b"# stable simulator\n",
        "manuals/manual.md": b"Stable manual.\n", "adapters/adapter.py": b"# stable adapter\n",
        "tasks/t03_diode_iv/task.yaml": b"id: t03_diode_iv\n",
        "tasks/t03_diode_iv/reference/ref_ok.py": b"print('reference three')\n",
        CORRECTION: b"print('original negative control')\n",
        "tasks/t01/task.yaml": b"id: t01\n",
        "tasks/t01/reference/ref_ok.py": b"print('reference one')\n",
        "tasks/t01/mutants/m_bad.py": b"print('negative one')\n",
    }
    if include_repeat:
        sources = {name.replace("tasks/t01/", "tasks/t06_thermal_hold/"):
                   (b"id: t06_thermal_hold\n" if name == "tasks/t01/task.yaml" else data)
                   for name, data in sources.items()}
    for name, data in sources.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "test baseline"], check=True)
    base = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    corrected = b"print('revised negative control')\n"
    (repo / CORRECTION).write_bytes(corrected)
    references, rows = set(), []
    for name, data in sources.items():
        parts = Path(name).parts
        if len(parts) != 4 or parts[2] not in ("reference", "mutants"):
            continue
        task, program = parts[1], Path(parts[3]).stem
        expected = parts[2] == "reference"
        if expected:
            references.add((task, program))
        for seed in range(1, 11):
            passed = expected or (name == CORRECTION and seed == 10)
            if extra_reference_failure and task == "t01" and expected and seed == 4:
                passed = False
            dfs = (70.0 if variable_reference and expected and task == "t01" and seed % 2 else 100.0) if passed else 0.0
            rows.append(_run(original, task, program, seed, data, expected, passed, dfs))
    gate = original / "gate.json"
    _write(gate, _summary(rows, references))
    new_rows = [_run(replacement, "t03_diode_iv", "m3_compliance_blind", seed, corrected, False,
                     replacement_escape and seed == 2, 100.0 if replacement_escape and seed == 2 else 0.0)
                for seed in range(1, 11)]
    receipt = _summary(new_rows, set())
    receipt.update(program_sha256=hashlib.sha256(corrected).hexdigest(),
                   original_program_sha256=hashlib.sha256(sources[CORRECTION]).hexdigest())
    _write(replacement / "gate.json", receipt)
    return repo, original, gate, replacement, base


def _run(root, task, program, seed, source, expected, passed, dfs):
    path = root / task / f"{program}_s{seed}"
    _write(path / "grade.json", {"pass": passed, "dfs": dfs, "hss": 100.0,
                                  "budget_ok": True, "fabricated": False,
                                  "notes": ["private-grade-detail"]})
    _write(path / "meta.json", {"task": task, "seed": seed,
                                 "label": "reference" if expected else "mutant", "mode": "a",
                                 "private_path": "/private/hidden-location"})
    (path / "submission").mkdir()
    (path / "submission" / "main.py").write_bytes(source)
    _write(path / "farm" / "recorder.jsonl", {"kind": "session", "private": "hidden-recorder-data"})
    return {"task": task, "program": program, "seed": seed, "pass": passed,
            "ok": passed == expected, "dfs": dfs}


def _compose(fixture):
    repo, original, gate, replacement, base = fixture
    return compose_gate(repo, original, gate, replacement, base_commit=base)


def test_full_composition_preserves_original_failure_history(tmp_path):
    fixture = _fixture(tmp_path)
    result = _compose(fixture)
    assert result["composition"] is True
    assert result["validation_passed"] is True
    assert result["runs"] == result["behaved"] == 40
    assert result["correction_receipt"]["retained_runs"] == 30
    assert result["correction_receipt"]["replaced_runs"] == 10
    assert result["original_gate_history"]["validation_passed"] is False
    assert result["original_gate_history"]["escapes"][0]["seeds"] == [10]
    assert result["source_verification"]["changed_source_files"] == [CORRECTION]
    assert all(set(row) >= {"grade_sha256", "meta_sha256", "recorder_sha256", "submission_sha256"}
               for row in result["results"])
    public = json.dumps(result)
    assert str(tmp_path) not in public
    assert "private-grade-detail" not in public
    assert "hidden-recorder-data" not in public


def test_unaffected_reference_failure_is_not_replaced(tmp_path):
    result = _compose(_fixture(tmp_path, extra_reference_failure=True))
    assert result["validation_passed"] is False
    assert result["behaved"] == 39
    assert result["escapes"] == [{"task": "t01", "program": "ref_ok", "seeds": [4], "attempts": 10}]


def test_reference_stability_is_recomputed_after_composition(tmp_path):
    result = _compose(_fixture(tmp_path, variable_reference=True))
    assert result["escapes"] == []
    assert result["validation_passed"] is False
    stability = result["reference_stability"]["per_task"]["t01"]
    assert stability["cv"] == pytest.approx(15 / 85)
    assert stability["pass"] is False


def test_replacement_escape_still_blocks_release(tmp_path):
    result = _compose(_fixture(tmp_path, replacement_escape=True))
    assert result["validation_passed"] is False
    assert result["escapes"][0]["seeds"] == [2]


@pytest.mark.parametrize("name", ["osicbench/core.py", "osicsim/core.py", "manuals/manual.md", "adapters/adapter.py", "tasks/t01/task.yaml"])
def test_any_other_core_change_invalidates_composition(tmp_path, name):
    fixture = _fixture(tmp_path)
    (fixture[0] / name).write_text("changed source")
    with pytest.raises(GateInvalid, match="outside the one"):
        _compose(fixture)


def test_untracked_core_file_is_not_ignored(tmp_path):
    fixture = _fixture(tmp_path)
    (fixture[0] / "osicbench" / "extra.py").write_text("extra source")
    with pytest.raises(GateInvalid, match="file coverage"):
        _compose(fixture)


def test_runtime_bytecode_does_not_change_source_proof(tmp_path):
    fixture = _fixture(tmp_path)
    cache = fixture[0] / "osicbench" / "__pycache__"
    cache.mkdir()
    (cache / "core.pyc").write_bytes(b"runtime cache")
    assert _compose(fixture)["validation_passed"] is True


def test_symlinked_core_directory_is_rejected(tmp_path):
    fixture = _fixture(tmp_path)
    core = fixture[0] / "osicsim"
    moved = tmp_path / "external_core"
    core.rename(moved)
    core.symlink_to(moved, target_is_directory=True)
    with pytest.raises(GateInvalid, match="symlink"):
        _compose(fixture)


def test_unexpected_actual_run_directory_is_rejected(tmp_path):
    fixture = _fixture(tmp_path)
    _write(fixture[1] / "t01" / "ref_ok_retry_s1" / "meta.json", {})
    with pytest.raises(GateInvalid, match="Unexpected actual"):
        _compose(fixture)


@pytest.mark.parametrize("artifact", ["submission/main.py", "grade.json", "meta.json", "farm/recorder.jsonl"])
def test_every_retained_run_requires_all_artifacts(tmp_path, artifact):
    fixture = _fixture(tmp_path)
    (fixture[1] / "t01" / "ref_ok_s1" / artifact).unlink()
    with pytest.raises(GateInvalid, match="artifact is missing"):
        _compose(fixture)


@pytest.mark.parametrize("which", ["original", "replacement"])
def test_submission_bytes_must_match_the_correct_revision(tmp_path, which):
    fixture = _fixture(tmp_path)
    root = fixture[1] if which == "original" else fixture[3]
    (root / "t03_diode_iv" / "m3_compliance_blind_s1" / "submission" / "main.py").write_text("tampered")
    with pytest.raises(GateInvalid, match="submission"):
        _compose(fixture)


@pytest.mark.parametrize("which", ["original", "replacement"])
@pytest.mark.parametrize("kind", ["missing", "duplicate", "unexpected"])
def test_exact_program_seed_coverage_required(tmp_path, which, kind):
    fixture = _fixture(tmp_path)
    path = fixture[2] if which == "original" else fixture[3] / "gate.json"
    record = json.loads(path.read_text())
    if kind == "missing":
        record["results"].pop()
    elif kind == "duplicate":
        record["results"].append(record["results"][0].copy())
    else:
        record["results"][0]["seed"] = 99
    _write(path, record)
    with pytest.raises(GateInvalid, match="coverage|Duplicate"):
        _compose(fixture)


def test_original_summary_cannot_hide_an_escape(tmp_path):
    fixture = _fixture(tmp_path)
    record = json.loads(fixture[2].read_text())
    record["escapes"] = []
    _write(fixture[2], record)
    with pytest.raises(GateInvalid, match="accounting"):
        _compose(fixture)


def test_grade_and_gate_claims_must_agree(tmp_path):
    fixture = _fixture(tmp_path)
    path = fixture[1] / "t01" / "ref_ok_s1" / "grade.json"
    grade = json.loads(path.read_text())
    grade["dfs"] = 80.0
    _write(path, grade)
    with pytest.raises(GateInvalid, match="summary row"):
        _compose(fixture)


def test_replacement_receipt_source_hash_required(tmp_path):
    fixture = _fixture(tmp_path)
    path = fixture[3] / "gate.json"
    record = json.loads(path.read_text())
    record["program_sha256"] = "0" * 64
    _write(path, record)
    with pytest.raises(GateInvalid, match="source receipt"):
        _compose(fixture)


def test_missing_original_gate_is_not_ready(tmp_path):
    fixture = _fixture(tmp_path)
    fixture[2].unlink()
    with pytest.raises(GateNotReady):
        _compose(fixture)


def test_partial_original_json_is_not_ready(tmp_path):
    fixture = _fixture(tmp_path)
    fixture[2].write_text('{"results":')
    with pytest.raises(GateNotReady):
        _compose(fixture)


def test_output_is_new_and_does_not_overwrite_original(tmp_path):
    fixture = _fixture(tmp_path)
    repo, original, gate, replacement, base = fixture
    old_bytes = gate.read_bytes()
    output = tmp_path / "composed.json"
    write_composed_gate(repo, original, gate, replacement, output, base_commit=base)
    assert gate.read_bytes() == old_bytes
    with pytest.raises(FileExistsError):
        write_composed_gate(repo, original, gate, replacement, output, base_commit=base)
    with pytest.raises(FileExistsError):
        write_composed_gate(repo, original, gate, replacement, gate, base_commit=base)


def _sleep_fixture(tmp_path):
    fixture = _fixture(tmp_path, include_repeat=True)
    repo, original, gate, replacement, base = fixture
    affected = []
    for path in original.glob("*/*/farm/recorder.jsonl"):
        relative = path.parent.parent.relative_to(original).as_posix()
        overlaps = relative.startswith("t06_thermal_hold/") and relative.rsplit("_s", 1)[1] in ("1", "2")
        epoch = 1100 if overlaps else 2000
        path.write_text(json.dumps({"t": epoch, "kind": "session"}) + "\n" +
                        json.dumps({"t": epoch + 1, "kind": "session"}) + "\n")
        if overlaps:
            affected.append(relative)
    incident = tmp_path / "incident.json"
    _write(incident, {"incident": {"start_utc": "1970-01-01T00:16:40+00:00", "end_utc": "1970-01-01T00:25:00+00:00"},
                      "overlap": {"run_ids": sorted(affected), "task_counts": {"t06_thermal_hold": len(affected)}}})
    repeat_root, repeat_gate = tmp_path / "awake", tmp_path / "awake" / "gate.json"
    rows, references = [], set()
    for category in ("reference", "mutants"):
        for program in (repo / "tasks" / "t06_thermal_hold" / category).glob("*.py"):
            expected = category == "reference"
            if expected:
                references.add(("t06_thermal_hold", program.stem))
            for seed in range(1, 11):
                rows.append(_run(repeat_root, "t06_thermal_hold", program.stem, seed,
                                 program.read_bytes(), expected, expected, 100.0 if expected else 0.0))
                _write(repeat_root / "t06_thermal_hold" / f"{program.stem}_s{seed}" / "farm" / "recorder.jsonl",
                       {"t": 3000, "kind": "session"})
    _write(repeat_gate, _summary(rows, references))
    return fixture, {"repeat_task_root": repeat_root, "repeat_task_gate": repeat_gate, "incident_audit": incident}


def _compose_sleep(fixture, options):
    repo, original, gate, replacement, base = fixture
    return compose_gate(repo, original, gate, replacement, base_commit=base, **options)


def test_sleep_replacement_covers_whole_task_not_only_affected_rows(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    result = _compose_sleep(fixture, options)
    assert result["validation_passed"] is True
    receipt = result["task_repeat_receipt"]
    assert receipt["reason"] == "host_suspend"
    assert receipt["contaminated_original_runs"] == 4
    assert receipt["full_task_runs_replaced"] == 20
    assert receipt["nonoverlapping_task_runs_also_replaced"] == 16
    assert len(receipt["original_task_results"]) == 20
    assert receipt["original_rows_checked_for_overlap"] == 40
    assert sum(row["origin"] == "task_repeat" for row in result["results"]) == 20
    assert result["correction_receipt"]["retained_runs"] == 10


def test_incident_receipt_cannot_drop_a_passing_overlapping_row(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    path = options["incident_audit"]
    receipt = json.loads(path.read_text())
    receipt["overlap"]["run_ids"].pop()
    _write(path, receipt)
    with pytest.raises(GateInvalid, match="all original overlapping"):
        _compose_sleep(fixture, options)


def test_suspension_on_another_task_blocks_t06_only_composition(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    path = fixture[1] / "t03_diode_iv" / "ref_ok_s1" / "farm" / "recorder.jsonl"
    _write(path, {"t": 1100})
    receipt = json.loads(options["incident_audit"].read_text())
    receipt["overlap"]["run_ids"].append("t03_diode_iv/ref_ok_s1")
    _write(options["incident_audit"], receipt)
    with pytest.raises(GateInvalid, match="outside the declared"):
        _compose_sleep(fixture, options)


def test_repeat_task_cannot_use_only_previously_failed_or_overlapping_rows(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    path = options["repeat_task_gate"]
    receipt = json.loads(path.read_text())
    receipt["results"] = [row for row in receipt["results"] if row["seed"] in (1, 2)]
    _write(path, receipt)
    with pytest.raises(GateInvalid, match="coverage"):
        _compose_sleep(fixture, options)


def test_sleep_replacement_requires_a_completed_gate(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    options["repeat_task_gate"].unlink()
    with pytest.raises(GateNotReady) as error:
        _compose_sleep(fixture, options)
    assert error.value.code == "repeat_task_gate_not_finalized"


def test_incomplete_replacement_arguments_are_rejected(tmp_path):
    fixture = _fixture(tmp_path)
    with pytest.raises(GateInvalid, match="together"):
        _compose_sleep(fixture, {"repeat_task_root": tmp_path / "awake"})


def test_repeat_task_artifacts_cannot_still_overlap_incident(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    path = options["repeat_task_root"] / "t06_thermal_hold" / "ref_ok_s1" / "farm" / "recorder.jsonl"
    _write(path, {"t": 1100})
    with pytest.raises(GateInvalid, match="still overlaps"):
        _compose_sleep(fixture, options)


def test_repeat_task_source_bytes_must_be_unchanged(tmp_path):
    fixture, options = _sleep_fixture(tmp_path)
    path = options["repeat_task_root"] / "t06_thermal_hold" / "ref_ok_s1" / "submission" / "main.py"
    path.write_text("different reference")
    with pytest.raises(GateInvalid, match="submission"):
        _compose_sleep(fixture, options)
