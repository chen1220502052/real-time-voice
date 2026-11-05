"""Text-only NDJSON transport for the same TextBot used by voice."""

import asyncio
import json
import uuid

from aiohttp import web

from .curriculum_chat import chat_scope, validate_chat_options
from .text_bot import BotEvent, BotMessage, BotRequest, TextBotError, adapt_text_bot, stream_bot
from .bot_contract import identifier, interaction, parse_request


def bot_http_error(error):
    status = 503 if error.code == "BOT_STORAGE_FAILED" else 429 if error.code == "NO_RESOURCE" else 409
    return web.json_response({"code": error.code, "message": str(error)}, status=status)


async def chat_scope_handler(request):
    try:
        scope = await asyncio.to_thread(chat_scope, request.match_info["chapter_id"])
        return web.json_response({**scope.summary(),
            "selection": {k:scope.chapter[k] for k in ('grade','publisher','edition','unit')},
            "chatPolicy": {"strategy": ["textbook", "prepared", "generated"],
                           "retrievalMaxOutsideRatio": 0, "fallbackVocabularyLimit": False}},
            headers={"Cache-Control": "no-store"})
    except (ValueError, OSError):
        return web.json_response({"message": "该单元词汇资料不可用，请检查词表配置"}, status=400)


def validate_options(request, options):
    scope = validate_chat_options(options)
    factory = request.app["text_bot_factory"]
    if scope is None and (getattr(factory, "requires_chapter", False)
                          or getattr(factory, "requires_grade", False) and "grade" not in options):
        raise ValueError("请选择聊天年级或教材单元")


async def interactions_handler(request):
    try:
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"sessionId", "interactions"}:
            raise ValueError("Invalid interaction batch")
        session = identifier(data["sessionId"])
        values = data["interactions"]
        if not isinstance(values, list) or not 1 <= len(values) <= 256:
            raise ValueError("Invalid interaction batch")
        facts = tuple(interaction(e) for e in values)
        request.app["accounts"].claim(request, session)
        await request.app["bot_service"].store.observe(session, facts)
        return web.json_response({"version": 2, "ok": True})
    except (ValueError, TypeError, KeyError):
        return web.json_response({"code": "INVALID_BOT_REQUEST"}, status=400)
    except TextBotError as error:
        return bot_http_error(error)


async def text_bot_handler(request: web.Request) -> web.StreamResponse:
    try:
        data = await request.json()
        if isinstance(data, dict) and data.get("version", 1) == 1:
            return await legacy_text_bot_handler(request)
        query = parse_request(data)
        await asyncio.to_thread(validate_options, request, query.options)
        request.app["accounts"].claim(request, query.session_id)
    except (ValueError, TypeError, OSError):
        return web.json_response({"code": "INVALID_BOT_REQUEST", "message": "检查教材单元、年级、会话标识、输入和协议版本"}, status=400)
    service = request.app["bot_service"]
    try:
        run = await service.start(query, adapt_text_bot(request.app["text_bot_factory"]()),
                                  request.app["resource_manager"])
    except TextBotError as error:
        return bot_http_error(error)
    response = web.StreamResponse(headers={
        "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    })
    iterator = service.events(run, query.after_event_seq)
    try:
        await response.prepare(request)
        async for value in iterator:
            await response.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
    except (ConnectionResetError, BrokenPipeError):
        pass
    except TextBotError:
        # The accepted journal is unavailable. End the incomplete stream;
        # never invent a durable done/error or send uncommitted reply text.
        response.force_close()
    finally:
        await iterator.aclose()
    return response


async def legacy_text_bot_handler(request: web.Request) -> web.StreamResponse:
    try:
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {
            "version", "requestId", "sessionId", "turnId", "replyId", "generationEpoch", "event", "options", "messages"
        }:
            raise ValueError("Invalid text bot request fields")
        options = data.get("options", {})
        await asyncio.to_thread(validate_options, request, options)
        messages = data.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 32:
            raise ValueError("messages must be an array of at most 32 messages")
        query = BotRequest(
            request_id=data.get("requestId", "req_" + uuid.uuid4().hex),
            session_id=data.get("sessionId", "text_" + uuid.uuid4().hex),
            turn_id=data.get("turnId", "turn_" + uuid.uuid4().hex),
            event=data.get("event", "user_message"), version=data.get("version", 1),
            messages=tuple(BotMessage(**item) for item in messages), options=options,
            reply_id=data.get("replyId", ""), generation_epoch=data.get("generationEpoch", 0),
        )
    except (ValueError, TypeError, OSError):
        return web.json_response({"code": "INVALID_BOT_REQUEST", "message": "请求格式错误：检查年级、消息角色、长度和版本"}, status=400)
    except web.HTTPRequestEntityTooLarge:
        raise

    request.app["accounts"].claim(request, query.session_id)
    manager = request.app["resource_manager"]
    owner = "text_" + uuid.uuid4().hex
    if not await manager.reserve(owner):
        return web.json_response({"code": "NO_RESOURCE", "message": "服务正在处理其他对话，请稍后重试"}, status=429)
    events = None
    response = web.StreamResponse(headers={
        "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    })
    try:
        bot = adapt_text_bot(request.app["text_bot_factory"]())
        events = stream_bot(bot, query)
        await response.prepare(request)
        async for event in events:
            if request.transport is None or request.transport.is_closing():
                raise ConnectionResetError()
            await response.write((json.dumps(event.to_dict(), ensure_ascii=False) + "\n").encode())
    except TextBotError as error:
        event = BotEvent(query.request_id, "error", code=error.code, message=str(error))
        if request.transport and not request.transport.is_closing():
            await response.write((json.dumps(event.to_dict()) + "\n").encode())
    except (ConnectionResetError, BrokenPipeError):
        pass
    except asyncio.CancelledError:
        raise
    finally:
        try:
            if events is not None:
                await events.aclose()
        finally:
            await manager.release(owner)
    return response
