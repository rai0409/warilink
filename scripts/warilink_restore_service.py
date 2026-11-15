#!/usr/bin/env python3
"""Run the monthly restore in a container owned by this invocation."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
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
    result = 1
    cleanup = 'not created'
    def terminate(signum, frame):
        raise InterruptedError('terminated')
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        password = os.environ['WARILINK_DRILL_DB_PASSWORD']
        if not password:
            raise ValueError('missing password')
        env = dict(os.environ, POSTGRES_PASSWORD=password)
        container_id = docker('run', '-d', '--name', name, '--label', f'{OWNER}=monthly-restore', '--label',
                              f'{RUN}={run_id}', '-e', 'POSTGRES_USER=warilink', '-e', 'POSTGRES_PASSWORD',
                              '-e', 'POSTGRES_DB=postgres', '-p', '127.0.0.1::5432', 'postgres:18-alpine', env=env)
        if not re.fullmatch(r'[0-9a-f]{64}', container_id):
            container_id = None
            raise ValueError('invalid container id')
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
               '--target-db', target, '--workdir', f'/var/lib/warilink/drill-work-{run_id}',
               '--receipt-out', str(RECEIPT)]
        result = subprocess.run(cmd, env=child_env, capture_output=True, timeout=14000, check=False).returncode
        if result == 0:
            receipt = json.loads(RECEIPT.read_text())
            if receipt.get('target_db') != target or receipt.get('result') != 'PASS':
                result = 1
    except (Exception, KeyboardInterrupt):
        result = 1
    finally:
        if container_id:
            try:
                if inspect_owned(container_id, run_id) is None:
                    raise ValueError('container ownership mismatch')
                docker('rm', '-f', container_id)
                cleanup = 'removed owned container'
            except Exception:
                cleanup = 'container cleanup failed'
                result = 1
        try:
            receipt = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {}
            if receipt.get('target_db') != target:
                receipt = {'tool': 'warilink_restore_verify', 'target_db': target}
            receipt['container_cleanup'] = cleanup
            if result:
                receipt['result'] = 'FAIL'
            RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + '\n')
        except (OSError, ValueError):
            result = 1
    return 0 if result == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
