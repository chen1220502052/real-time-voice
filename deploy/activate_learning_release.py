"""Activate the tested learning sample release, with code/config rollback.

No provider settings or existing account data are replaced. Run as root on ECS.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request

ROOT = Path('/opt/real-time-voice')
RELEASE = 'rtv-20261023-learning-r1'
STAGE = Path('/var/tmp/rtv-learning-candidate')
BACKUP = Path('/var/backups/real-time-voice') / RELEASE
CONFIG = Path('/etc/real-time-voice.env')
NGINX = Path('/etc/nginx/sites-available/real-time-voice-https')


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def health():
    return json.load(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    expected = json.loads(Path('/var/tmp/rtv-learning-baseline.json').read_text())
    for relative, sha in expected.items():
        target = NGINX if relative == '@nginx-https' else ROOT / relative
        if not target.is_file() or digest(target) != sha:
            raise RuntimeError('Remote code/config changed since preflight: ' + relative)
    current_files = {str(p.relative_to(ROOT)) for folder in ('backend','web')
                     for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    if current_files != set(expected) - {'@nginx-https'}:
        raise RuntimeError('Remote application files were added or removed since preflight')
    if health().get('resources', {}).get('inUse', 0):
        raise RuntimeError('An active conversation is using the service; retry later')
    verified = json.loads((STAGE / 'verified.json').read_text())
    assert verified.get('ok') and verified.get('release') == RELEASE and verified.get('checks',0) > 100, 'Candidate smoke test has not passed'
    manifest = json.loads((STAGE / 'app-manifest.json').read_text())
    assert all(digest(STAGE / name) == sha for name, sha in manifest.items()), 'Tested candidate changed'
    BACKUP.mkdir(parents=True, mode=0o700, exist_ok=False)
    with tarfile.open(BACKUP / 'app.tar.gz', 'w:gz') as archive:
        for folder in ('backend','web'):
            archive.add(ROOT/folder, arcname=folder)
    shutil.copy2(CONFIG, BACKUP/'runtime.env')
    shutil.copy2(NGINX, BACKUP/'nginx.conf')
    os.chmod(BACKUP/'runtime.env', 0o600)
    lines = CONFIG.read_text().splitlines()
    settings = dict(line.split('=',1) for line in lines if '=' in line and not line.lstrip().startswith('#'))
    for key in ('BOT_STATE_DB', 'ACCOUNT_DB'):
        path = Path(settings.get(key, '').strip('"\''))
        if path.is_file():
            with sqlite3.connect(path) as source, sqlite3.connect(BACKUP/(key+'.sqlite3')) as dest:
                source.backup(dest)
            os.chmod(BACKUP/(key+'.sqlite3'), 0o600)
    if health().get('resources', {}).get('inUse', 0):
        raise RuntimeError('A conversation started during backup; retry later')
    changed = False
    try:
        run('systemctl','stop','real-time-voice')
        changed = True
        for folder in ('backend','web'):
            shutil.rmtree(ROOT/folder)
            shutil.copytree(STAGE/folder, ROOT/folder)
        overrides = {'GIT_COMMIT':RELEASE, 'ACCOUNT_AUTH_ENABLED':'1',
                     'ENGLISH_RESOURCE_ROOT':str(ROOT/'英语')}
        if not settings.get('ACCOUNT_DB'):
            overrides['ACCOUNT_DB'] = '/var/lib/real-time-voice/accounts.sqlite3'
        CONFIG.write_text('\n'.join(line for line in lines if line.partition('=')[0].strip() not in overrides)
                          + '\n' + '\n'.join(f'{key}={value}' for key,value in overrides.items()) + '\n')
        os.chmod(CONFIG, 0o600)
        run('systemctl','start','real-time-voice')
        for _ in range(120):
            try:
                state = health()
                if state.get('ok') and state.get('gitCommit') == RELEASE and state.get('browserAuth') == 'account':
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('New release did not become healthy')
        # Replace the shared Basic password only after application accounts are healthy.
        shutil.copyfile(STAGE/'deploy/nginx-https.conf', NGINX)
        run('nginx','-t')
        run('systemctl','reload','nginx')
        account_db = settings.get('ACCOUNT_DB', '/var/lib/real-time-voice/accounts.sqlite3').strip('"\'')
        report = subprocess.check_output([str(ROOT/'.venv/bin/python'), str(STAGE/'scripts/check_learning_release.py'),
            'https://aimaoren.com', '--account-db', account_db], text=True)
        (BACKUP/'public-smoke-report.json').write_text(report)
        print(report.strip(), flush=True)
        print(json.dumps({'ok':True,'release':RELEASE,'backup':str(BACKUP)}), flush=True)
    except BaseException:
        if changed:
            run('systemctl','stop','real-time-voice')
            for folder in ('backend','web'):
                shutil.rmtree(ROOT/folder)
            with tarfile.open(BACKUP/'app.tar.gz') as archive:
                archive.extractall(ROOT)
            shutil.copy2(BACKUP/'runtime.env', CONFIG)
            shutil.copy2(BACKUP/'nginx.conf', NGINX)
            run('nginx','-t')
            run('systemctl','reload','nginx')
            run('systemctl','start','real-time-voice')
            print('Activation failed; previous code and configuration restored', flush=True)
        raise


if __name__ == '__main__':
    main()
