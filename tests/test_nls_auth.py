import asyncio
import sys
from types import SimpleNamespace

import pytest

from backend.providers.nls_auth import (AliyunNlsTokenProvider, EcsRamRoleNlsTokenProvider,
                                        NlsFailure, token_provider_from_environment)
from backend.providers.asr_nls import AliyunNlsAsrVadAdapter, checked_transcriber_factory
from backend.app import WebSocketConnection


def test_nls_concurrency_limit_is_not_misreported_as_auth_failure():
    adapter=AliyunNlsAsrVadAdapter(transcriber_factory=lambda **kwargs:None)
    adapter._on_nls_error({'header':{'status':40000005,'status_message':'Too many requests'}})
    assert adapter._cloud_error.code=='NLS_CONCURRENCY_LIMIT'
    assert '并发' in str(adapter._cloud_error)
    assert not adapter._cloud_error.fatal


def test_role_mode_never_falls_back_to_static_secrets(monkeypatch):
    monkeypatch.setenv('ALIYUN_NLS_AUTH_MODE', 'ecs_ram_role')
    monkeypatch.setenv('ALIYUN_NLS_TOKEN', 'obsolete-token')
    monkeypatch.setenv('ALIYUN_AK_ID', 'unrelated-ak')
    monkeypatch.setenv('ALIYUN_AK_SECRET', 'unrelated-secret')
    assert isinstance(token_provider_from_environment(), EcsRamRoleNlsTokenProvider)


def test_role_sts_rotates_on_nls_refresh_and_cache_coalesces(monkeypatch):
    AliyunNlsTokenProvider._cache.clear()
    now, states, clients, requests = [1000], [], [], []
    class Credentials:
        def get_credential(self):
            n = len(states)+1
            states.append(n)
            return SimpleNamespace(get_access_key_id=lambda:f'id{n}',
                get_access_key_secret=lambda:f'secret{n}', get_security_token=lambda:f'sts{n}')
    def client(**kwargs):
        clients.append(kwargs)
        def invoke(request):
            requests.append(request)
            return ('{"Token":{"Id":"nls'+str(len(states))+'","ExpireTime":'+str(now[0]+1000)+'}}').encode()
        return SimpleNamespace(do_action_with_exception=invoke)
    class Request:
        def __getattr__(self, key):
            return lambda value:setattr(self,key,value)
    monkeypatch.setitem(sys.modules,'aliyunsdkcore.client',SimpleNamespace(AcsClient=client))
    monkeypatch.setitem(sys.modules,'aliyunsdkcore.auth.credentials',SimpleNamespace(StsTokenCredential=lambda *args:args))
    monkeypatch.setitem(sys.modules,'aliyunsdkcore.request',SimpleNamespace(CommonRequest=Request))
    provider=EcsRamRoleNlsTokenProvider('fixture-role',credential_client=Credentials(),clock=lambda:now[0])
    assert provider.get_token() == provider.get_token() == 'nls1'
    now[0]=1750
    assert provider.get_token() == 'nls2'
    assert clients[0]['credential'] == ('id1','secret1','sts1')
    assert clients[1]['credential'] == ('id2','secret2','sts2')
    assert requests[0].set_protocol_type == 'https'
    assert len(states) == 2


def test_parallel_sessions_share_one_token_refresh():
    from concurrent.futures import ThreadPoolExecutor
    AliyunNlsTokenProvider._cache.clear()
    calls=[]
    def fetch():calls.append(1);return 'shared',2000
    providers=[EcsRamRoleNlsTokenProvider('parallel-role',fetcher=fetch,clock=lambda:1000) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda provider:provider.get_token(),providers)) == ['shared']*8
    assert len(calls)==1


def test_role_provider_uses_only_hardened_imds_and_redacts_failure(monkeypatch):
    seen={}
    def config(**kwargs):seen.update(kwargs);return kwargs
    def fail(*args):raise RuntimeError('secret-signed-url-fixture')
    monkeypatch.setitem(sys.modules,'aliyunsdkcore.client',SimpleNamespace(AcsClient=object))
    monkeypatch.setitem(sys.modules,'aliyunsdkcore.auth.credentials',SimpleNamespace(StsTokenCredential=object))
    monkeypatch.setitem(sys.modules,'alibabacloud_credentials.client',SimpleNamespace(Client=fail))
    monkeypatch.setitem(sys.modules,'alibabacloud_credentials.models',SimpleNamespace(Config=config))
    with pytest.raises(NlsFailure) as error:
        EcsRamRoleNlsTokenProvider('test-role')._client()
    assert error.value.code == 'NLS_ROLE_CREDENTIALS_UNAVAILABLE'
    assert 'secret-signed' not in str(error.value)
    assert seen['type']=='ecs_ram_role' and seen['disable_imds_v1'] is True


def test_failed_token_refresh_never_reuses_expired_token_or_leaks_error():
    AliyunNlsTokenProvider._cache.clear()
    now=[1000]
    def fetch():
        if now[0]>1100:raise NlsFailure('NLS_TOKEN_REFRESH_FAILED','safe failure')
        return 'token',1100
    provider=EcsRamRoleNlsTokenProvider('expiry',fetcher=fetch,clock=lambda:now[0],refresh_margin_seconds=10)
    assert provider.get_token()=='token'
    now[0]=1200
    with pytest.raises(NlsFailure):provider.get_token()


def test_handshake_403_surfaces_without_upload_or_sensitive_exception():
    class Sdk:
        def __init__(self,**kwargs):self.callbacks=kwargs
        def start(self,**kwargs):
            error=RuntimeError('Token=secret-fixture')
            error.status_code=403
            self._NlsSpeechTranscriber__tr_core_on_error(error)
            self.callbacks['on_close']()
        def stop(self):pass
        def send_audio(self,pcm):raise AssertionError('Audio sent before readiness')
    a=AliyunNlsAsrVadAdapter(transcriber_factory=checked_transcriber_factory(SimpleNamespace(NlsSpeechTranscriber=Sdk)))
    a._start_recognition()
    a._send_cloud_audio(b'\0'*3200)
    with pytest.raises(NlsFailure) as error:a._stop_recognition()
    assert error.value.code=='NLS_AUTH_FAILED' and error.value.fatal
    assert 'secret-fixture' not in str(error.value)
    with pytest.raises(NlsFailure):asyncio.run(a.reset())
    assert not a._send_thread and not a._in_speech


def test_start_return_is_not_readiness_and_old_callbacks_are_fenced():
    callbacks=[]
    class Sdk:
        def __init__(self,**kwargs):callbacks.append(kwargs)
        def start(self,**kwargs):pass
        def stop(self):pass
    a=AliyunNlsAsrVadAdapter(transcriber_factory=Sdk)
    a._start_recognition()
    with pytest.raises(NlsFailure) as error:a._stop_recognition()
    assert error.value.code=='NLS_NOT_READY'
    with pytest.raises(NlsFailure):asyncio.run(a.reset())
    callbacks[0]['on_start']({})
    callbacks[0]['on_error']({'header':{'message':'token secret-fixture invalid'}})
    assert not a._nls_ready.is_set() and a._cloud_error is None


def test_successful_start_completion_and_close_are_not_reported_as_failure():
    class Sdk:
        def __init__(self,**kwargs):self.callbacks=kwargs
        def start(self,**kwargs):self.callbacks['on_start']({})
        def send_audio(self,pcm):pass
        def stop(self):
            self.callbacks['on_sentence_end']({'payload':{'result':'Hello.'}})
            self.callbacks['on_completed']({})
            self.callbacks['on_close']()
    a=AliyunNlsAsrVadAdapter(transcriber_factory=Sdk)
    a._start_recognition()
    a._send_cloud_audio(b'\0'*3200)
    assert a._stop_recognition()=='Hello.'
    asyncio.run(a.reset())


def test_asr_public_error_retains_top_level_code_and_whitelists_reason():
    import json
    class Socket:
        values=[]
        async def send_str(self,value):self.values.append(json.loads(value))
    async def run():
        socket=Socket();connection=WebSocketConnection(socket)
        await connection.send_error('ASR_FAILED','secret',reason_code='NLS_AUTH_FAILED')
        assert socket.values[-1]['code']=='ASR_FAILED'
        assert socket.values[-1]['reasonCode']=='NLS_AUTH_FAILED'
        assert 'secret' not in str(socket.values)
        await connection.send_error('ASR_FAILED','secret',reason_code='secret')
        assert 'reasonCode' not in socket.values[-1]
    asyncio.run(run())
