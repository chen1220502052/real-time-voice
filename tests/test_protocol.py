import pytest

from backend.protocol import PCM_100MS_BYTES, ProtocolError, SequenceValidator, decode_audio_frame, encode_audio_frame


def test_audio_frame_round_trip_and_speech_time():
    payload = b"\x00" * PCM_100MS_BYTES
    encoded = encode_audio_frame(0, "sess_test", 4, 123, payload)
    decoded = decode_audio_frame(encoded)
    assert decoded.direction == 0
    assert decoded.sequence == 4
    assert decoded.speech_time == 500
    assert decoded.payload == payload


def test_sequence_validator_rejects_gaps_and_duplicates_then_resynchronizes():
    validator = SequenceValidator()
    validator.validate(0)
    with pytest.raises(ProtocolError, match="expected 1, got 2"):
        validator.validate(2)
    validator.validate(3)
    with pytest.raises(ProtocolError, match="expected 4, got 3"):
        validator.validate(3)
