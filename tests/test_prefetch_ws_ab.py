import pytest

from scripts.check_prefetch_ws_ab import summarize, turn


def row(value, cache, lead=0):
    return {"fixtureSignalEndToFirstPacketMs": value, "finalToFirstPacketMs": value - 500,
            "botStartToFirstTextMs": value - 700, "cache": cache, "asrFinal": "Hello.",
            "cacheDetail": {"prefetches": int(cache != "disabled"), "effectiveLeadMs": lead,
                            "remainingWaitMs": value - 700}}


def test_all_turns_including_misses_and_regressions_are_in_denominator():
    pairs = [{"off": row(2000, "disabled"), "on": row(1500, "hit", 400)},
             {"off": row(1800, "disabled"), "on": row(1900, "miss")}]
    result = summarize(pairs)
    assert result["arms"]["on"]["hitRate"] == .5
    assert result["arms"]["on"]["effectiveLeadMsMedianAllTurns"] == 200
    assert result["pairedSavingsMs"]["fixtureSignalEndToFirstPacketMs"] == {
        "mean": 200, "median": 200, "min": -100, "max": 500, "fasterPairs": 1}
    pairs[1]["on"]["asrFinal"] = "Hello!"
    assert summarize(pairs)["sameTranscriptPairs"] == 1


def test_no_pairs_and_missing_metrics_are_not_fake_zero_results():
    assert summarize([]) == {"pairs": 0}
    with pytest.raises(KeyError):
        summarize([{"off": row(2000, "disabled"), "on": {"failed": True}}])


def test_reply_alignment_excludes_opening_and_uses_server_clock_only():
    report = {"errors": [], "asrFinals": ["Hello."], "replies": [{"replyId": "opening"}, {
        "replyId": "user", "serverStages": {"botStartToFirstTextMs": 400},
        "firstPacketReceivedMs": 90000000,
        "timing": {"asrFinalMs": 1000, "firstFrameSendStartedMs": 1600}}]}
    value = turn(report)
    assert value["replyId"] == "user" and value["asrFinal"] == "Hello."
    assert value["finalToFirstPacketMs"] == 600
    assert value["botStartToFirstTextMs"] == 400
