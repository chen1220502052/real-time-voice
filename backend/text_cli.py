"""Standalone text chat: python3.11 -m backend.text_cli --grade 7."""

import argparse
import asyncio
import os
from pathlib import Path
import uuid

from .english_chat import EnglishChatBot
from .text_bot import TextBotError, stream_bot
from .bot_contract import ConversationRequest
from .bot_service import BotService
from .llm_runtime import LLMHttpPool, use_llm_pool


def load_local_env() -> None:
    """Read simple local key=value settings without executing shell code."""
    path = Path(__file__).resolve().parent.parent / ".env.local"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not separator or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


async def chat(args) -> None:
    service = BotService()
    pool = LLMHttpPool()
    try:
        with use_llm_pool(pool):
            await _chat(args, service)
    finally:
        try:
            await service.close()
        finally:
            await pool.close()


async def _chat(args, service) -> None:
    bot = service.session(EnglishChatBot())
    session_id = "text_" + uuid.uuid4().hex

    async def reply(event="user_message", text=""):
        query = ConversationRequest(
            request_id="req_" + uuid.uuid4().hex, session_id=session_id,
            turn_id="turn_" + uuid.uuid4().hex, options={"grade": args.grade}, event=event,
            text=text, channel="text",
        )
        answer = ""
        print("Kitty: ", end="", flush=True)
        async for item in stream_bot(bot, query):
            if item.type == "text_delta":
                answer += item.text
                print(item.text, end="", flush=True)
        print()
        await bot.observe(session_id, ({"eventId": "display_" + query.request_id,
                                      "replyId": query.request_id, "type": "text_displayed", "sequence": 1},))

    await reply("session_start")
    if args.message:
        for text in args.message:
            print("You: " + text)
            await reply(text=text)
        return
    while True:
        try:
            text = await asyncio.to_thread(input, "You (or /quit): ")
        except EOFError:
            return
        if text.strip() == "/quit":
            return
        if not text.strip():
            continue
        try:
            await reply(text=text)
        except (TextBotError, ValueError) as error:
            print("Chat error: " + str(error))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grade", type=int, choices=[3, 4, 5, 6, 7, 8, 9], default=7)
    parser.add_argument("--base-url", help="Existing model endpoint (never a secret URL)")
    parser.add_argument("--model", help="Existing model name")
    parser.add_argument("--message", action="append", help="Repeat for a scripted multi-turn check")
    args = parser.parse_args()
    load_local_env()
    if args.base_url:
        os.environ["LLM_BASE_URL"] = args.base_url
    if args.model:
        os.environ["LLM_MODEL"] = args.model
    try:
        asyncio.run(chat(args))
    except (KeyboardInterrupt, EOFError):
        pass
    except (TextBotError, ValueError) as error:
        print("Chat error: " + str(error))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
