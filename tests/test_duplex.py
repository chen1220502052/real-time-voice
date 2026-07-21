import asyncio

from backend.atomic import DemoAgent, DemoTts, OPENING_MESSAGE, VadAsrStub
from backend.domain import Session, SessionState
from backend.duplex import DuplexDialogue


def test_duplex_development_reply_becomes_playback_packets():
    async def run():
        duplex = DuplexDialogue(Session("sess_test", SessionState.PICK_UP))
        await duplex.start()
        await duplex.submit_text_for_development("你好")
        packets = 0
        async for event in duplex.events():
            if event.kind == "playback_packet":
                packets += 1
                await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])
            if event.kind == "reply_complete":
                break
        assert packets > 0
        assert duplex.session.robot.status == 0
        assert [item.role for item in duplex.history.items] == ["user", "signal", "robot"]
        await duplex.close("test_done")

    asyncio.run(run())


def test_pick_up_signal_generates_the_fixed_opening_reply():
    async def run():
        duplex = DuplexDialogue(
            Session("sess_pickup", SessionState.PICK_UP),
            asr=VadAsrStub(),
            agent=DemoAgent(),
            tts=DemoTts(),
        )
        await duplex.start()
        await duplex.pick_up()
        packets = 0
        async for event in duplex.events():
            if event.kind == "playback_packet":
                packets += 1
                await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])
            if event.kind == "reply_complete":
                break
        assert packets > 0
        assert [item.message for item in duplex.history.items] == ["pick_up", "reply_start", OPENING_MESSAGE]
        await duplex.close("test_done")

    asyncio.run(run())
