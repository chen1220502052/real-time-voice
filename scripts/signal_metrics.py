"""PCM signal measurements matching recorder.js and output-worklet.js.

Signal is abs(PCM16) >= 300, not a linguistic speech-end detector.
"""
SIGNAL_THRESHOLD_PCM16 = 300


def first_onset(samples):
    return next((i for i, value in enumerate(samples) if abs(value) >= SIGNAL_THRESHOLD_PCM16), None)


def last_onset(samples):
    return next((i for i in range(len(samples)-1, -1, -1)
                 if abs(samples[i]) >= SIGNAL_THRESHOLD_PCM16), None)


def bursts(samples, min_gap_ms=300, sample_rate=16000):
    result, last = [], -sample_rate
    for i, value in enumerate(samples):
        if abs(value) >= SIGNAL_THRESHOLD_PCM16:
            if i-last > sample_rate*min_gap_ms/1000:
                result.append(i)
            last = i
    return result


def complete_sum(values):
    values = list(values)
    return None if any(v is None for v in values) else sum(values)


def reconciliation(row):
    """Disjoint intervals; combinedTransport ends at first SEND START, not end."""
    server, client = row.get("serverParts", {}), row.get("clientParts", {})
    dotted = complete_sum(server.get(k) for k in (
        "sourceEndFrameToFinalMs", "finalToBotRequestMs", "botToFirstTextMs", "firstTextToTtsMs",
        "ttsToFirstPcmMs", "pcmToFrameMs", "frameQueueMs", "sendLockWaitMs"))
    upstream = complete_sum(client.get(k) for k in (
        "signalToPacketEndMs", "captureDeliveryLagMs", "captureCallbackToSendMs"))
    downstream = complete_sum(client.get(k) for k in (
        "receiveToPlannedSignalMs", "plannedToGraphSignalMs"))
    expected = complete_sum((dotted, upstream, row.get("combinedTransportMs"), downstream))
    measured = client.get("captureSignalToGraphMs")
    return {"serverReceiveToSendStartMs": dotted, "clientUpstreamMs": upstream,
            "clientDownstreamMs": downstream, "reconstructedMs": expected,
            "residualMs": measured-expected if measured is not None and expected is not None else None}
