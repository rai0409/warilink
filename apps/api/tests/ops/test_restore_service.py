import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / 'scripts/warilink_restore_service.py'


@pytest.fixture
def runner(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('restore_service_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'RECEIPT', tmp_path / 'receipt.json')
    monkeypatch.setenv('WARILINK_DRILL_DB_PASSWORD', 'secret-for-test')
    calls = []
    state = {'id': 'a' * 64, 'inspect_id': 'a' * 64, 'label': True, 'fail_run': False,
             'fail_remove': False, 'ready': True}

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'run':
            state['run'] = next(x.split('=', 1)[1] for x in args if x.startswith(module.RUN + '='))
            if state['fail_run']:
                raise subprocess.CalledProcessError(1, args)
            return state['id']
        if args[0] == 'inspect':
            return json.dumps([{'Id': state['inspect_id'], 'Config': {'Labels': {
                module.OWNER: 'monthly-restore' if state['label'] else 'other', module.RUN: state['run']}},
                'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '43210'}]}}}])
        if args[0] == 'exec' and not state['ready']:
            raise subprocess.CalledProcessError(1, args)
        if args[0] == 'rm' and state['fail_remove']:
            raise subprocess.CalledProcessError(1, args)
        return ''

    def run(cmd, **kwargs):
        calls.append(tuple(cmd))
        module.RECEIPT.write_text(json.dumps({'target_db': cmd[cmd.index('--target-db') + 1], 'result': 'PASS'}))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    return module, state, calls


def test_success_only_removes_owned_id_and_hides_password(runner):
    module, state, calls = runner
    assert module.main() == 0
    assert [x for x in calls if x[0] == 'rm'] == [('rm', '-f', state['id'])]
    assert '127.0.0.1::5432' in calls[0] and 'POSTGRES_PASSWORD' in calls[0]
    assert 'secret-for-test' not in str(calls)
    child = next(c for c in calls if '--execute' in c)
    assert child[child.index('--port') + 1] == '43210'
    assert 'secret-for-test' not in module.RECEIPT.read_text()


@pytest.mark.parametrize('failure', ['fail_run', 'id', 'label', 'fail_remove', 'ready'])
def test_failure_does_not_delete_unowned_or_report_success(runner, failure):
    module, state, calls = runner
    if failure == 'id':
        state['inspect_id'] = 'b' * 64
    elif failure in ('label', 'ready'):
        state[failure] = False
    else:
        state[failure] = True
    assert module.main() == 1
    removed = [x for x in calls if x[0] == 'rm']
    if failure in ('fail_run', 'id', 'label'):
        assert removed == []
    else:
        assert removed == [('rm', '-f', state['id'])]
    assert json.loads(module.RECEIPT.read_text())['result'] == 'FAIL'
