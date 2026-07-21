from backend.protocol import PCM_100MS_BYTES, decode_audio_frame, encode_audio_frame


def test_audio_frame_round_trip_and_speech_time():
    payload = b"\x00" * PCM_100MS_BYTES
    encoded = encode_audio_frame(0, "sess_test", 4, 123, payload)
    decoded = decode_audio_frame(encoded)
    assert decoded.direction == 0
    assert decoded.sequence == 4
    assert decoded.speech_time == 500
    assert decoded.payload == payload
