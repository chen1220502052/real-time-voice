"""Drive the real Chrome page over CDP: click 拨通, sample DOM state, collect
console events, then hang up. Usage:
    PYTHONPATH=. python3.11 scripts/cdp_browser_test.py --duration 120
Requires Chrome started with --remote-debugging-port=9222 and the real server
running on localhost:8765. Not part of the default pytest suite.
"""

import argparse
import asyncio
import json
import time

import aiohttp

CDP_HTTP = "http://127.0.0.1:9222"
SERVER_HEALTH = "http://127.0.0.1:8765/health"
SAMPLES_PATH = "/tmp/cdp_states.jsonl"

STATE_EXPR = """({
  state: document.querySelector('#sessionState').textContent,
  status: document.querySelector('#status').textContent,
  user: document.querySelector('#userState').textContent,
  robot: document.querySelector('#robotState').textContent,
  mic: document.querySelector('#micState').textContent,
  micValue: document.querySelector('#micValue').textContent,
  buffer: document.querySelector('#bufferValue').textContent,
  asr: document.querySelector('#metricAsr').textContent,
  llm: document.querySelector('#metricLlm').textContent,
  tts: document.querySelector('#metricTts').textContent,
  audio: document.querySelector('#metricAudio').textContent,
  interrupt: document.querySelector('#metricInterrupt').textContent,
  errors: document.querySelector('#metricErrors').textContent,
  packets: document.querySelector('#packetCounts').textContent,
  msgs: document.querySelector('#messageCount').textContent,
  partial: document.querySelector('#partialTranscript p').textContent,
  partialShown: !document.querySelector('#partialTranscript').hidden,
  mouth: document.querySelector('#cat').className,
  btn: document.querySelector('#callButton').textContent,
  errBanner: !document.querySelector('#errorBanner').hidden,
})"""


class CdpClient:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.next_id = 0
        self.pending = {}
        self.console = []
        self.reader_task = None

    async def start(self) -> None:
        self.reader_task = asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self.pending:
                self.pending.pop(data["id"]).set_result(data)
            elif data.get("method") == "Runtime.consoleAPICalled":
                args = [a.get("value", a.get("description", "")) for a in data["params"].get("args", [])]
                self.console.append({"kind": "console." + data["params"]["type"], "text": " ".join(map(str, args))})
            elif data.get("method") == "Runtime.exceptionThrown":
                d = data["params"]["exceptionDetails"]
                self.console.append({"kind": "exception", "text": d.get("text", "") + " " + str(d.get("exception", {}).get("description", ""))})
            elif data.get("method") == "Log.entryAdded":
                e = data["params"]["entry"]
                self.console.append({"kind": "log." + e.get("level", ""), "text": e.get("text", "")})

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        await self.ws.send_json({"id": self.next_id, "method": method, "params": params or {}})
        reply = await asyncio.wait_for(future, timeout=30)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    async def evaluate(self, expression: str):
        result = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")


async def wait_server_healthy(timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession(trust_env=False) as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(SERVER_HEALTH, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if (await resp.json()).get("ok"):
                        return
            except Exception:
                pass
            await asyncio.sleep(3)
    raise TimeoutError("server did not become healthy")


async def find_page_ws() -> str:
    async with aiohttp.ClientSession(trust_env=False) as session:
        async with session.get(CDP_HTTP + "/json") as resp:
            targets = await resp.json()
    pages = [t for t in targets if t.get("type") == "page" and "localhost:8765" in t.get("url", "")]
    if not pages:
        raise RuntimeError("no Chrome page on localhost:8765; open http://localhost:8765 in the CDP Chrome")
    return pages[0]["webSocketDebuggerUrl"]


async def run(duration: float) -> dict:
    await wait_server_healthy()
    ws_url = await find_page_ws()
    async with aiohttp.ClientSession(trust_env=False) as session:
        ws = await session.ws_connect(ws_url)
        cdp = CdpClient(ws)
        await cdp.start()
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.call("Page.enable")
        # Page may have loaded before the server was ready; reload for a clean check.
        await cdp.call("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(3)

        t0 = time.monotonic()
        samples = []

        async def record(label: str, state) -> None:
            sample = {"t": round(time.monotonic() - t0, 1), "label": label, "state": state}
            samples.append(sample)
            with open(SAMPLES_PATH, "a") as fh:
                fh.write(json.dumps(sample, ensure_ascii=False) + "\n")

        pre = await cdp.evaluate(STATE_EXPR)
        await record("after_reload", pre)
        if "服务不可用" in str(await cdp.evaluate("document.querySelector('#serviceBadge').textContent")):
            raise RuntimeError("page still reports 服务不可用 after reload")

        await cdp.evaluate("document.querySelector('#callButton').click()")
        await asyncio.sleep(1.0)
        await record("after_call_click", await cdp.evaluate(STATE_EXPR))

        deadline = time.monotonic() + duration
        last_mouth = ""
        while time.monotonic() < deadline:
            state = await cdp.evaluate(STATE_EXPR)
            if state["mouth"] != last_mouth:
                last_mouth = state["mouth"]
                await record("mouth", state)
            elif state["state"] not in ("Active",) or state["partialShown"] or state["robot"] != "Idle":
                await record("active", state)
            await asyncio.sleep(0.5)
        await record("final", await cdp.evaluate(STATE_EXPR))

        # Hang up and confirm closure.
        await cdp.evaluate("document.querySelector('#callButton').click()")
        await asyncio.sleep(2.5)
        await record("after_hangup", await cdp.evaluate(STATE_EXPR))

        errors = [c for c in cdp.console if c["kind"] in ("exception", "log.error", "console.error", "console.warn", "log.warning")]
        return {"samples": samples, "console_problems": errors[:50], "console_total": len(cdp.console)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120.0, help="seconds to keep the call open")
    args = parser.parse_args()
    open(SAMPLES_PATH, "w").close()
    summary = asyncio.run(run(args.duration))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
