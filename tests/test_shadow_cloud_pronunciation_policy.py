import hashlib
import json
from types import SimpleNamespace

import pytest
from scripts import build_shadow_assets as builder


def fixture_library(tmp_path, monkeypatch, *, cloud=False):
    resources=tmp_path/'audio';data=tmp_path/'data'
    (resources/'科普版').mkdir(parents=True);data.mkdir()
    (resources/'科普版/初中本地课文词表索引.json').write_text('[]')
    sample=resources/'actor.wav';sample.write_bytes(b'previous-pronunciation-must-not-be-overwritten')
    entry={'kind':'synthetic','path':'actor.wav','sha256':hashlib.sha256(sample.read_bytes()).hexdigest(),'duration':1.25}
    if cloud:entry.update(provider='openai',model='cloud-speech-test',generationMethod='desktop-export')
    metadata=data/'word-audio.json';metadata.write_text(json.dumps({'version':1,'words':{'actor':entry}}))
    monkeypatch.setattr(builder,'DATA',data);monkeypatch.setattr(builder,'RESOURCES',resources)
    monkeypatch.setattr(builder,'ChapterLibrary',lambda root:SimpleNamespace(chapters={'c':{'vocabulary':[{'word':'actor'}],'recordings':[]}}))
    return metadata,sample


def test_existing_local_synthetic_file_does_not_authorize_local_tts_rebuild(tmp_path,monkeypatch):
    metadata,sample=fixture_library(tmp_path,monkeypatch)
    previous_metadata=metadata.read_bytes();previous_audio=sample.read_bytes()
    with pytest.raises(RuntimeError,match='local TTS is disabled'):
        builder.build()
    assert metadata.read_bytes()==previous_metadata
    assert sample.read_bytes()==previous_audio


def test_packaging_cloud_asset_preserves_provenance_for_next_rebuild(tmp_path,monkeypatch):
    import soundfile
    metadata,sample=fixture_library(tmp_path,monkeypatch,cloud=True)
    monkeypatch.setattr(soundfile,'info',lambda path:SimpleNamespace(duration=1.25))
    builder.build()
    item=json.loads(metadata.read_text())['words']['actor']
    assert (item['provider'],item['model'],item['generationMethod'])==('openai','cloud-speech-test','desktop-export')
    assert item['sha256']==hashlib.sha256(sample.read_bytes()).hexdigest()
