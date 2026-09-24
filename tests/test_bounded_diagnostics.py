from src.scraper.company_careers.diagnostics import (
    MAX_SAMPLE_LENGTH,
    bounded_scalar_sample,
    structure_diagnostic,
)


def test_bounded_scalar_sample_escapes_and_truncates():
    value = "line one\nline two\t" + ("z" * 200)
    sample = bounded_scalar_sample(value)

    assert len(sample) <= MAX_SAMPLE_LENGTH
    assert r"\n" in sample
    assert r"\t" in sample
    assert value not in sample


def test_structure_diagnostic_is_deterministic_and_omits_description_values():
    value = [{
        "description": "do not leak " + ("x" * 200),
        "address": {"country": "Canada", "city": "Calgary"},
        "locationName": "Calgary\nAlberta",
    }]

    first = structure_diagnostic(value)
    second = structure_diagnostic(value)

    assert first == second
    assert "fields=[address,description,locationName]" in first
    assert "address(city,country)" in first
    assert r"Calgary\nAlberta" in first
    assert "do not leak" not in first
    assert "x" * 80 not in first


def test_structure_diagnostic_includes_bounded_second_level_shape_and_scalars():
    value = [{
        "address": {
            "countrySubdivisionLevel1": {
                "displayValue": "British Columbia",
                "opaqueId": "550e8400-e29b-41d4-a716-446655440000",
            }
        }
    }]

    diagnostic = structure_diagnostic(value)

    assert "address.countrySubdivisionLevel1(displayValue,opaqueId)" in diagnostic
    assert "address.countrySubdivisionLevel1.displayValue=str" in diagnostic
    assert len(diagnostic) < 1000
