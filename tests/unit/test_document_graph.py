from tenderguard.domain.document_graph import (
    DocumentGraphNode,
    validate_document_graph,
)


def test_document_graph_blocks_missing_references_and_identity_mismatch() -> None:
    result = validate_document_graph(
        (
            DocumentGraphNode(
                document_id="doc-1",
                revision_id="rev-1",
                logical_key="tender-spec",
                revision_label="1",
                issue_date=None,
                is_current=True,
                cancelled=False,
                referenced_logical_keys=frozenset({"appendix-a"}),
                project_code="PROJECT-A",
                object_name="Water line",
            ),
            DocumentGraphNode(
                document_id="doc-2",
                revision_id="rev-2",
                logical_key="drawings",
                revision_label="2",
                issue_date=None,
                is_current=True,
                cancelled=False,
                project_code="PROJECT-B",
                object_name="Water line",
            ),
        )
    )
    assert not result.passed
    codes = {item.code.value for item in result.findings}
    assert "DOCUMENT_REFERENCE_MISSING" in codes
    assert "DOCUMENT_IDENTITY_MISMATCH" in codes


def test_document_graph_detects_supersession_cycle() -> None:
    result = validate_document_graph(
        (
            DocumentGraphNode(
                document_id="doc-1",
                revision_id="rev-1",
                logical_key="spec",
                revision_label="1",
                issue_date=None,
                is_current=False,
                cancelled=False,
                supersedes_revision_id="rev-2",
            ),
            DocumentGraphNode(
                document_id="doc-1",
                revision_id="rev-2",
                logical_key="spec",
                revision_label="2",
                issue_date=None,
                is_current=True,
                cancelled=False,
                supersedes_revision_id="rev-1",
            ),
        )
    )
    assert any(item.code.value == "DOCUMENT_GRAPH_CYCLE" for item in result.findings)
