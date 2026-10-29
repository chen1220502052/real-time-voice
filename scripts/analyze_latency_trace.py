"""Same-reply timing audit; no RTT/2 or physical speaker claim."""
import argparse
import json
from pathlib import Path


def analyze(report):
    rows = []
    for reply in report.get("replies", []):
        s = reply.get("timing", {})
        source_sequence = reply.get("sourceEndSequence")
        receipt = dict(s.get("inputReceipts", [])).get(source_sequence)
        send = reply.get("sourceEndSentMs")
        first = reply.get("firstPacketReceivedMs")
        stages = {}
        points = [("endpointAndFinal", receipt), ("finalToBot", s.get("asrFinalMs")),
                  ("botToFirstText", s.get("botRequestStartedMs")),
                  ("firstTextToTts", s.get("botFirstTextMs")),
                  ("ttsToFirstPcm", s.get("ttsStartedMs")),
                  ("pcmToFrame", s.get("ttsFirstPcmMs")),
                  ("frameQueue", s.get("firstPcmEnqueuedMs")),
                  ("sendLockWait", s.get("firstFrameSendQueuedMs")),
                  (None, s.get("firstFrameSendStartedMs"))]
        for (name, start), (_, end) in zip(points, points[1:]):
            stages[name+"Ms"] = end-start if start is not None and end is not None else None
        total = (points[-1][1]-receipt if receipt is not None and points[-1][1] is not None else None)
        combined = first-send-total if first is not None and send is not None and total is not None else None
        assembly = 100-reply["sourceEndWithinPacketMs"] if reply.get("sourceEndWithinPacketMs") is not None else None
        measured = reply.get("fixtureSignalEndToFirstPacketMs")
        budget = assembly+combined+total if all(v is not None for v in (assembly, combined, total)) else None
        rows.append({"replyId": reply.get("replyId"), "sourceEndSequence": source_sequence,
                     "serverStages": stages, "serverTotalMs": total,
                     "packetAssemblyRemainderMs": assembly,
                     "combinedTransportAndClientDeliveryMs": combined,
                     "fixtureSignalEndToFirstPacketMs": measured,
                     "closureResidualMs": measured-budget if measured is not None and budget is not None else None,
                     "physicalOutput": None})
    return {"measurement": "paced_fixture_to_packet_receipt_not_browser_or_speaker", "replies": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.report.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
