from __future__ import annotations

from tenderguard.domain.common import content_hash
from tenderguard.domain.enums import Severity
from tenderguard.domain.models import DomainModel, ScopeFinding


class ScopeRule(DomainModel):
    rule_id: str
    rule_pack_version_id: str
    trigger_any_work_codes: frozenset[str] = frozenset()
    trigger_all_work_codes: frozenset[str] = frozenset()
    required_work_codes: frozenset[str]
    required_project_tags: frozenset[str] = frozenset()
    severity: Severity = Severity.BLOCKER
    rationale: str


class ScopeEvaluation(DomainModel):
    rule_pack_version_id: str
    evaluated_work_codes: frozenset[str]
    findings: tuple[ScopeFinding, ...]


def evaluate_scope(
    *,
    wbs_node_id: str,
    present_work_codes: frozenset[str],
    project_tags: frozenset[str],
    rules: tuple[ScopeRule, ...],
) -> ScopeEvaluation:
    findings: list[ScopeFinding] = []
    versions = {rule.rule_pack_version_id for rule in rules}
    if len(versions) != 1:
        raise ValueError("Scope evaluation requires exactly one rule-pack version")
    for rule in rules:
        any_triggered = not rule.trigger_any_work_codes or bool(
            rule.trigger_any_work_codes & present_work_codes
        )
        all_triggered = rule.trigger_all_work_codes <= present_work_codes
        tags_apply = rule.required_project_tags <= project_tags
        if not (any_triggered and all_triggered and tags_apply):
            continue
        for missing_code in sorted(rule.required_work_codes - present_work_codes):
            identity = {
                "rule_id": rule.rule_id,
                "wbs_node_id": wbs_node_id,
                "missing_code": missing_code,
            }
            findings.append(
                ScopeFinding(
                    finding_id=f"scope-{content_hash(identity)[:24]}",
                    rule_id=rule.rule_id,
                    wbs_node_id=wbs_node_id,
                    required_work_code=missing_code,
                    severity=rule.severity,
                    reason=rule.rationale,
                )
            )
    return ScopeEvaluation(
        rule_pack_version_id=next(iter(versions)),
        evaluated_work_codes=present_work_codes,
        findings=tuple(findings),
    )


def pipeline_companion_rules(rule_pack_version_id: str) -> tuple[ScopeRule, ...]:
    """Candidate rule pack; it must be methodology-approved before bid release."""

    companion_codes = frozenset(
        {
            "EARTHWORK_EXCAVATION",
            "TRENCH_SHORING",
            "DEWATERING",
            "BEDDING",
            "MATERIAL_DELIVERY",
            "PIPE_INSTALLATION",
            "PIPE_JOINTING",
            "TESTING",
            "FLUSHING",
            "DISINFECTION",
            "BACKFILL",
            "COMPACTION",
            "SURPLUS_SOIL_DISPOSAL",
            "SURFACE_REINSTATEMENT",
            "AS_BUILT_DOCUMENTATION",
        }
    )
    return (
        ScopeRule(
            rule_id="pipeline-companion-work-completeness",
            rule_pack_version_id=rule_pack_version_id,
            trigger_any_work_codes=frozenset({"PIPE_INSTALLATION", "PIPELINE_CONSTRUCTION"}),
            required_work_codes=companion_codes,
            severity=Severity.BLOCKER,
            rationale=(
                "Pipeline work requires each companion activity to be present or "
                "explicitly resolved as not applicable with evidence"
            ),
        ),
    )
