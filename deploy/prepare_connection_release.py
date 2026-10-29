"""Cloud-only immutable, verified two-call browser connection release bundle."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile


def hashes(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ('backend','web') for p in (root/folder).rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('._')}


def main():
    release,candidate=sys.argv[1],Path(sys.argv[2]).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',release) or candidate.parent!=Path('/var/tmp') or not candidate.name.startswith('rtv-connection-lab.'):
        raise ValueError('Invalid release/candidate')
    root=Path('/opt/real-time-voice');baseline=hashes(root);tested=hashes(candidate)
    original=json.loads((candidate/'production-hashes.json').read_text())
    if baseline!=original:raise RuntimeError('Production changed during candidate tests')
    changed={k for k,v in tested.items() if baseline.get(k)!=v}
    allowed={'backend/app.py','backend/browser_access.py','backend/observability.py',
        'backend/providers/asr_cloud.py','backend/providers/asr_nls.py','web/app.js','web/index.html','web/connection.js'}
    if changed!=allowed or set(baseline)-set(tested):raise RuntimeError('Unexpected serving code changes')
    for name in ('multicall-final','disconnect-final','auth-final','safari-final'):
        report=json.loads((candidate/(name+'.json')).read_text())
        if not report.get('passed'):raise RuntimeError(name+' acceptance has not passed')
        if name=='multicall-final' and report.get('capacity')!=2:raise RuntimeError('Only the validated two-call release is approved')
    browser=json.loads((candidate/'chrome-final.json').read_text())
    if browser.get('errors') or browser.get('asrFinalCount')!=1 or browser.get('userReplyCount')!=1:
        raise RuntimeError('Real browser voice acceptance has not passed')
    baseline['@nginx-https']=hashlib.sha256(Path('/etc/nginx/sites-available/real-time-voice-https').read_bytes()).hexdigest()
    manifest=Path('/var/tmp')/(release+'-expected.json');bundle=Path('/var/tmp')/(release+'.tar.gz')
    if manifest.exists() or bundle.exists():raise RuntimeError('Never overwrite release evidence')
    manifest.write_text(json.dumps(baseline,indent=2)+'\n');manifest.chmod(0o600)
    def clean(info):
        return None if '__pycache__' in Path(info.name).parts or Path(info.name).name.startswith('._') else info
    with tarfile.open(bundle,'w:gz') as archive:
        for folder in ('backend','web'):archive.add(candidate/folder,arcname=folder,filter=clean)
        archive.add(candidate/'deploy/nginx-https.conf',arcname='deploy/nginx-https.conf')
    bundle.chmod(0o600)
    print(json.dumps({'bundleSha256':hashlib.sha256(bundle.read_bytes()).hexdigest(),
        'changedFiles':sorted(changed),'candidateHashes':tested}),flush=True)


if __name__=='__main__':main()
