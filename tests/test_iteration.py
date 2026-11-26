import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline.orchestration.iteration import add_preflight, score_summary, select_tasks


class IterationControlTests(unittest.TestCase):
    def test_selection_is_reproducible_and_exactly_ten(self):
        with tempfile.TemporaryDirectory() as temp:
            pool = Path(temp) / "pool.jsonl"
            pool.write_text("".join(json.dumps({"sample_id": f"S{i:03d}"}) + "\n" for i in range(1, 101)))
            a = select_tasks(pool, seed=17)
            b = select_tasks(pool, seed=17)
            self.assertEqual(a["selected_task_ids"], b["selected_task_ids"])
            self.assertEqual(len(a["selected_task_ids"]), 10)
            self.assertEqual(len(set(a["selected_task_ids"])), 10)

    def test_preflight_keeps_missing_data_visible(self):
        result = add_preflight({"tasks": [{"task_id": "S001", "data_urls": ["https://example.test/data"]},
                                           {"task_id": "S002"}]})
        self.assertEqual(result["preflight"]["downloadable"], 1)
        self.assertEqual(result["preflight"]["blocked"], 1)

    def test_preflight_recovers_from_stale_historical_path(self):
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "candidate"
            candidate.mkdir()
            (candidate / "instruction.md").write_text("task")
            (candidate / "task.toml").write_text("task = {}")
            result = add_preflight({"tasks": [{
                "task_id": "S003",
                "task_path": str(Path(temp) / "missing-history"),
                "task_definition_candidates": [{"path": str(candidate)}],
            }]})
            self.assertEqual(result["tasks"][0]["readiness_status"], "partial")
            self.assertEqual(result["tasks"][0]["readiness_evidence"]["candidate_paths_considered"], [str(candidate.resolve())])

    def test_score_summary_does_not_turn_unavailable_into_zero(self):
        result = score_summary([{"task_id": "S001", "scientific_score": 100, "is_valid": True, "status": "complete"},
                                {"task_id": "S002", "status": "data_unavailable"}])
        self.assertEqual(result["tasks_scoreable"], 1)
        self.assertEqual(result["mean_score_scoreable"], 100)
        self.assertFalse(result["passed"])

    def test_quality_and_scientific_namespaces_cannot_be_mixed(self):
        quality = score_summary([{"sample_id": "S001", "q0": 100}], score_kind="task_quality")
        scientific = score_summary([{"sample_id": "S001", "q0": 100}], score_kind="scientific")
        self.assertEqual(quality["mean_score_scoreable"], 100)
        self.assertEqual(scientific["tasks_scoreable"], 0)

    def test_nine_high_scores_cannot_pass_a_ten_task_iteration(self):
        rows = [{"task_id": f"S{i:03d}", "q0": 100} for i in range(10)]
        self.assertTrue(score_summary(rows, score_kind="task_quality")["passed"])
        partial = score_summary(rows[:9], score_kind="task_quality")
        self.assertEqual(partial["mean_score_scoreable"], 100)
        self.assertFalse(partial["passed"])
        self.assertEqual(partial["coverage"], 0.9)
        self.assertEqual(partial["tasks_blocked_or_unscored"], 1)

    def test_replaced_or_duplicated_tasks_cannot_pass(self):
        selected = [f"S{i:03d}" for i in range(10)]
        rows = [{"task_id": tid, "d0": 95} for tid in selected]
        self.assertTrue(score_summary(rows, score_kind="difficulty", expected_task_ids=selected)["passed"])
        rows[-1]["task_id"] = "replacement"
        self.assertFalse(score_summary(rows, score_kind="difficulty", expected_task_ids=selected)["passed"])
        rows[-1]["task_id"] = selected[0]
        self.assertFalse(score_summary(rows, score_kind="difficulty")["passed"])

    def test_invalid_scores_and_partial_trajectory_do_not_pass(self):
        for value in (float('nan'), float('inf'), 101, -1, True):
            result = score_summary([{"task_id": "S001", "q0": value}], score_kind="task_quality")
            self.assertEqual(result["tasks_scoreable"], 0)
        rows = [{"task_id": f"S{i:03d}", "t0": {"score": 100, "coverage": 0.4}} for i in range(10)]
        self.assertFalse(score_summary(rows, score_kind="trajectory")["passed"])

    def test_main_dispatcher_writes_selection_and_preflight(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            pool = Path(temp) / "pool.jsonl"
            pool.write_text("".join(json.dumps({"sample_id": f"S{i:03d}"}) + "\n" for i in range(1, 101)))
            output = Path(temp) / "iteration"
            result = subprocess.run([str(root / "bin/run_pipeline.sh"), "iteration", "--pool", str(pool),
                                     "--output", str(output), "--seed", "9"],
                                    text=True, capture_output=True, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["selected_task_ids"]), 10)
            self.assertTrue((output / "selection.json").is_file())
            self.assertTrue((output / "preflight.json").is_file())


if __name__ == "__main__":
    unittest.main()
