"""Root-only isolated cloud candidate, without changing production services."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tarfile
import tempfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--capacity',choices=('2','3'),default='2')
    parser.add_argument('--unit',default='rtv-connection-candidate-20261023-r2')
    args=parser.parse_args()
    stage=Path(tempfile.mkdtemp(prefix='rtv-connection-lab.',dir='/var/tmp'))
    stage.chmod(0o755)
    with tarfile.open('/var/tmp/rtv-connection-candidate-20261023.tar.gz') as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk() or not (stage/member.name).resolve().is_relative_to(stage):
                raise ValueError('Unsafe candidate path')
        bundle.extractall(stage)
    root=Path('/opt/real-time-voice')
    hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('backend','web') for p in (root/folder).rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('._')}
    (stage/'production-hashes.json').write_text(json.dumps(hashes,indent=2)+'\n')
    key=Path('/var/lib/real-time-voice/browser-auth.key')
    if not key.exists():
        descriptor=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'wb') as stream:stream.write(secrets.token_bytes(48))
        shutil.chown(key,user='catvoice',group='catvoice')
    config=(stage/'deploy/nginx-https.conf').read_text()
    config='\n'.join(line for line in config.splitlines() if not any(x in line for x in ('ssl_certificate','ssl_protocols')))
    config=config.replace('listen 443 ssl;','listen 127.0.0.1:8878;').replace('127.0.0.1:8765','127.0.0.1:8877')
    config=f'user www-data;\npid {stage}/nginx.pid;\nerror_log {stage}/nginx-error.log;\nevents {{worker_connections 256;}}\nhttp {{access_log {stage}/nginx-access.log;\n'+config+'\n}\n'
    (stage/'nginx.conf').write_text(config)
    for path in [stage,*stage.rglob('*')]:
        shutil.chown(path,user='catvoice',group='catvoice')
    subprocess.run(['nginx','-t','-c',str(stage/'nginx.conf'),'-p',str(stage)],check=True)
    subprocess.run(['nginx','-c',str(stage/'nginx.conf'),'-p',str(stage)],check=True)
    environment={
        'PYTHONPATH':f'{stage}:/opt/real-time-voice/runtime_deps/nls_role',
        'HOST':'127.0.0.1','PORT':'8877','BOT_STATE_DB':str(stage/'bot-state.sqlite3'),
        'GIT_COMMIT':'rtv-20261023-connection-candidate-r2','MAX_CONCURRENT_SESSIONS':args.capacity,
        'BROWSER_AUTH_ENABLED':'1','BROWSER_AUTH_KEY_FILE':str(key),
        'BROWSER_AUTH_COOKIE_SECURE':'0','LOG_AUDIO_SPANS':'0'}
    subprocess.run(['systemd-run','--unit='+args.unit,'--collect',
        '--property=User=catvoice','--property=Group=catvoice',f'--working-directory={stage}',
        '--property=EnvironmentFile=/etc/real-time-voice.env','--property=MemoryMax=3G',
        '--property=LogRateLimitBurst=2000','/usr/bin/env',*[f'{k}={v}' for k,v in environment.items()],
        '/opt/real-time-voice/.venv/bin/python','-m','backend.app'],check=True)
    print(json.dumps({'stage':str(stage),'backendPort':8877,'proxyPort':8878}),flush=True)


if __name__=='__main__':main()
