from copy import deepcopy
import pytest
import yaml

from scripts import validate_smartrecruiters_recovery as activation


def test_effective_targets_and_exact_rollback():
    assert activation.validate() == {'ServiceNow': 1005, 'Intuitive Surgical': 858, 'Kioxia': 33}
    path = activation.ROOT / 'config/target_companies.yaml'
    assert activation.registry_before_bounds(yaml.safe_load(path.read_bytes())) == yaml.safe_load(
        activation.predecessor_bytes('config/target_companies.yaml', path.read_bytes()))


def test_unreviewed_tenant_scope_or_bound_is_rejected():
    rows = yaml.safe_load((activation.ROOT / 'config/target_companies.yaml').read_bytes())
    for field, value in [('source_scope', 'us'), ('slug','Other'), ('max_jobs',5000)]:
        changed = deepcopy(rows)
        next(r for r in changed['target_companies'] if r['name']=='ServiceNow')[field] = value
        with pytest.raises(ValueError):
            activation.registry_before_bounds(changed)
    raw = (activation.ROOT / activation.OVERLAY).read_bytes()
    with pytest.raises(ValueError):
        activation.predecessor_bytes(activation.OVERLAY, raw+b'\n')
