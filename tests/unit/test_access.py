import pytest

from tenderguard.domain.access import (
    PROJECT_ROLE_MASK_MAX,
    project_role_bit,
    project_role_mask,
    validate_project_role_evidence,
)
from tenderguard.domain.enums import ActorRole


def test_project_role_mask_is_stable_and_excludes_system() -> None:
    assert project_role_bit(ActorRole.ESTIMATOR) == 1
    assert project_role_bit(ActorRole.REVIEWER) == 8
    assert project_role_bit(ActorRole.ADMIN) == 256
    assert PROJECT_ROLE_MASK_MAX == 511
    assert project_role_mask((ActorRole.ESTIMATOR, ActorRole.REVIEWER)) == 9
    with pytest.raises(ValueError, match="not a project membership role"):
        project_role_bit(ActorRole.SYSTEM)


@pytest.mark.parametrize(
    ("roles", "mask"),
    [
        ([], 0),
        (["ESTIMATOR", "ESTIMATOR"], 1),
        (["SYSTEM"], 1),
        (["UNKNOWN"], 1),
        (["ESTIMATOR"], 8),
        ({"ESTIMATOR": True}, 1),
        (["ESTIMATOR"], True),
    ],
)
def test_project_role_evidence_rejects_malformed_or_divergent_values(
    roles: object,
    mask: object,
) -> None:
    with pytest.raises(ValueError):
        validate_project_role_evidence(roles, mask)
