from pathlib import Path
import os, sqlite3, shutil, json, hashlib, subprocess, time, sys

os.umask(0o077)
base = Path(os.environ['N8N_PREFLIGHT_DIRECTORY']).resolve()
old = Path('/mnt/mydisk/n8nData/.n8n/.n8n')
home = base / 'stage'
data = home / '.n8n'
data.mkdir(parents=True, exist_ok=True)

def inventory(c):
    workflows = {}
    for ident, name, active, nodes, connections in c.execute('SELECT id,name,active,nodes,connections FROM workflow_entity'):
        value = json.dumps([json.loads(nodes), json.loads(connections)],sort_keys=True)
        workflows[ident] = [name, bool(active), hashlib.sha256(value.encode()).hexdigest()]
    credentials = {k: hashlib.sha256(v.encode()).hexdigest() for k,v in c.execute('SELECT id,data FROM credentials_entity')}
    return dict(workflows=workflows,credentials=credentials,executions=c.execute('SELECT count(*) FROM execution_entity').fetchone()[0])

if not (base/'inventory-before.json').exists():
    assert not (data/'database.sqlite').exists(), 'Inspect partial preparation first'
    raw = base/'uncompacted.sqlite'
    assert not raw.exists(), 'Inspect partial preparation first'
    assert shutil.disk_usage(base).free > 4_000_000_000, 'Insufficient safe preparation space'
    with sqlite3.connect('file:'+str(old/'database.sqlite')+'?mode=ro',uri=True) as source:
        with sqlite3.connect(raw) as target:
            source.backup(target,pages=2048,sleep=0.05)
            before=inventory(target)
            target.execute('VACUUM INTO ?', (str(data/'database.sqlite'),))
    with sqlite3.connect(data/'database.sqlite') as compact:
        assert compact.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert before==inventory(compact)
    # Only the temporary uncompacted copy made above is removed after full verification.
    assert raw.parent == base and raw.name == 'uncompacted.sqlite'
    raw.unlink()
    shutil.copy2(old/'config',data/'config')
    (data/'config').chmod(0o600)
    (base/'inventory-before.json').write_text(json.dumps(before))
else:
    before=json.loads((base/'inventory-before.json').read_text())
print('CONSISTENT_COPY_READY',len(before['workflows']),len(before['credentials']),before['executions'],flush=True)
if '--prepare-only' in sys.argv:
    print('COMPACT_DATABASE_BYTES',(data/'database.sqlite').stat().st_size,flush=True)
    sys.exit(0)
env=os.environ.copy()
env.update(PATH=str(base/'node-v24.21.0-linux-x64/bin')+':'+env['PATH'],
           NODE_OPTIONS='--max-old-space-size=512',N8N_USER_FOLDER=str(home),
           DB_SQLITE_DATABASE=str(data/'database.sqlite'),N8N_PORT='15678',
           N8N_LISTEN_ADDRESS='127.0.0.1',N8N_HOST='localhost',N8N_PROTOCOL='http',
           WEBHOOK_URL='http://127.0.0.1:15678',N8N_EDITOR_BASE_URL='http://127.0.0.1:15678',
           N8N_DIAGNOSTICS_ENABLED='false',N8N_VERSION_NOTIFICATIONS_ENABLED='false',
           N8N_LICENSE_AUTO_RENEW_ENABLED='false',N8N_TEMPLATES_ENABLED='false')
cli=str(base/'runtime/node_modules/.bin/n8n')
process=subprocess.Popen([cli,'export:workflow','--all','--output='+str(base/'workflows-after.json')],env=env)
while process.poll() is None:
    if shutil.disk_usage(base).free < 1_000_000_000:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise RuntimeError('Preflight stopped to preserve at least 1 GB for production')
    time.sleep(1)
assert process.returncode==0,'Migration command failed'
with sqlite3.connect('file:'+str(data/'database.sqlite')+'?mode=ro',uri=True) as c:
    after=inventory(c)
    print('DATABASE_CHECK',c.execute('PRAGMA quick_check').fetchone()[0],flush=True)
    print('COUNTS_AFTER',len(after['workflows']),len(after['credentials']),after['executions'],flush=True)
    print('DEFINITION_AND_CREDENTIAL_MATCH',before==after,flush=True)
    print('DATABASE_SIZE', (data/'database.sqlite').stat().st_size,flush=True)
    columns={r[1] for r in c.execute('PRAGMA table_info(workflow_entity)')}
    if 'activeVersionId' in columns:
        print('PUBLISHED',c.execute('SELECT count(*) FROM workflow_entity WHERE activeVersionId IS NOT NULL').fetchone()[0],flush=True)
(base/'inventory-after.json').write_text(json.dumps(after))
assert before==after,'Inventory changed during migration'
(base/'preflight.ok').write_text(str(time.time()))
print('PREFLIGHT_MIGRATION_PASSED',flush=True)
