from pathlib import Path

from backend.observability import _sanitize, log_event


def test_log_sanitizer_redacts_tokens_and_home_paths():
    home_file = str(Path.home() / "private" / "model.bin")
    value = _sanitize(f"failed at {home_file} with ghp_abcdefghijklmnopqrstuvwxyz123456")
    assert home_file not in value
    assert "ghp_" not in value
    assert "<path>" in value
    assert "<secret>" in value


def test_frame_spans_do_not_hide_connection_failures(monkeypatch,caplog):
    monkeypatch.setenv('LOG_AUDIO_SPANS','0')
    with caplog.at_level('INFO'):
        log_event('span.start',span='duplex.asr_vad')
        log_event('span.end',span='websocket.audio.decode')
        log_event('client.event',eventType='playback_progress')
        log_event('browser.connection_failure',code='WEBSOCKET_TIMEOUT')
    assert 'duplex.asr_vad' not in caplog.text and 'playback_progress' not in caplog.text
    assert 'WEBSOCKET_TIMEOUT' in caplog.text
