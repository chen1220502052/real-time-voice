import pytest

from deploy.activate_release import release_settings
from deploy.configure_prefetch import updated_config


def test_deployment_preserves_provider_and_voice_flags():
    assert release_settings("test-release") == {"GIT_COMMIT": "test-release"}
    assert release_settings("test-release", "0") == {
        "GIT_COMMIT": "test-release", "DUPLEX_PREFETCH_ENABLED": "0"}
    assert release_settings("test-release", "1")["DUPLEX_PREFETCH_ENABLED"] == "1"
    with pytest.raises(ValueError):
        release_settings("test-release", "invalid")


def test_role_migration_is_explicit_and_preserves_duplex_flags():
    settings=release_settings('role-release', None, 'ecs_ram_role')
    assert settings['ALIYUN_NLS_AUTH_MODE']=='ecs_ram_role'
    assert 'DUPLEX_PREFETCH_ENABLED' not in settings
    assert 'ALIYUN_AK_SECRET' not in settings and 'ALIYUN_NLS_TOKEN' not in settings
    with pytest.raises(ValueError):release_settings('role-release', None, 'token')


def test_browser_auth_capacity_migration_preserves_duplex_and_models():
    settings=release_settings('mobile-release',connection_capacity='3')
    assert settings['MAX_CONCURRENT_SESSIONS']=='3'
    assert settings['BROWSER_AUTH_ENABLED']=='1' and settings['BROWSER_AUTH_COOKIE_SECURE']=='1'
    assert not any(k in settings for k in ('VAD_MAX_END_SILENCE_MS','DUPLEX_PREFETCH_ENABLED','TTS_BACKEND','ALIYUN_NLS_AUTH_MODE'))
    with pytest.raises(ValueError):release_settings('bad',connection_capacity='99')


def test_prefetch_config_only_release_preserves_credentials_vad_and_models():
    original = "# keep\nALIYUN_NLS_AUTH_MODE=ecs_ram_role\nVAD_END_MS=550\nTTS_BACKEND=piper\nGIT_COMMIT=old\nDUPLEX_PREFETCH_ENABLED=0\n"
    result = updated_config(original, "prefetch-on", "1")
    assert result == "# keep\nALIYUN_NLS_AUTH_MODE=ecs_ram_role\nVAD_END_MS=550\nTTS_BACKEND=piper\nGIT_COMMIT=prefetch-on\nDUPLEX_PREFETCH_ENABLED=1\n"
    assert updated_config(result, "prefetch-on", "1") == result
    with pytest.raises(ValueError):
        updated_config(original, "../invalid", "1")
    with pytest.raises(ValueError):
        updated_config(original, "valid", "true")
