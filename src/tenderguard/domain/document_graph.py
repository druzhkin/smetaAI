from __future__ import annotations

from datetime import date

from tenderguard.domain.enums import FindingCode, Severity
from tenderguard.domain.models import DomainModel, ValidationFinding


class DocumentGraphNode(DomainModel):
    document_id: str
    revision_id: str
    logical_key: str
    revision_label: str
    issue_date: date | None
    is_current: bool
    cancelled: bool
    supersedes_revision_id: str | None = None
    referenced_logical_keys: frozenset[str] = frozenset()
    project_code: str | None = None
    object_name: str | None = None


class DocumentGraphValidation(DomainModel):
    passed: bool
    findings: tuple[ValidationFinding, ...]


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def validate_document_graph(
    nodes: tuple[DocumentGraphNode, ...],
) -> DocumentGraphValidation:
    findings: list[ValidationFinding] = []
    current_by_key: dict[str, list[DocumentGraphNode]] = {}
    for node in nodes:
        if node.is_current and not node.cancelled:
            current_by_key.setdefault(node.logical_key, []).append(node)
    for logical_key, current in sorted(current_by_key.items()):
        if len(current) != 1:
            findings.append(
                ValidationFinding(
                    code=FindingCode.DOCUMENT_REVISION_AMBIGUOUS,
                    severity=Severity.BLOCKER,
                    message=f"Logical document has multiple current revisions: {logical_key}",
                    entity_ids=tuple(item.revision_id for item in current),
                )
            )

    available = set(current_by_key)
    for node in nodes:
        if not node.is_current or node.cancelled:
            continue
        missing = sorted(node.referenced_logical_keys - available)
        if missing:
            findings.append(
                ValidationFinding(
                    code=FindingCode.DOCUMENT_REFERENCE_MISSING,
                    severity=Severity.BLOCKER,
                    message="Current document references unavailable applications/documents",
                    entity_ids=(node.revision_id, *missing),
                )
            )

    by_revision = {node.revision_id: node for node in nodes}
    for start in by_revision:
        visited: set[str] = set()
        cursor: str | None = start
        while cursor is not None and cursor in by_revision:
            if cursor in visited:
                findings.append(
                    ValidationFinding(
                        code=FindingCode.DOCUMENT_GRAPH_CYCLE,
                        severity=Severity.BLOCKER,
                        message="Document supersession graph contains a cycle",
                        entity_ids=tuple(sorted(visited)),
                    )
                )
                break
            visited.add(cursor)
            cursor = by_revision[cursor].supersedes_revision_id

    current_nodes = [node for node in nodes if node.is_current and not node.cancelled]
    project_codes = {_normalized(node.project_code) for node in current_nodes if node.project_code}
    object_names = {_normalized(node.object_name) for node in current_nodes if node.object_name}
    if len(project_codes) > 1:
        findings.append(
            ValidationFinding(
                code=FindingCode.DOCUMENT_IDENTITY_MISMATCH,
                severity=Severity.BLOCKER,
                message="Current documents contain different project codes",
                entity_ids=tuple(sorted(project_codes)),
            )
        )
    if len(object_names) > 1:
        findings.append(
            ValidationFinding(
                code=FindingCode.DOCUMENT_IDENTITY_MISMATCH,
                severity=Severity.BLOCKER,
                message="Current documents contain different object names",
                entity_ids=tuple(sorted(object_names)),
            )
        )
    deduplicated = {(item.code, item.entity_ids): item for item in findings}
    result = tuple(deduplicated.values())
    return DocumentGraphValidation(
        passed=not any(item.severity is Severity.BLOCKER for item in result),
        findings=result,
    )
