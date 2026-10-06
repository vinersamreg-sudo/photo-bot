"""One authorized recovery proof; isolated ops branch, never an app entrypoint."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path('/opt/photo-bot')
EXPECTED_RUNTIME = '1bf95db68b36c17615b6fcdc640fe69eb9b5b9a1'
BACKUPS = ROOT / 'data/backups'
STATE = BACKUPS / 'controlled_recovery_20261006.json'


def service():
    return subprocess.check_output(
        ['systemctl','show','photo-bot.service','-p','MainPID','-p','NRestarts','-p','ActiveState'],
        text=True,
    ).strip()


def existing_backups():
    return {p.name:[p.stat().st_ino,p.stat().st_size,p.stat().st_mtime_ns]
            for p in BACKUPS.glob('*.enc') if p.is_file() and not p.is_symlink()}


def main():
    action, implementation, run = sys.argv[1:4]
    assert action in ('create','confirm')
    assert implementation == '0135c1d639952584c2bf2c639e87fba08a434a13'
    assert run.isdigit()
    os.umask(0o077)
    stage = Path(__file__).resolve().parent
    code = stage/'backup.py'
    assert hashlib.sha256(code.read_bytes()).hexdigest() == (stage/'module.sha256').read_text().strip()
    assert (ROOT/'.deploy-sha').read_text().strip() == EXPECTED_RUNTIME
    assert ROOT.resolve()==ROOT and BACKUPS.resolve()==BACKUPS
    assert stat.S_IMODE(BACKUPS.stat().st_mode)==0o700
    spec=importlib.util.spec_from_file_location('isolated_backup',code)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class NoPruneBackupManager(module.BackupManager):
        def prune(self, now=None):
            return 0

    manager=NoPruneBackupManager(ROOT/'data/photo_bot.sqlite3',BACKUPS,14)
    if action=='confirm':
        proof=json.loads(STATE.read_text())
        assert proof['overall']=='PASS' and proof['run_id']==run
        artifact_id=sys.argv[4]
        assert artifact_id.isdigit() and int(artifact_id)>0
        assert manager._sha256(BACKUPS/proof['backup_name'])==proof['sha256']
        report=manager.mark_recovery_offsite(proof['backup_name'],'github-actions')
        proof.update(offsite='PASS',artifact_id=artifact_id)
        module._write_json(STATE,proof)
        print(json.dumps({'offsite':'PASS','backup_name':report['backup_name'],'artifact_id':artifact_id}))
        return

    # Durable once-only guard: rerunning a workflow must never make a second copy.
    with STATE.open('x') as f:
        json.dump({'overall':'STARTED','run_id':run,'implementation_sha':implementation},f)
    env_hash=hashlib.sha256((ROOT/'.env').read_bytes()).hexdigest()
    svc=service()
    assert 'ActiveState=active' in svc and 'NRestarts=0' in svc
    conn=sqlite3.connect((ROOT/'data/photo_bot.sqlite3').as_uri()+'?mode=ro',uri=True)
    conn.execute('PRAGMA query_only=ON')
    assert conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==14
    conn.close()
    v=os.statvfs(BACKUPS)
    assert v.f_bavail*v.f_frsize >= 5*1024**3
    old=existing_backups()
    passphrase=sys.stdin.readline().rstrip('\r\n')
    created=manager.create_recovery_bundle(passphrase)
    restored=manager.restore_recovery_bundle(Path(created['recovery_bundle_name']),passphrase)
    del passphrase
    assert restored['overall']=='PASS' and restored['migration']==14
    assert restored['source_revision']==EXPECTED_RUNTIME
    assert all(existing_backups().get(n)==s for n,s in old.items())
    assert service()==svc
    assert hashlib.sha256((ROOT/'.env').read_bytes()).hexdigest()==env_hash
    assert (ROOT/'.deploy-sha').read_text().strip()==EXPECTED_RUNTIME
    v=os.statvfs(BACKUPS)
    proof={**restored,'sha256':created['sha256'],'size_bytes':created['size_bytes'],
           'implementation_sha':implementation,'run_id':run,'offsite':'PENDING',
           'prune_disabled':True,'old_backups_preserved':True,'service_unchanged':True,
           'env_unchanged':True,'free_bytes_after':v.f_bavail*v.f_frsize}
    module._write_json(STATE,proof)
    print(json.dumps(proof))


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Never expose tracebacks, raw stderr or private paths from an ops runner.
        safe=str(exc) if type(exc).__name__=='BackupError' else type(exc).__name__
        print('controlled_recovery=FAIL; reason='+safe,file=sys.stderr)
        sys.exit(1)
