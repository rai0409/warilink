import importlib.util
import json
import os
import signal
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
             'fail_remove': False, 'ready': True, 'child_rc': 0}

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'run':
            state['run'] = next(x.split('=', 1)[1] for x in args if x.startswith(module.RUN + '='))
            if state['fail_run']:
                raise subprocess.CalledProcessError(1, args)
            return state['id']
        if args[0] == 'inspect':
            if state['fail_run']:
                raise subprocess.CalledProcessError(1, args)
            return json.dumps([{'Id': state['inspect_id'], 'Config': {'Labels': {
                module.OWNER: 'monthly-restore' if state['label'] else 'other', module.RUN: state['run']}},
                'NetworkSettings': {'Ports': {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '43210'}]}}}])
        if args[0] == 'exec' and not state['ready']:
            raise subprocess.CalledProcessError(1, args)
        if args[0] == 'rm' and state['fail_remove']:
            raise subprocess.CalledProcessError(1, args)
        return ''

    class Child:
        def __init__(self, cmd, **kwargs):
            calls.append(tuple(cmd))
            self.cmd = cmd
            self.stopped = False

        def wait(self, timeout):
            self.stopped = True
            module.RECEIPT.write_text(json.dumps({'target_db': self.cmd[self.cmd.index('--target-db') + 1],
                                                   'result': 'PASS'}))
            return state['child_rc']

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def kill(self):
            self.stopped = True

    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.subprocess, 'Popen', Child)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    return module, state, calls


def test_success_only_removes_owned_id_and_hides_password(runner):
    module, state, calls = runner
    assert module.main() == 0
    assert [x for x in calls if x[0] == 'rm'] == [('rm', '-f', '-v', state['id'])]
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
        assert removed == [('rm', '-f', '-v', state['id'])]
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL'
    if failure == 'fail_run':
        assert receipt['container_cleanup'] == 'creation unconfirmed'
        assert receipt['retained_resources']['confirmation'] == 'unconfirmed'


@pytest.mark.parametrize('failure', ['error', 'timeout', 'invalid_output'])
def test_creation_ambiguous_recovers_only_verified_id(runner, monkeypatch, failure):
    module, state, calls = runner
    original = module.docker
    def docker(*args, **kwargs):
        out = original(*args, **kwargs)
        if args[0] == 'run':
            if failure == 'error':
                raise subprocess.CalledProcessError(1, args)
            if failure == 'timeout':
                raise subprocess.TimeoutExpired(args, 20)
            return 'truncated'
        return out
    monkeypatch.setattr(module, 'docker', docker)
    assert module.main() == 1
    assert [x for x in calls if x[0] == 'rm'] == [('rm', '-f', '-v', state['id'])]
    assert json.loads(module.RECEIPT.read_text())['result'] == 'FAIL'


@pytest.mark.parametrize('failure', ['owner', 'run_id', 'id_mismatch', 'inspect_error', 'remove_error'])
@pytest.mark.parametrize('creation_result', ['error', 'invalid_output'])
def test_creation_ambiguous_refuses_unverified_cleanup(runner, monkeypatch, failure, creation_result):
    module, state, calls = runner
    original = module.docker
    def docker(*args, **kwargs):
        if args[0] == 'run':
            original(*args, **kwargs)
            if creation_result == 'error':
                raise subprocess.CalledProcessError(1, args)
            return 'truncated'
        if args[0] == 'inspect' and failure == 'inspect_error':
            calls.append(args)
            raise subprocess.TimeoutExpired(args, 20)
        if args[0] == 'rm' and failure == 'remove_error':
            calls.append(args)
            raise subprocess.CalledProcessError(1, args)
        out = original(*args, **kwargs)
        if args[0] == 'inspect':
            item = json.loads(out)[0]
            if failure == 'owner':
                item['Config']['Labels'][module.OWNER] = 'unrelated'
            if failure == 'run_id':
                item['Config']['Labels'][module.RUN] = 'b' * 32
            if failure == 'id_mismatch' and args[1] == state['id']:
                item['Id'] = 'b' * 64
            return json.dumps([item])
        return out
    monkeypatch.setattr(module, 'docker', docker)
    assert module.main() == 1
    removed = [x for x in calls if x[0] == 'rm']
    assert removed == ([('rm', '-f', '-v', state['id'])] if failure == 'remove_error' else [])
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL'
    retained = receipt['retained_resources']
    assert retained['run_id'] == state['run'] and retained['container_name'] == 'warilink-drill-' + state['run']
    assert retained['confirmation'] == ('owned container verified' if failure == 'remove_error' else 'unconfirmed')
    assert retained['container_id'] == (None if failure == 'inspect_error' else state['id'])


@pytest.mark.parametrize('shape', ['missing_config', 'missing_labels', 'invalid_json'])
def test_creation_ambiguous_records_candidate_before_metadata_check(runner, monkeypatch, shape):
    module, state, calls = runner
    original = module.docker

    def docker(*args, **kwargs):
        if args[0] == 'run':
            original(*args, **kwargs)
            raise subprocess.CalledProcessError(1, args)
        if args[0] == 'inspect' and args[1].startswith('warilink-drill-'):
            raw = original(*args, **kwargs)
            if shape == 'invalid_json':
                return '{invalid'
            item = json.loads(raw)[0]
            if shape == 'missing_config':
                item.pop('Config')
            else:
                item['Config'].pop('Labels')
            return json.dumps([item])
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'docker', docker)
    assert module.main() == 1
    assert not [args for args in calls if args[0] == 'rm']
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL'
    assert receipt['creation_check'] == 'unconfirmed'
    retained = receipt['retained_resources']
    assert retained['run_id'] == state['run']
    assert retained['container_name'] == 'warilink-drill-' + state['run']
    assert retained['confirmation'] == 'unconfirmed'
    assert retained['container_id'] == (None if shape == 'invalid_json' else state['id'])


def test_timeout_reaps_child_before_removing_workdir(runner, monkeypatch, tmp_path):
    module, state, calls = runner
    untouched = tmp_path / 'unrelated'
    untouched.write_text('keep')
    events = []
    class TimedOutChild:
        def __init__(self, cmd, **kwargs):
            self.work = Path(cmd[cmd.index('--workdir') + 1])
            self.work.mkdir()
            (self.work / 'data').write_text('temporary')
            self.stopped = False
        def poll(self):
            return 0 if self.stopped else None
        def terminate(self):
            events.append('terminate')
            self.stopped = True
        def wait(self, timeout):
            events.append('wait')
            if timeout == 14000:
                raise subprocess.TimeoutExpired('restore', timeout)
            assert self.stopped
            return 1
    monkeypatch.setattr(module.subprocess, 'Popen', TimedOutChild)
    original = module.shutil.rmtree
    def remove(path):
        assert events == ['wait', 'terminate', 'wait']
        events.append('cleanup')
        original(path)
    monkeypatch.setattr(module.shutil, 'rmtree', remove)
    assert module.main() == 1
    assert events[-1] == 'cleanup' and untouched.read_text() == 'keep'
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL' and receipt['workdir_cleanup'] == 'removed owned workdir'
    assert not list(tmp_path.glob('drill-*'))


def test_timeout_kill_and_wait_permit_cleanup(runner, monkeypatch, tmp_path):
    module, state, calls = runner
    events = []
    class Child:
        def __init__(self, cmd, **kwargs):
            self.killed = False
        def poll(self):
            return None
        def terminate(self):
            events.append('terminate')
        def kill(self):
            events.append('kill')
            self.killed = True
        def wait(self, timeout):
            events.append('wait')
            if not self.killed:
                raise subprocess.TimeoutExpired('restore', timeout)
            return -9
    monkeypatch.setattr(module.subprocess, 'Popen', Child)
    assert module.main() == 1
    assert events == ['wait', 'terminate', 'wait', 'kill', 'wait']
    assert ('rm', '-f', '-v', state['id']) in calls
    assert not list(tmp_path.glob('drill-*'))
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL' and receipt['container_cleanup'] == 'removed owned container'


def test_child_already_exited_before_cleanup(runner, monkeypatch, tmp_path):
    module, state, calls = runner
    class Child:
        def __init__(self, cmd, **kwargs):
            pass
        def poll(self):
            return 1
        def wait(self, timeout):
            if timeout == 14000:
                raise subprocess.TimeoutExpired('restore', timeout)
            return 1
    monkeypatch.setattr(module.subprocess, 'Popen', Child)
    assert module.main() == 1
    assert ('rm', '-f', '-v', state['id']) in calls
    assert not list(tmp_path.glob('drill-*'))


def test_workdir_cleanup_failure_reports_failure(runner, monkeypatch):
    module, _, _ = runner
    def fail(path):
        raise OSError('cleanup failed')
    monkeypatch.setattr(module.shutil, 'rmtree', fail)
    assert module.main() == 1
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL' and receipt['workdir_cleanup'] == 'workdir cleanup failed'


@pytest.mark.parametrize('failure', ['terminate', 'kill', 'wait'])
def test_running_child_prevents_workdir_removal(runner, monkeypatch, tmp_path, failure):
    module, state, calls = runner
    removals = []
    monkeypatch.setattr(module.shutil, 'rmtree', lambda path: removals.append(path))
    class StuckChild:
        def __init__(self, cmd, **kwargs):
            pass
        def poll(self):
            return None
        def terminate(self):
            if failure == 'terminate':
                raise OSError('cannot terminate')
        def kill(self):
            if failure == 'kill':
                raise OSError('cannot kill')
        def wait(self, timeout):
            raise subprocess.TimeoutExpired('restore', timeout)
    monkeypatch.setattr(module.subprocess, 'Popen', StuckChild)
    assert module.main() == 1
    assert not [call for call in calls if call[0] == 'rm']
    assert not removals
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL'
    assert receipt['workdir_cleanup'] == 'deferred: child termination unconfirmed'
    assert receipt['container_cleanup'] == 'deferred: child termination unconfirmed'
    paths = list(tmp_path.glob('drill-*'))
    assert len(paths) == 1
    assert receipt['retained_resources'] == {'run_id': state['run'], 'container_id': state['id'],
                                             'workdir': str(paths[0])}


def test_nonzero_child_cleans_owned_workdir(runner, tmp_path):
    module, state, _ = runner
    state['child_rc'] = 1
    assert module.main() == 1
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL' and receipt['workdir_cleanup'] == 'removed owned workdir'
    assert not list(tmp_path.glob('drill-*'))


def test_signal_during_child_launch_reaps_before_cleanup(runner, monkeypatch, tmp_path):
    module, _, _ = runner
    events = []
    class Child:
        def __init__(self, cmd, **kwargs):
            events.append('launch')
            os.kill(os.getpid(), signal.SIGTERM)
        def poll(self):
            return None
        def terminate(self):
            events.append('terminate')
        def wait(self, timeout):
            events.append('wait')
            return 1
    monkeypatch.setattr(module.subprocess, 'Popen', Child)
    assert module.main() == 1
    assert events == ['launch', 'terminate', 'wait']
    assert not list(tmp_path.glob('drill-*'))
