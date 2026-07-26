import pytest

from tenderguard.application.boq import (
    BoqLineDraft,
    CostComponentDraft,
    ManualChangePolicy,
    ManualChangePolicyRule,
)
from tenderguard.domain.enums import ActorRole, CostBasisKind, CostCategory


def test_cost_component_semantic_key_fits_downstream_pricing_identity() -> None:
    with pytest.raises(ValueError, match="at most 128"):
        CostComponentDraft(
            semantic_key="x" * 129,
            category=CostCategory.MATERIAL,
            basis_kind=CostBasisKind.MARKET,
        )
    with pytest.raises(ValueError, match="must be normalized"):
        CostComponentDraft(
            semantic_key=" pipe-material ",
            category=CostCategory.MATERIAL,
            basis_kind=CostBasisKind.MARKET,
        )
    with pytest.raises(ValueError, match="factor IDs must be unique"):
        CostComponentDraft(
            semantic_key="pipe-material",
            category=CostCategory.MATERIAL,
            basis_kind=CostBasisKind.MARKET,
            factor_ids=("regional-factor", "regional-factor"),
        )
    with pytest.raises(ValueError, match="factor ID is invalid"):
        CostComponentDraft(
            semantic_key="pipe-material",
            category=CostCategory.MATERIAL,
            basis_kind=CostBasisKind.MARKET,
            factor_ids=(" invalid-factor ",),
        )


def test_boq_line_rejects_ambiguous_evidence_and_non_normalized_identity() -> None:
    component = CostComponentDraft(
        semantic_key="pipe-material",
        category=CostCategory.MATERIAL,
        basis_kind=CostBasisKind.MARKET,
    )
    with pytest.raises(ValueError, match="observation IDs must be unique"):
        BoqLineDraft(
            line_key="pipeline-main",
            wbs_node_id="wbs-pipeline",
            work_code="PIPE_INSTALLATION",
            description="Install pipeline",
            unit="m",
            evidence_observation_ids=("observation-1", "observation-1"),
            cost_components=(component,),
        )
    with pytest.raises(ValueError, match="text fields must be normalized"):
        BoqLineDraft(
            line_key=" pipeline-main ",
            wbs_node_id="wbs-pipeline",
            work_code="PIPE_INSTALLATION",
            description="Install pipeline",
            unit="m",
            evidence_observation_ids=("observation-1",),
            cost_components=(component,),
        )
    with pytest.raises(ValueError, match="semantic keys must be unique"):
        BoqLineDraft(
            line_key="pipeline-main",
            wbs_node_id="wbs-pipeline",
            work_code="PIPE_INSTALLATION",
            description="Install pipeline",
            unit="m",
            evidence_observation_ids=("observation-1",),
            cost_components=(component, component),
        )


def test_manual_change_policy_rejects_ambiguous_approval_ownership() -> None:
    with pytest.raises(ValueError, match="require an assigned role"):
        ManualChangePolicyRule(
            entity_type="quantity",
            field_name="value",
            critical=True,
        )
    with pytest.raises(ValueError, match="cannot assign an approval role"):
        ManualChangePolicyRule(
            entity_type="quantity",
            field_name="value",
            critical=False,
            assigned_role=ActorRole.REVIEWER,
        )
    with pytest.raises(ValueError, match="SYSTEM cannot approve"):
        ManualChangePolicyRule(
            entity_type="quantity",
            field_name="value",
            critical=True,
            assigned_role=ActorRole.SYSTEM,
        )
    rule = ManualChangePolicyRule(
        entity_type="quantity",
        field_name="value",
        critical=True,
        assigned_role=ActorRole.REVIEWER,
    )
    with pytest.raises(ValueError, match="duplicate target rules"):
        ManualChangePolicy(rules=(rule, rule))
