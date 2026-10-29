#!/usr/bin/env python3
"""Recorder validation through a private headless Chrome client.

Injects a fixture WAV as the browser microphone via a getUserMedia patch,
drives a live call against the server, clicks a latency tag at first
playback and exports the recording. Cross-checks scheduled PCM WAV against
AudioContext graph onset using one absolute PCM16 threshold. This does not
measure a physical microphone/speaker or human reaction time.
"""
import base64
import argparse
import json
import os
import struct
import subprocess
import tempfile
import sys
import time
import urllib.request
import wave
from pathlib import Path

import websocket

os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
_CDP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from scripts.signal_metrics import first_onset, last_onset, bursts, reconciliation, SIGNAL_THRESHOLD_PCM16
FIXTURE = ROOT / "tests" / "fixtures" / "en" / "en_pause300_16k.wav"
URL = "https://aimaoren.com/"
AUTH = None  # Loaded only from the ignored auth file; never embed credentials.
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEBUG_PORT = 9223
DL_DIR = Path("/tmp/rtv-recording-test")
USER_DIR = "/tmp/chrome-rtv-profile"
PROFILE = None


def http_json(path):
    with _CDP_OPENER.open(f"http://localhost:{DEBUG_PORT}{path}", timeout=5) as response:
        return json.loads(response.read().decode())


class Cdp:
    def __init__(self, ws_url, fulfill=None):
        self.ws = websocket.create_connection(ws_url, timeout=5, max_size=64 * 1024 * 1024)
        self.next_id = 0
        self.fulfill = fulfill or []

    def send(self, method, params=None):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params or {}}))
        return self.next_id

    def wait_for(self, want_id, timeout=30, what=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if message.get("id") == want_id:
                if "error" in message:
                    raise RuntimeError(f"{what}: {message['error']}")
                return message.get("result", {})
            self.on_event(message)
        raise TimeoutError(f"cdp response {want_id} ({what})")

    def on_event(self, message):
        method = message.get("method")
        if method == "Fetch.requestPaused":
            params = message["params"]
            print("  [fetch paused]", params.get("request", {}).get("url", "")[:80],
                  "auth" if params.get("authChallenge") else "")
            if params.get("authChallenge"):
                self._provide_auth(params["requestId"])
            else:
                url = params.get("request", {}).get("url", "")
                body = next((b64 for needle, b64 in self.fulfill if needle in url), None)
                if body is not None:
                    self.send("Fetch.fulfillRequest", {
                        "requestId": params["requestId"],
                        "responseCode": 200,
                        "responseHeaders": [{"name": "Content-Type", "value": "application/javascript"}],
                        "body": body,
                    })
                else:
                    self.send("Fetch.continueRequest", {"requestId": params["requestId"]})
        elif method == "Fetch.authRequired":
            print("  [auth required]", message["params"].get("requestId", ""))
            self._provide_auth(message["params"]["requestId"])
        elif method:
            print("  [event]", method)

    def _provide_auth(self, request_id):
        if AUTH is None:
            raise RuntimeError("Trial authentication requires --auth-file")
        self.send("Fetch.continueWithAuth", {
            "requestId": request_id,
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": AUTH[0],
                "password": AUTH[1],
            },
        })

    def eval(self, expression, await_promise=False, timeout=30):
        result = self.wait_for(self.send("Runtime.evaluate", {
            "expression": expression, "awaitPromise": await_promise,
            "returnByValue": True,
        }), timeout=timeout)
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:400])
        return result.get("result", {}).get("value")


def launch_chrome():
    global USER_DIR, DEBUG_PORT, PROFILE
    DL_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE = tempfile.TemporaryDirectory(prefix="rtv-recorder-browser-")
    USER_DIR = PROFILE.name
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        f"--user-data-dir={USER_DIR}", "--mute-audio", "--window-size=1400,1000",
        "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        "--no-first-run", "--disable-session-crashed-bubble",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            DEBUG_PORT = int((Path(USER_DIR) / "DevToolsActivePort").read_text().splitlines()[0])
            http_json("/json/version")
            return proc
        except Exception:
            time.sleep(0.3)
    proc.terminate()
    proc.wait(timeout=10)
    PROFILE.cleanup()
    raise RuntimeError("chrome did not start")


WORKLET_TEMPLATE = """
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.frameSize = Math.round(sampleRate / 10);
    this.frameStart = null;
    this.captureSequence = 0;
    const b64chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
    const b64 = '__FIXTURE_B64__';
    const bytes = new Uint8Array(b64.length * 3 / 4);
    let out = 0;
    for (let i = 0; i < b64.length; i += 4) {
      const c0 = b64chars.indexOf(b64[i]);
      const c1 = b64chars.indexOf(b64[i + 1]);
      const c2 = b64[i + 2] === '=' ? 0 : b64chars.indexOf(b64[i + 2]);
      const c3 = b64[i + 3] === '=' ? 0 : b64chars.indexOf(b64[i + 3]);
      const n = (c0 << 18) | (c1 << 12) | (c2 << 6) | c3;
      bytes[out] = (n >> 16) & 255; out += 1;
      if (b64[i + 2] !== '=') { bytes[out] = (n >> 8) & 255; out += 1; }
      if (b64[i + 3] !== '=') { bytes[out] = n & 255; out += 1; }
    }
    const pcm = new Int16Array(bytes.buffer, 0, out >> 1);
    this.fixture = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i += 1) this.fixture[i] = pcm[i] / 32768;
    this.rendered = 0;
    this.playing = false;
    this.port.onmessage = ({data}) => {
      if (data.type === 'fixture_start') { this.rendered = 0; this.playing = true; }
    };
  }
  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    const step = 16000 / sampleRate;
    for (let i = 0; i < input.length; i += 1) {
      if (this.frameStart === null) this.frameStart = currentFrame + i;
      const pos = this.playing ? this.rendered * step : -1;
      const idx = Math.floor(pos);
      const frac = pos - idx;
      let fx = 0;
      if (idx >= 0 && idx + 1 < this.fixture.length) fx = this.fixture[idx] * (1 - frac) + this.fixture[idx + 1] * frac;
      else if (idx >= 0 && idx < this.fixture.length) fx = this.fixture[idx];
      this.samples.push(fx);
      if (this.playing) this.rendered += 1;
      if (this.samples.length === this.frameSize) {
        const start = this.frameStart;
        const frame = new Float32Array(this.samples);
        this.port.postMessage({pcm: frame, captureSequence: this.captureSequence++,
          captureStartCtx: start / sampleRate,
          captureEndCtx: (start + this.frameSize) / sampleRate,
          sampleRate}, [frame.buffer]);
        this.samples = [];
        this.frameStart = null;
      }
    }
    return true;
  }
}
registerProcessor("capture-processor", CaptureProcessor);
"""


def build_injected_worklet() -> str:
    with wave.open(str(FIXTURE), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())
    return WORKLET_TEMPLATE.replace("__FIXTURE_B64__", base64.b64encode(pcm).decode())


def wait_until(cdp, expression, timeout, poll=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval(expression):
            return True
        time.sleep(poll)
    state = cdp.eval("document.querySelector('#sessionState')?.textContent")
    status = cdp.eval("document.querySelector('#status')?.textContent")
    error = cdp.eval("document.querySelector('#errorMessage')?.textContent")
    seen = cdp.eval("window.__rtv ? window.__rtv.events.map(e => e.eventType).slice(-12) : null")
    raise TimeoutError(f"{expression}\n  state={state} status={status} error={error}\n  last events={seen}")


def analyze(wav_path):
    with wave.open(str(wav_path), "rb") as wav:
        if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) != (16000, 2, 1):
            raise ValueError("Recorder evidence must be mono PCM16 at 16000 Hz")
        frames = wav.readframes(wav.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    return samples




def analyze_recording(path):
    log = json.loads(path.read_text())
    base = path.name.removesuffix("-log.json")
    mic = analyze(path.parent / (base + "-mic.wav"))
    robot = analyze(path.parent / (base + "-robot-scheduled.wav"))
    end = last_onset(mic)
    origin = log["clock"]["originCtxTime"]
    users = [r for r in (log.get("latency") or {}).get("replies", [])
             if r.get("sourceSignalEndSequence") is not None]
    scheduled = log.get("replyScheduled", {})
    starts = [(r, scheduled.get(r["replyId"], {}).get("firstSignalCtx")) for r in users]
    starts = [(r, start) for r, start in starts if start is not None]
    first = min(starts, key=lambda pair: pair[1]) if starts else (None, None)
    row, start = first
    # Correlate by replyId before reading WAV; never select the next burst after
    # speech-end, which would hide an early reply during a mid-sentence pause.
    onset = None
    if row:
        frames = [f for f in log["robotFrames"] if f["replyId"] == row["replyId"]]
        if not frames:
            raise ValueError("Scheduled reply has no matching PCM frames")
        lower = max(0, round((min(f["startAtCtx"] for f in frames)-origin)*16000))
        upper = min(len(robot), round((max(min(f["startAtCtx"]+f["samples"]/16000,
                        f.get("stopAtCtx", float("inf"))) for f in frames)-origin)*16000))
        position = first_onset(robot[lower:upper])
        onset = lower+position if position is not None else None
    wav_ms = (onset-end)/16 if onset is not None and end is not None else None
    actual_end_ctx = origin+end/16000 if end is not None else None
    graph_onset = (row or {}).get("graphOnsetCtx")
    corrected = (graph_onset-actual_end_ctx)*1000 if graph_onset is not None and actual_end_ctx is not None else None
    # Split utterances have an earlier per-reply source boundary. Preserve it
    # explicitly instead of comparing it against the complete WAV's final tail.
    per_reply_graph = (row or {}).get("clientParts", {}).get("captureSignalToGraphMs")
    reply_source = (row or {}).get("sourceSignalEndCtx")
    early = [r["replyId"] for r in users if actual_end_ctx is not None and
             r.get("graphOnsetCtx") is not None and r["graphOnsetCtx"] < actual_end_ctx]
    finals = [e for e in log.get("events", []) if e.get("eventType") == "asr_final"]
    errors = [e.get("attributes", {}).get("code", e.get("eventType"))
              for e in log.get("events", []) if e.get("level") == "error"]
    return {"measurement": "signal_end_to_scheduled_pcm_and_audio_graph_not_physical",
            "signalThresholdPcm16": SIGNAL_THRESHOLD_PCM16, "physicalOutput": None,
            "sessionId": log.get("meta", {}).get("sessionId"), "logFile": str(path),
            "wavSignalLatencyMs": wav_ms, "correctedGraphLatencyMs": corrected,
            "perReplyGraphLatencyMs": per_reply_graph,
            "sourceBoundaryDifferenceMs": (actual_end_ctx-reply_source)*1000
                if actual_end_ctx is not None and reply_source is not None else None,
            "wavMinusCorrectedMs": wav_ms-corrected if wav_ms is not None and corrected is not None else None,
            "userReplyCount": len(users), "asrFinalCount": len(finals),
            "earlyGraphReplies": early, "errors": errors, "warnings": log.get("warnings", {}),
            "replies": users, "reconciliation": reconciliation(row) if row else None}


def main():
    global FIXTURE, URL, DL_DIR, AUTH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--analyze-log", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.analyze_log:
        report = analyze_recording(args.analyze_log)
    else:
        URL, FIXTURE = args.url, args.fixture
        DL_DIR = args.download_dir or Path(tempfile.mkdtemp(prefix="rtv-recording-evidence-"))
        if args.auth_file:
            values = dict(line.split(":", 1) for line in args.auth_file.read_text().splitlines() if ":" in line)
            AUTH = (values["Username"].strip(), values["Password"].strip())
        proc = launch_chrome()
        cdp = browser = None
        try:
            page = next(t for t in http_json("/json") if t["type"] == "page")
            request = urllib.request.Request(URL.rstrip('/') + '/app.js')
            if AUTH:
                request.add_header('Authorization', 'Basic ' + base64.b64encode(
                    (AUTH[0]+':'+AUTH[1]).encode()).decode())
            with _CDP_OPENER.open(request, timeout=20) as response:
                app_source = response.read().decode()
            # app.js is a module; expose only this fixture trigger in our
            # intercepted response, never by changing the deployed frontend.
            app_source += '\nwindow.__rtvStartFixture=()=>captureNode.port.postMessage({type:"fixture_start"});\n'
            cdp = Cdp(page["webSocketDebuggerUrl"], fulfill=[
                ("audio-worklet.js", base64.b64encode(build_injected_worklet().encode()).decode()),
                ("/app.js", base64.b64encode(app_source.encode()).decode())])
            browser = Cdp(http_json("/json/version")["webSocketDebuggerUrl"])
            browser.wait_for(browser.send("Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(DL_DIR.resolve())}))
            cdp.wait_for(cdp.send("Page.enable"))
            cdp.wait_for(cdp.send("Runtime.enable"))
            cdp.wait_for(cdp.send("Fetch.enable", {"handleAuthRequests": True,
                "patterns": [{"urlPattern": "*"}]}))
            cdp.wait_for(cdp.send("Page.navigate", {"url": URL}))
            wait_until(cdp, "document.readyState === 'complete' && !!window.ConversationRecorder", 45)
            cdp.eval("document.querySelector('#callButton').click()")
            wait_until(cdp, "window.__rtv?.events.some(e=>e.eventType==='playback_completed')", 60)
            # Start only after the opening is really completed in the browser.
            cdp.eval("window.__rtvStartFixture()")
            with wave.open(str(FIXTURE), "rb") as source:
                duration = source.getnframes()/source.getframerate()
            time.sleep(duration+.5)
            wait_until(cdp, "window.__rtv.events.filter(e=>e.eventType==='playback_completed').length>=2", 60)
            cdp.eval("window.__rtv.recorder.mark('reply_delay','automatic observation, not human reaction',window.__rtv.audioContext)")
            cdp.eval("document.querySelector('#callButton').click()")
            wait_until(cdp, "['Closed','Error'].includes(document.querySelector('#sessionState').textContent)", 30)
            cdp.eval("document.querySelector('#exportRecording').click()")
            deadline = time.monotonic()+20
            while time.monotonic()<deadline:
                paths = sorted(DL_DIR.glob("*-log.json"))
                if paths and (DL_DIR/(paths[-1].name.removesuffix("-log.json")+"-robot-scheduled.wav")).exists():
                    break
                time.sleep(.1)
            else:
                raise RuntimeError("Recorder export did not finish")
            report = analyze_recording(paths[-1])
            report["fixture"] = FIXTURE.name
        finally:
            for connection in (cdp, browser):
                if connection:
                    connection.ws.close()
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            if PROFILE:
                PROFILE.cleanup()
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k != "replies"}, ensure_ascii=False))
    return 0 if report["wavSignalLatencyMs"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
