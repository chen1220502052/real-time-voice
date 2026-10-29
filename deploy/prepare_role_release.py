"""Build an immutable ECS role release and a live-source optimistic-lock manifest.

Run on the remote server only after candidate tests pass. Does not activate code,
change credentials, or modify the running service.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile


def main():
    release, candidate = sys.argv[1], Path(sys.argv[2]).resolve()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", release):
        raise ValueError("Invalid release")
    if candidate.parent != Path('/var/tmp') or not candidate.name.startswith('rtv-ram-verify.'):
        raise ValueError('Expected a named isolated candidate directory')
    root=Path('/opt/real-time-voice')
    dependency=candidate/'dependencies-https'
    if not (dependency/'alibabacloud_credentials').is_dir():
        raise RuntimeError('Validated role dependencies not found')
    expected={}
    candidate_hashes={}
    for folder in ('backend','web'):
        for path in (root/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and not path.name.startswith('._'):
                expected[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (candidate/folder).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and not path.name.startswith('._'):
                candidate_hashes[str(path.relative_to(candidate))]=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest=Path('/var/tmp')/(release+'-expected.json')
    bundle=Path('/var/tmp')/(release+'.tar.gz')
    if manifest.exists() or bundle.exists():
        raise RuntimeError('Release already exists; do not overwrite validated artifacts')
    manifest.write_text(json.dumps(expected,indent=2)+'\n')
    manifest.chmod(0o600)
    def clean(info):
        return None if '__pycache__' in Path(info.name).parts or Path(info.name).name.startswith('._') else info
    with tarfile.open(bundle,'w:gz') as archive:
        for folder in ('backend','web'):
            archive.add(candidate/folder,arcname=folder,filter=clean)
        archive.add(dependency,arcname='runtime_deps/nls_role',filter=clean)
    bundle.chmod(0o600)
    print(json.dumps({'release':release,'bundleSha256':hashlib.sha256(bundle.read_bytes()).hexdigest(),
                      'candidateHashes':candidate_hashes,'baselineFiles':len(expected)},indent=2))


if __name__=='__main__':main()
