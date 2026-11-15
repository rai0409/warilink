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
    assert json.loads(module.RECEIPT.read_text())['result'] == 'FAIL'


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


def test_workdir_cleanup_failure_reports_failure(runner, monkeypatch):
    module, _, _ = runner
    def fail(path):
        raise OSError('cleanup failed')
    monkeypatch.setattr(module.shutil, 'rmtree', fail)
    assert module.main() == 1
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL' and receipt['workdir_cleanup'] == 'workdir cleanup failed'


def test_running_child_prevents_workdir_removal(runner, monkeypatch, tmp_path):
    module, _, _ = runner
    class StuckChild:
        def __init__(self, cmd, **kwargs):
            pass
        def poll(self):
            return None
        def terminate(self):
            raise OSError('cannot terminate')
        def wait(self, timeout):
            raise subprocess.TimeoutExpired('restore', timeout)
    monkeypatch.setattr(module.subprocess, 'Popen', StuckChild)
    assert module.main() == 1
    receipt = json.loads(module.RECEIPT.read_text())
    assert receipt['result'] == 'FAIL' and receipt['workdir_cleanup'] == 'workdir cleanup failed'
    assert len(list(tmp_path.glob('drill-*'))) == 1


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
