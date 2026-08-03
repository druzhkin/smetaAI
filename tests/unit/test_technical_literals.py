# ruff: noqa: RUF001

import pytest

from tenderguard.domain.technical_literals import (
    TechnicalLiteralComparison,
    compare_technical_literals,
    extract_technical_literals,
)


def test_literal_extraction_preserves_raw_evidence_and_normalizes_only_syntax() -> None:
    value = (
        "Кабель АПвПг 1х240/70; муфта 1ПСТ-10-150/240(Б); "
        "размер 220×840 мм, толщина 0,08 мм, I класс, ЛСЭ 350"
    )

    literals = extract_technical_literals(value)
    identities = {item.identity for item in literals}

    assert "DESIGNATION:апвпг1x240/70" in identities
    assert "DESIGNATION:1пст-10-150/240(б)" in identities
    assert "DIMENSION:220x840мм" in identities
    assert "MEASUREMENT:0.08мм" in identities
    assert "CLASS_MARKER:iкласс" in identities
    assert "DESIGNATION:лсэ350" in identities
    dimension = next(item for item in literals if item.identity == "DIMENSION:220x840мм")
    assert value[dimension.start_offset : dimension.end_offset] == dimension.raw_literal


def test_comparison_reports_literal_overlap_without_claiming_equivalence() -> None:
    comparison = compare_technical_literals(
        boq_text="Кабель АПвПг 1х240/70",
        source_text="Кабель АПвПг 1x240/70",
    )

    assert comparison.name_relation == "EXACT_LITERAL_NAME"
    assert "DESIGNATION:апвпг1x240/70" in comparison.matched_literal_identities
    assert not comparison.establishes_technical_equivalence


def test_comparison_exposes_missing_and_variant_literals() -> None:
    comparison = compare_technical_literals(
        boq_text=(
            "Лента «Осторожно! Оптический кабель», ширина 70 мм, "
            "толщина 0,08 мм (ЛСЭ 350)"
        ),
        source_text=(
            "Лента \"Осторожно! Оптический кабель\" 70 мм х 500 м REXANT 19-3021"
        ),
    )

    assert comparison.name_relation == "PARTIAL_LITERAL_OVERLAP"
    assert "MEASUREMENT:70мм" in comparison.matched_literal_identities
    assert "MEASUREMENT:0.08мм" in comparison.boq_only_literal_identities
    assert "DESIGNATION:лсэ350" in comparison.boq_only_literal_identities
    assert "MEASUREMENT:500м" in comparison.source_only_literal_identities
    assert "DESIGNATION:rexant19-3021" in comparison.source_only_literal_identities


def test_cyrillic_x_is_normalized_only_inside_numeric_dimensions() -> None:
    literals = extract_technical_literals('Табличка "Охранная зона" 220х840 мм')

    identities = {item.identity for item in literals}
    assert "QUOTED_PHRASE:охранная зона" in identities
    assert "DIMENSION:220x840мм" in identities


def test_comparison_rejects_forged_literals_or_equivalence_claim() -> None:
    comparison = compare_technical_literals(boq_text="Труба d160", source_text="Труба d110")
    payload = comparison.model_dump(mode="python")
    payload["matched_literal_identities"] = ("DESIGNATION:d160",)
    with pytest.raises(ValueError, match="does not reproduce"):
        TechnicalLiteralComparison.model_validate(payload)

    with pytest.raises(ValueError, match="cannot establish"):
        TechnicalLiteralComparison.model_validate(
            comparison.model_copy(
                update={"establishes_technical_equivalence": True}
            ).model_dump(mode="python")
        )
