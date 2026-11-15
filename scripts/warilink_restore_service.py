#!/usr/bin/env python3
"""Run the monthly restore in a container owned by this invocation."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OWNER = "org.warilink.restore.owner"
RUN = "org.warilink.restore.run"
RECEIPT = Path('/var/lib/warilink/restore-receipt.json')


def docker(*args, env=None, timeout=20):
    return subprocess.run(['docker', *args], capture_output=True, text=True, env=env,
                          timeout=timeout, check=True).stdout.strip()


def inspect_owned(container_id, run_id):
    data = json.loads(docker('inspect', container_id))
    item = data[0]
    labels = item['Config']['Labels'] or {}
    if item['Id'] == container_id and labels.get(OWNER) == 'monthly-restore' and labels.get(RUN) == run_id:
        return item
    return None


def resolve_owned(name, expected_id=None, expected_run=None):
    item = json.loads(docker('inspect', name))[0]
    container_id = item['Id']
    labels = item['Config']['Labels'] or {}
    run_id = labels.get(RUN, '')
    if (not isinstance(container_id, str) or not re.fullmatch(r'[0-9a-f]{64}', container_id)
            or not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id)
            or labels.get(OWNER) != 'monthly-restore'
            or (expected_id is not None and expected_id != container_id)
            or (expected_run is not None and expected_run != run_id)
            or inspect_owned(container_id, run_id) is None):
        raise ValueError('container ownership mismatch')
    return container_id, run_id


def main():
    run_id = uuid.uuid4().hex
    name = f'warilink-drill-{run_id}'
    target = f'warilink_restore_{run_id}'
    container_id = None
    candidate_id = None
    creation_uncertain = False
    creation_check = None
    result = 1
    cleanup = 'not created'
    work_cleanup = 'not created'
    work_root = None
    work_identity = None
    child = None
    child_stopped = True
    launching = False
    interrupted = False
    def terminate(signum, frame):
        nonlocal interrupted
        if launching:
            interrupted = True
            return
        raise InterruptedError('terminated')
    previous_term = signal.signal(signal.SIGTERM, terminate)
    previous_int = signal.signal(signal.SIGINT, terminate)
    try:
        password = os.environ['WARILINK_DRILL_DB_PASSWORD']
        if not password:
            raise ValueError('missing password')
        work_root = Path(tempfile.mkdtemp(prefix=f'drill-{run_id}-', dir=RECEIPT.parent))
        info = work_root.lstat()
        work_identity = (info.st_dev, info.st_ino)
        env = dict(os.environ, POSTGRES_PASSWORD=password)
        creation_uncertain = True
        output = docker('run', '-d', '--name', name, '--label', f'{OWNER}=monthly-restore', '--label',
                        f'{RUN}={run_id}', '-e', 'POSTGRES_USER=warilink', '-e', 'POSTGRES_PASSWORD',
                        '-e', 'POSTGRES_DB=postgres', '-p', '127.0.0.1::5432', 'postgres:18-alpine', env=env)
        if not isinstance(output, str) or not re.fullmatch(r'[0-9a-f]{64}', output):
            raise ValueError('invalid container id')
        container_id = output
        creation_uncertain = False
        item = inspect_owned(container_id, run_id)
        if item is None:
            raise ValueError('container ownership mismatch')
        ports = item['NetworkSettings']['Ports']['5432/tcp']
        if len(ports) != 1 or ports[0]['HostIp'] != '127.0.0.1':
            raise ValueError('invalid port binding')
        port = int(ports[0]['HostPort'])
        if not 1 <= port <= 65535:
            raise ValueError('invalid port')
        for _ in range(60):
            try:
                docker('exec', container_id, 'pg_isready', '-U', 'warilink', '-q', timeout=2)
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            raise TimeoutError('postgres readiness timeout')
        child_env = dict(os.environ, WARILINK_RESTORE_DB_PASSWORD=password,
                         WARILINK_RESTORE_CONTAINER_ID=container_id, WARILINK_RESTORE_RUN_ID=run_id)
        cmd = [sys.executable, str(ROOT / 'warilink_restore_verify.py'), '--execute', '--from-report',
               '/var/lib/warilink/last-backup.json', '--container', name, '--port', str(port), '--user', 'warilink',
               '--target-db', target, '--workdir', str(work_root / 'work'),
               '--receipt-out', str(RECEIPT)]
        launching = True
        try:
            child = subprocess.Popen(cmd, env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child_stopped = False
        finally:
            launching = False
        if interrupted:
            raise InterruptedError('terminated during child launch')
        result = child.wait(timeout=14000)
        child_stopped = True
        if result == 0:
            receipt = json.loads(RECEIPT.read_text())
            if receipt.get('target_db') != target or receipt.get('result') != 'PASS':
                result = 1
    except (Exception, KeyboardInterrupt):
        result = 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if child is not None and not child_stopped:
            try:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
                child_stopped = True
            except Exception:
                result = 1
        if creation_uncertain and child_stopped:
            creation_check = 'unconfirmed'
            cleanup = 'creation unconfirmed'
            try:
                item = json.loads(docker('inspect', name))[0]
                found = item['Id']
                if isinstance(found, str) and re.fullmatch(r'[0-9a-f]{64}', found):
                    candidate_id = found
                labels = item['Config']['Labels'] or {}
                if (candidate_id is None or labels.get(OWNER) != 'monthly-restore' or labels.get(RUN) != run_id
                        or inspect_owned(candidate_id, run_id) is None):
                    raise ValueError('container ownership mismatch')
                container_id = candidate_id
                creation_check = 'owned container verified'
            except Exception:
                result = 1
        if not child_stopped:
            result = 1
            if work_root is not None:
                work_cleanup = 'deferred: child termination unconfirmed'
            if container_id:
                cleanup = 'deferred: child termination unconfirmed'
        elif work_root is not None:
            try:
                info = work_root.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or (info.st_dev, info.st_ino) != work_identity):
                    raise RuntimeError('workdir identity or child state unknown')
                shutil.rmtree(work_root)
                work_cleanup = 'removed owned workdir'
            except Exception:
                work_cleanup = 'workdir cleanup failed'
                result = 1
        if container_id and child_stopped:
            try:
                if inspect_owned(container_id, run_id) is None:
                    raise ValueError('container ownership mismatch')
                docker('rm', '-f', '-v', container_id)
                cleanup = 'removed owned container'
            except Exception:
                cleanup = 'container cleanup failed'
                result = 1
        try:
            receipt = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {}
            if receipt.get('target_db') != target:
                receipt = {'tool': 'warilink_restore_verify', 'target_db': target}
            receipt['container_cleanup'] = cleanup
            receipt['workdir_cleanup'] = work_cleanup
            if creation_uncertain:
                receipt['creation_check'] = creation_check
                if cleanup != 'removed owned container':
                    receipt['retained_resources'] = {'run_id': run_id, 'container_name': name,
                                                     'container_id': candidate_id,
                                                     'confirmation': creation_check}
            elif cleanup == 'container cleanup failed':
                receipt['retained_resources'] = {'run_id': run_id, 'container_name': name,
                                                 'container_id': container_id, 'confirmation': 'cleanup failed'}
            if not child_stopped:
                receipt['retained_resources'] = {'run_id': run_id, 'container_id': container_id,
                                                 'workdir': str(work_root) if work_root is not None else None}
            if result:
                receipt['result'] = 'FAIL'
            RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + '\n')
        except (OSError, ValueError):
            result = 1
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    return 0 if result == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
