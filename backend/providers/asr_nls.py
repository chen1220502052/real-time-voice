"""asr nls provider boundary."""

import asyncio
import json
import os
import queue
import threading
from typing import Optional
from .asr_cloud import DashScopeAsrVadAdapter
from .nls_auth import AliyunNlsTokenProvider, NlsFailure, token_provider_from_environment


def checked_transcriber_factory(nls):
    """v1.0.2 drops transport errors; bridge them without global SDK patches."""
    class CheckedTranscriber(nls.NlsSpeechTranscriber):
        def __init__(self, *, on_transport_error, **kwargs):
            self._transport_error = on_transport_error
            super().__init__(**kwargs)

        def _NlsSpeechTranscriber__tr_core_on_error(self, error, *args):
            self._transport_error(error)
            # The pinned SDK implementation only debug-logs the raw exception.
            # Do not call it: signed headers/tokens must not enter debug logs.
    return CheckedTranscriber


class AliyunNlsAsrVadAdapter(DashScopeAsrVadAdapter):
    """Local FSMN-VAD plus Alibaba Cloud Intelligent Speech (NLS) ASR."""

    # The application protocol stays at 100 ms per frame.  Alibaba's Python
    # SDK recommends 20 ms PCM writes, so split only at this adapter boundary.
    NLS_AUDIO_CHUNK_BYTES = 16_000 * 2 * 20 // 1000

    def __init__(self, device: Optional[str] = None, transcriber_factory=None) -> None:
        super().__init__(device=device, recognition_factory=transcriber_factory)
        self.app_key = os.getenv("ALIYUN_NLS_APP_KEY", "")
        self.token = os.getenv("ALIYUN_NLS_TOKEN", "")
        self.url = os.getenv(
            "ALIYUN_NLS_URL", "wss://nls-gateway-cn-shanghai.aliyuncs.com/ws/v1"
        )
        self._token_provider = None
        # Cloud upload runs on its own thread so a slow websocket write never
        # blocks the VAD/duplex consumption loop (which must stay under the
        # 100 ms packet cadence or the input queue overflows).
        self._send_queue: "queue.Queue" = queue.Queue(maxsize=100)
        self._send_thread: Optional[threading.Thread] = None
        self._send_thread_lock = threading.Lock()
        self._recognition_stop_lock = threading.Lock()
        self._nls_completed = threading.Event()
        self._nls_ready = threading.Event()
        self._callback_epoch = 0

    async def warmup(self) -> None:
        await super().warmup()
        if self._token_provider is not None:
            # Refresh credentials before admitting microphone packets, without
            # opening a billable recognition stream.
            await asyncio.get_running_loop().run_in_executor(None, self._token_provider.get_token)

    def _ensure_dependencies(self) -> None:
        self._ensure_vad()
        if not self.app_key:
            raise RuntimeError("ALIYUN_NLS_APP_KEY is required when ASR_BACKEND=nls")
        if self._token_provider is None:
            self._token_provider = token_provider_from_environment()
        if self._recognition_factory is None:
            try:
                import certifi
                import nls
            except ImportError as exc:
                raise RuntimeError(
                    "Alibaba Cloud NLS SDK is not installed. Install with "
                    "`python3.11 -m pip install -e '.[nls-asr]'`."
                ) from exc
            # websocket-client otherwise uses this Python installation's
            # incomplete system trust path on macOS. Keep verification enabled
            # and explicitly use certifi's CA bundle.
            os.environ.setdefault("WEBSOCKET_CLIENT_CA_BUNDLE", certifi.where())
            self._recognition_factory = checked_transcriber_factory(nls)

    def _start_recognition(self) -> None:
        with self._result_lock:
            self._latest_text = ""
            self._final_text = ""
            self._cloud_error = None
        self._ensure_sender()
        self._nls_completed.clear()
        self._nls_ready.clear()
        self._callback_epoch += 1
        self._send_queue.put_nowait(_NlsStartMarker())

    def _start_recognition_now(self) -> None:
        # Ordered with audio/flush on the sender, never on the 100ms VAD path.
        token = self._token_provider.get_token() if self._token_provider is not None else self.token
        epoch = self._callback_epoch
        def guarded(callback):
            def invoke(*args):
                if epoch == self._callback_epoch:
                    callback(*args)
            return invoke
        callbacks = dict(
            url=self.url,
            token=token,
            appkey=self.app_key,
            on_sentence_begin=guarded(self._on_nls_sentence_begin),
            on_sentence_end=guarded(self._on_nls_sentence_end),
            on_start=guarded(self._on_nls_start),
            on_result_changed=guarded(self._on_nls_result_changed),
            on_completed=guarded(self._on_nls_completed),
            on_error=guarded(self._on_nls_error),
            on_close=guarded(self._on_nls_close),
            callback_args=[],
        )
        # Only the checked production factory accepts this extra callback.
        if hasattr(self._recognition_factory, '_NlsSpeechTranscriber__tr_core_on_error'):
            callbacks['on_transport_error'] = guarded(self._on_transport_error)
        self._recognition = self._recognition_factory(**callbacks)
        self._recognition.start(
            aformat="pcm",
            sample_rate=16_000,
            ch=1,
            enable_intermediate_result=True,
            enable_punctuation_prediction=True,
            enable_inverse_text_normalization=True,
            ex={"max_sentence_silence": max(
                200, min(6000, int(os.getenv("NLS_MAX_SENTENCE_SILENCE_MS", "600")))
            )},
        )
        with self._result_lock:
            if self._cloud_error:
                raise self._cloud_error
            if not self._nls_ready.is_set():
                raise NlsFailure("NLS_NOT_READY", "NLS 任务未就绪，未发送语音；请检查鉴权或连接")

    def _ensure_sender(self) -> None:
        with self._send_thread_lock:
            if self._send_thread is not None and self._send_thread.is_alive():
                return
            self._send_thread = threading.Thread(
                target=self._sender_loop, name="nls-audio-sender", daemon=True
            )
            self._send_thread.start()

    def _sender_loop(self) -> None:
        while True:
            item = self._send_queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, _NlsStartMarker):
                    self._start_recognition_now()
                    continue
                if isinstance(item, _NlsFlushMarker):
                    item.done.set()
                    continue
                self._send_chunks_now(item)
            except Exception as exc:  # surfaced at stop time via _cloud_error
                with self._result_lock:
                    if self._cloud_error is None:
                        self._cloud_error = exc if isinstance(exc, NlsFailure) else NlsFailure(
                            "NLS_UPLOAD_FAILED", "NLS 音频上传失败，请重新连接")
            finally:
                self._send_queue.task_done()

    def _send_chunks_now(self, pcm: bytes) -> None:
        recognition = self._recognition
        with self._result_lock:
            failed = self._cloud_error is not None
        if recognition is None or failed:
            return
        if not self._nls_ready.is_set():
            raise NlsFailure("NLS_NOT_READY", "NLS 任务未就绪，未发送语音")
        for offset in range(0, len(pcm), self.NLS_AUDIO_CHUNK_BYTES):
            chunk = pcm[offset:offset + self.NLS_AUDIO_CHUNK_BYTES]
            if chunk:
                recognition.send_audio(chunk)

    def _send_cloud_audio(self, pcm: bytes) -> None:
        try:
            self._send_queue.put_nowait(bytes(pcm))
        except queue.Full:
            # Never discard a start/flush command or silently corrupt an
            # utterance. Surface a bounded cloud-upload failure instead.
            with self._result_lock:
                self._cloud_error = NlsFailure("NLS_UPLOAD_QUEUE_FULL", "NLS 上传队列已满")
            raise RuntimeError("NLS upload queue full")

    def _flush_pending_audio(self, timeout: float = 5.0) -> None:
        if self._send_thread is None or not self._send_thread.is_alive():
            return
        marker = _NlsFlushMarker()
        self._send_queue.put(marker, timeout=timeout)
        if not marker.done.wait(timeout):
            raise RuntimeError("NLS audio sender flush timed out")

    @staticmethod
    def _parse_nls_message(message) -> dict:
        if isinstance(message, dict):
            return message
        try:
            return json.loads(message)
        except (TypeError, json.JSONDecodeError):
            return {}

    def _update_nls_text(self, message, final: bool = False) -> None:
        payload = self._parse_nls_message(message).get("payload", {})
        text = str(payload.get("result", "")).strip()
        if not text:
            return
        with self._result_lock:
            self._latest_text = text
            if final:
                self._final_text = text

    def _on_nls_sentence_begin(self, message, *args) -> None:
        return None

    def _on_nls_sentence_end(self, message, *args) -> None:
        self._update_nls_text(message, final=True)

    def _on_nls_start(self, message, *args) -> None:
        self._nls_ready.set()

    def _on_nls_result_changed(self, message, *args) -> None:
        self._update_nls_text(message)

    def _on_nls_completed(self, message, *args) -> None:
        self._update_nls_text(message, final=True)
        self._nls_completed.set()

    def _on_nls_error(self, message, *args) -> None:
        event = self._parse_nls_message(message)
        header = event.get("header", {})
        code = header.get("status", "unknown_error")
        detail = str(header.get("status_message") or header.get("message") or "").lower()
        auth = any(word in detail for word in ("token", "access_denied", "appkey", "permission", "privilege"))
        status = f"（{code}）" if isinstance(code, int) and 10000000 <= code <= 99999999 else ""
        with self._result_lock:
            if str(code) == '40000005':
                self._cloud_error = NlsFailure('NLS_CONCURRENCY_LIMIT',
                    '语音识别并发或请求频率已达到阿里云额度，请稍后重试或检查 NLS 服务额度'+status)
                return
            self._cloud_error = NlsFailure("NLS_AUTH_FAILED" if auth else "NLS_SERVICE_FAILED",
                ("NLS 鉴权失败，请检查角色权限、Appkey 和 Token" if auth else "NLS 服务拒绝了识别请求")+status, fatal=auth)

    def _on_transport_error(self, error, *args):
        auth = getattr(error, 'status_code', None) in {401, 403}
        with self._result_lock:
            if self._cloud_error is None:
                self._cloud_error = NlsFailure("NLS_AUTH_FAILED" if auth else "NLS_CONNECTION_FAILED",
                    "NLS 鉴权失败（HTTP 401/403），请检查角色权限和 Token" if auth else "NLS 连接失败，请检查网络和 TLS", fatal=auth)

    def _on_nls_close(self, *args) -> None:
        if not self._nls_completed.is_set():
            with self._result_lock:
                if self._cloud_error is None:
                    self._cloud_error = NlsFailure("NLS_CONNECTION_CLOSED", "NLS 连接在识别完成前关闭，请重新连接")

    def _stop_recognition(self) -> str:
        # Utterance-end must observe every queued frame before the final
        # command, or the cloud would truncate the sentence tail.
        self._flush_pending_audio()
        with self._recognition_stop_lock:
            recognition, self._recognition = self._recognition, None
            if recognition is not None and not self._nls_completed.is_set():
                # Detach before calling the SDK: error/reset cannot send a
                # second StopTranscription for an already stopping task.
                recognition.stop()
        with self._result_lock:
            if self._cloud_error:
                raise self._cloud_error
            return (self._final_text or self._latest_text).strip()

    async def reset(self) -> None:
        try:
            # A pending asynchronous start must also be drained and stopped.
            await asyncio.get_running_loop().run_in_executor(None, self._stop_recognition)
        finally:
            self._callback_epoch += 1
            self._finish_utterance()
            self._vad_cache = {}
            with self._send_thread_lock:
                thread = self._send_thread
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(self._send_queue.put, None, True, 2.0)
                await asyncio.to_thread(thread.join, 2.0)
                if thread.is_alive():
                    self._unusable = True
                    raise RuntimeError("NLS sender did not stop")
            with self._send_thread_lock:
                if self._send_thread is thread:
                    self._send_thread = None
            while not self._send_queue.empty():
                try:
                    self._send_queue.get_nowait()
                    self._send_queue.task_done()
                except queue.Empty:
                    break


class _NlsStartMarker:
    """Start commands share the sender FIFO with their utterance audio."""


class _NlsFlushMarker:
    """Barrier that confirms every audio frame queued before it was sent."""

    def __init__(self) -> None:
        self.done = threading.Event()



