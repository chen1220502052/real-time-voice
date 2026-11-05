"""Key-free Ollama adapter for local curriculum chat and writing."""
import asyncio
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from contextlib import asynccontextmanager

from .llm_runtime import llm_client, timing, local_inference_gate


def enabled(purpose):
    return (os.getenv(purpose.upper() + '_PROVIDER') or os.getenv('LLM_PROVIDER', 'deepseek')).lower() == 'ollama'


def endpoint():
    url = os.getenv('OLLAMA_NATIVE_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('Local Ollama must use a loopback address')
    return url + '/api/chat'


async def stream_reply(messages, *, purpose='chat', json_mode=False, max_tokens=256, timeout=90):
    gate=local_inference_gate()
    @asynccontextmanager
    async def admitted():
        if gate is None:
            yield
        else:
            async with gate.request(purpose) as wait:
                if timing():timing().metrics['localAdmissionWaitMs']=round(wait,3)
                yield
    async with admitted():
        source=_stream_reply(messages,purpose=purpose,json_mode=json_mode,max_tokens=max_tokens,timeout=timeout)
        try:
            async for part in source:yield part
        finally:
            await source.aclose()


async def _stream_reply(messages, *, purpose, json_mode, max_tokens, timeout):
    payload = {
        'model': (os.getenv(purpose.upper() + '_MODEL')
                  or ('qwen2.5:3b' if purpose == 'voice_chat' else None)
                  or os.getenv('OLLAMA_MODEL') or 'qwen3.5:9b'),
        'messages': messages, 'stream': True,
        'think': purpose == 'writing' and os.getenv('OLLAMA_WRITING_THINK', '0') == '1',
        'keep_alive': os.getenv('OLLAMA_KEEP_ALIVE','10m'),
        'options': {'temperature': .3, 'num_ctx': int(os.getenv('VOICE_CHAT_NUM_CTX','4096')) if purpose=='voice_chat' else 8192,
                    'num_predict': max_tokens},
    }
    if json_mode:
        payload['format'] = json_mode if isinstance(json_mode, dict) else 'json'
    probe=timing()
    if probe:
        probe.mark('providerRequestMs')
        probe.metrics.update(promptChars=sum(len(m['content']) for m in messages),
            systemPromptChars=sum(len(m['content']) for m in messages if m['role']=='system'),
            historyMessages=sum(m['role']!='system' for m in messages),connectionReused=False)
    finished = False
    async with asyncio.timeout(timeout):
        async with llm_client() as client:
            async with client.post(endpoint(), json=payload, allow_redirects=False,trace_request_ctx=probe) as response:
                if probe:probe.mark('providerHeadersMs')
                if response.status != 200:
                    raise RuntimeError('本地模型未就绪，请确认 Ollama 正在运行且模型已下载。')
                async for line in response.content:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if item.get('error'):
                        raise RuntimeError('本地模型生成失败，请稍后重试。')
                    content = item.get('message', {}).get('content', '')
                    if not isinstance(content, str):
                        raise ValueError('Invalid local model content')
                    if content:
                        if probe:probe.mark('providerFirstTokenMs')
                        yield content
                    if item.get('done'):
                        if probe:
                            for key,label in [('load_duration','modelLoadMs'),('prompt_eval_duration','promptEvalMs'),
                                              ('eval_duration','tokenGenerationMs')]:
                                if isinstance(item.get(key),(int,float)):probe.metrics[label]=round(item[key]/1e6,3)
                            for key,label in [('prompt_eval_count','promptTokens'),('prompt_eval_cached_count','cachedPromptTokens'),
                                              ('eval_count','generatedTokens')]:
                                if isinstance(item.get(key),int):probe.metrics[label]=item[key]
                        if item.get('done_reason') not in (None, 'stop'):
                            raise ValueError('Local model response was truncated')
                        finished = True
                        break
    if not finished:
        raise ValueError('Local model stream ended before completion')


async def complete(messages, **options):
    return ''.join([part async for part in stream_reply(messages, **options)])


class LocalAgent:
    def __init__(self, prompt, *, purpose='chat', json_mode=False, max_tokens=256):
        self.prompt = prompt
        self.options = dict(purpose=purpose, json_mode=json_mode, max_tokens=max_tokens)

    def messages(self, state):
        messages = list(state['messages'])
        if self.options['purpose'] == 'writing' and len(messages) > 1:
            try:
                feedback = json.loads(messages[-1]['content'])
                audit = feedback['audit']
                words = ', '.join(item['word'] for item in audit['outsideWords'])
                instruction = (
                    f"Edit the previous draft into natural, coherent English about the SAME topic. "
                    f"The body has {audit['wordCount']} words. Target: {audit['policy']['targetWords']} words, "
                    "with 20% tolerance. "
                    + ("The draft passed vocabulary and length checks. Keep its good content; improve only genuine problems. "
                       if audit['passed'] else
                       f"The draft FAILED. Rewrite it; do not just add glossary entries. Avoid these outside words: {words}. "
                       "Replace entire ideas with simple related descriptions if necessary, using the supplied vocabulary. ")
                    + "Return the complete JSON: title, essay, glossary. Do not reuse an invalid draft. "
                    "Provide Chinese meanings in glossary for any remaining outside words, exactly matching those words."
                )
                messages[-1] = {'role': 'user', 'content': instruction}
            except (ValueError, KeyError, TypeError):
                pass
        return [{'role': 'system', 'content': self.prompt}, *messages]

    async def astream(self, state, config=None, stream_mode=None):
        source = stream_reply(self.messages(state), **self.options)
        try:
            async for content in source:
                yield SimpleNamespace(content=content), {'langgraph_node': 'model'}
        finally:
            await source.aclose()

    async def ainvoke(self, state, config=None):
        content = await complete(self.messages(state), **self.options)
        return {'messages': [*state['messages'], {'role': 'assistant', 'content': content}]}
