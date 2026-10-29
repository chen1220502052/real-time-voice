from scripts.analyze_latency_trace import analyze


def test_same_reply_chain_closes_without_assuming_one_way_rtt():
    points = dict(zip(["asrFinalMs", "botRequestStartedMs", "botFirstTextMs", "ttsStartedMs",
                       "ttsFirstPcmMs", "firstPcmEnqueuedMs", "firstFrameSendQueuedMs",
                       "firstFrameSendStartedMs"], [1100, 1110, 1810, 1820, 2070, 2080, 2090, 2100]))
    points["inputReceipts"] = [[4, 500]]
    row = analyze({"replies": [{"replyId": "r", "sourceEndSequence": 4, "sourceEndSentMs": 100,
                                "sourceEndWithinPacketMs": 70, "firstPacketReceivedMs": 2000,
                                "fixtureSignalEndToFirstPacketMs": 1930, "timing": points}]} )["replies"][0]
    assert row["serverTotalMs"] == 1600
    assert sum(row["serverStages"].values()) == 1600
    assert row["combinedTransportAndClientDeliveryMs"] == 300
    assert row["closureResidualMs"] == 0
    assert row["physicalOutput"] is None


def test_missing_stage_is_not_zero_or_assigned_to_browser():
    row = analyze({"replies": [{"replyId": "opening"}]} )["replies"][0]
    assert row["closureResidualMs"] is None
    assert row["combinedTransportAndClientDeliveryMs"] is None
