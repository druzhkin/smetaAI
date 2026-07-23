from tenderguard.domain.scope import evaluate_scope, pipeline_companion_rules


def test_pipeline_presence_does_not_imply_companion_work_is_unnecessary() -> None:
    rules = pipeline_companion_rules("scope-rules-draft-v1")
    evaluation = evaluate_scope(
        wbs_node_id="wbs-pipeline-1",
        present_work_codes=frozenset({"PIPE_INSTALLATION", "TESTING"}),
        project_tags=frozenset({"water"}),
        rules=rules,
    )
    missing = {finding.required_work_code for finding in evaluation.findings}
    assert "EARTHWORK_EXCAVATION" in missing
    assert "BACKFILL" in missing
    assert "TESTING" not in missing
    assert "AS_BUILT_DOCUMENTATION" in missing
