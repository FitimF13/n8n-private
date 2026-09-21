"""Start the pinned runtime with a separate, recoverable persistent database.

The legacy data directory is never migrated. A Render rollback to the previous
artifact therefore opens the original database, not a downgraded new schema.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

os.umask(0o077)
root = Path(__file__).resolve().parent
legacy = Path(os.environ['N8N_USER_FOLDER']).resolve() / '.n8n'
runtime_home = legacy / 'runtime-2.39.10'
data = runtime_home / '.n8n'
marker = runtime_home / 'copy-complete.json'
verified = runtime_home / 'migration-verified.json'

def connect_readonly(path):
    return sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)

def inventory(connection):
    workflows = {}
    for ident, name, active, nodes, connections in connection.execute(
            'SELECT id,name,active,nodes,connections FROM workflow_entity'):
        content = json.dumps([json.loads(nodes), json.loads(connections)], sort_keys=True)
        workflows[ident] = {'name': name, 'active': bool(active),
                           'definition': hashlib.sha256(content.encode()).hexdigest()}
    credentials = dict(connection.execute('SELECT id,data FROM credentials_entity'))
    return {'workflows': workflows,
            'credentials': {key: hashlib.sha256(value.encode()).hexdigest()
                            for key, value in credentials.items()},
            'execution_count': connection.execute('SELECT count(*) FROM execution_entity').fetchone()[0]}

if not marker.exists():
    assert (legacy / 'database.sqlite').is_file(), 'Legacy database not found; refusing empty setup'
    assert (legacy / 'config').is_file(), 'Encryption configuration not found'
    assert not data.exists(), 'Partial migration directory exists; inspect before retrying'
    needed = (legacy / 'database.sqlite').stat().st_size * 2 + 256 * 1024**2
    assert shutil.disk_usage(legacy).free > needed, 'Insufficient free space for migration and rollback'
    data.mkdir(parents=True, mode=0o700)
    with connect_readonly(legacy / 'database.sqlite') as source:
        with sqlite3.connect(data / 'database.sqlite') as target:
            source.backup(target, pages=2048, sleep=0.05)
            assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            before = inventory(target)
            target.execute('VACUUM INTO ?', (str(data / 'database.compact.sqlite'),))
    with connect_readonly(data / 'database.compact.sqlite') as compact:
        assert compact.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        assert inventory(compact) == before, 'Compacted copy does not match'
    os.replace(data / 'database.compact.sqlite', data / 'database.sqlite')
    for name in ('config', 'binaryData', 'git', 'ssh'):
        source = legacy / name
        if source.is_dir():
            shutil.copytree(source, data / name)
        elif source.is_file():
            shutil.copy2(source, data / name)
    (data / 'config').chmod(0o600)
    marker.write_text(json.dumps({'copiedAt': time.time(), 'before': before}, indent=2))
    print('Legacy database preserved; consistent runtime copy created.', flush=True)

env = os.environ.copy()
env['N8N_USER_FOLDER'] = str(runtime_home)
env['DB_SQLITE_DATABASE'] = str(data / 'database.sqlite')
env['N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS'] = 'true'
env['NODE_OPTIONS'] = '--max-old-space-size=1024'
executable = root / 'node_modules/.bin/n8n'

if not verified.exists():
    with (runtime_home / 'migration.log').open('w') as log:
        result = subprocess.run([str(executable), 'export:workflow', '--all',
                                 '--output=' + str(runtime_home / 'workflow-verification.json')],
                                env=env, stdout=log, stderr=subprocess.STDOUT)
    assert result.returncode == 0, 'Migration failed; see protected migration.log; legacy database unchanged'
    before = json.loads(marker.read_text())['before']
    with connect_readonly(data / 'database.sqlite') as connection:
        assert connection.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        after = inventory(connection)
    assert before == after, 'Post-migration inventory mismatch; refusing activation'
    verified.write_text(json.dumps({'verifiedAt': time.time(), 'workflowCount': len(after['workflows']),
                                    'credentialCount': len(after['credentials'])}, indent=2))
    print('Migration verified: workflow definitions, active flags, credentials and execution count match.', flush=True)

os.execve(str(executable), [str(executable), 'start'], env)
