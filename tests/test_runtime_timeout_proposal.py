from pathlib import Path
from copy import deepcopy
import pytest
from scripts import validate_successfactors_ten_activation as activation


def workflow():
    return (activation.ROOT / activation.TIMEOUT_WORKFLOW).read_bytes()


def test_only_job_timeout_changes_and_exact_preexisting_lock_still_applies():
    proposed = workflow()
    assert proposed.count(b'timeout-minutes: 60') == 1
    original = proposed.replace(b'timeout-minutes: 60', b'timeout-minutes: 45')
    assert activation.digest(original) == activation.TIMEOUT_BEFORE_SHA256
    assert activation.digest(proposed) == activation.TIMEOUT_60_SHA256
    assert activation._artifact_matches(activation.TIMEOUT_WORKFLOW, proposed, activation.TIMEOUT_BEFORE_SHA256)
    assert b'17 */6 * * *' in proposed
    assert b'cancel-in-progress: false' in proposed
    assert b'group: job-radar-hourly' in proposed


@pytest.mark.parametrize('old,new', [(b'60', b'75'), (b'60', b'90'), (b'*/6', b'*/1'),
                                    (b'cancel-in-progress: false', b'cancel-in-progress: true'),
                                    (b'contents: read', b'contents: write'),
                                    (b'ubuntu-latest', b'windows-latest')])
def test_operational_timeout_exception_does_not_authorize_other_drift(old, new):
    assert not activation._artifact_matches(activation.TIMEOUT_WORKFLOW, workflow().replace(old,new), activation.TIMEOUT_BEFORE_SHA256)


def test_timeout_exception_cannot_be_used_for_source_config_or_other_expected_hash():
    assert not activation._artifact_matches('config/target_companies.yaml', workflow(), activation.TIMEOUT_BEFORE_SHA256)
    assert not activation._artifact_matches(activation.TIMEOUT_WORKFLOW, workflow(), '0'*64)


def test_all533_exact_targets_ten_certificates_and_five_baseline_behaviors_still_validate():
    result = activation.validate()
    assert result['effective_sources'] == 533
    assert result['certificates_applicable'] == 10
    assert result['unchanged_companies'] == 523
    assert result['retained_jobs'] == 520
    assert result['production_activation_executed'] is False
