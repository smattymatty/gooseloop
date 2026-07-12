"""Tests for gooseloop.artifact: the versioned artifact contract (PROTOCOL §12).

The semantics mirror the review protocol_version check: same major compatible,
different major refused loudly, missing version kept-and-named (fail-safe in
the KEEP direction, so hand-sealed pre-versioning artifacts keep working).
"""

import pytest

from gooseloop.artifact import ArtifactVersionError, check_artifact_version


def test_matching_version_is_clean():
    assert check_artifact_version({"schema_version": "1.0"}, "1.0") == []


def test_minor_drift_is_compatible_both_ways():
    assert check_artifact_version({"schema_version": "1.7"}, "1.0") == []
    assert check_artifact_version({"schema_version": "1.0"}, "1.7") == []


def test_missing_version_reads_with_named_problem():
    problems = check_artifact_version({}, "1.0", what="pain corpus")
    assert len(problems) == 1
    assert "pain corpus" in problems[0]
    assert 'schema_version = "1.0"' in problems[0]


def test_empty_version_treated_as_missing():
    problems = check_artifact_version({"schema_version": "  "}, "1.0")
    assert len(problems) == 1 and "assuming 1.0" in problems[0]


def test_major_mismatch_refused_loudly():
    with pytest.raises(ArtifactVersionError) as e:
        check_artifact_version({"schema_version": "2.0"}, "1.0", what="pain corpus")
    msg = str(e.value)
    assert "2.0" in msg and "major 1" in msg and "pain corpus" in msg


def test_unparseable_version_refused_loudly():
    with pytest.raises(ArtifactVersionError) as e:
        check_artifact_version({"schema_version": "banana"}, "1.0")
    assert "banana" in str(e.value) and "not parseable" in str(e.value)


def test_toml_numeric_version_values_work():
    # TOML allows `schema_version = 1.0` (float) or `= 1` (int); both coerce.
    assert check_artifact_version({"schema_version": 1.0}, "1.0") == []
    assert check_artifact_version({"schema_version": 1}, "1.5") == []
    with pytest.raises(ArtifactVersionError):
        check_artifact_version({"schema_version": 2}, "1.0")


def test_custom_key_and_default_what():
    problems = check_artifact_version({}, "1.0", key="corpus_version")
    assert "corpus_version" in problems[0] and "artifact" in problems[0]


def test_bad_supported_version_is_a_programmer_error():
    with pytest.raises(ArtifactVersionError):
        check_artifact_version({"schema_version": "1.0"}, "nope")
