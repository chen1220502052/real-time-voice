"""Server-only NLS identity and expiring token cache. Never logs credentials."""
import json
import logging
import os
import threading
import time


class NlsFailure(RuntimeError):
    def __init__(self, code, message, *, fatal=False):
        super().__init__(message)
        self.code, self.fatal = code, fatal


class AliyunNlsTokenProvider:
    _cache = {}
    _cache_lock = threading.Lock()

    def __init__(self, access_key_id, access_key_secret, region=None, domain=None,
                 refresh_margin_seconds=None, fetcher=None, clock=None):
        self.access_key_id, self.access_key_secret = access_key_id, access_key_secret
        self.region = region or os.getenv("ALIYUN_NLS_TOKEN_REGION", "cn-shanghai")
        self.domain = domain or os.getenv("ALIYUN_NLS_TOKEN_DOMAIN", "nls-meta.cn-shanghai.aliyuncs.com")
        self.refresh_margin_seconds = (refresh_margin_seconds if refresh_margin_seconds is not None
                                       else int(os.getenv("ALIYUN_NLS_TOKEN_REFRESH_MARGIN_SECONDS", "300")))
        self._fetcher, self._clock = fetcher, clock or time.time

    def cache_key(self):
        return ("access_key", self.access_key_id, self.region, self.domain)

    def get_token(self):
        with self._cache_lock:
            cached = self._cache.get(self.cache_key())
            if cached and self._clock() < cached[1]-self.refresh_margin_seconds:
                return cached[0]
            token, expires = self._fetcher() if self._fetcher else self._fetch_token()
            if not token or expires <= self._clock():
                raise NlsFailure("NLS_TOKEN_INVALID", "NLS 返回无效或已过期的 Token", fatal=True)
            self._cache[self.cache_key()] = (token, float(expires))
            return token

    def _client(self):
        from aliyunsdkcore.client import AcsClient
        return AcsClient(self.access_key_id, self.access_key_secret, self.region,
                         connect_timeout=3, timeout=5, auto_retry=False)

    def _fetch_token(self):
        try:
            from aliyunsdkcore.request import CommonRequest
            client = self._client()
            request = CommonRequest()
            request.set_method("POST")
            request.set_protocol_type("https")
            request.set_domain(self.domain)
            request.set_version("2019-02-28")
            request.set_action_name("CreateToken")
            payload = json.loads(client.do_action_with_exception(request))
            token = payload.get("Token", {})
            return str(token.get("Id", "")), float(token.get("ExpireTime", 0))
        except NlsFailure:
            raise
        except Exception:
            # SDK exception strings may include signed requests or credentials.
            raise NlsFailure("NLS_TOKEN_REFRESH_FAILED",
                             "无法获取 NLS Token，请检查角色的 nls:CreateToken 权限、账号和网络", fatal=True) from None


class EcsRamRoleNlsTokenProvider(AliyunNlsTokenProvider):
    def __init__(self, role_name=None, *, credential_client=None, **kwargs):
        super().__init__("", "", **kwargs)
        self.role_name = role_name or os.getenv("ALIYUN_ECS_RAM_ROLE_NAME", "")
        self._credentials = credential_client

    def cache_key(self):
        return ("ecs_ram_role", self.role_name, self.region, self.domain)

    def _client(self):
        try:
            from aliyunsdkcore.client import AcsClient
            from aliyunsdkcore.auth.credentials import StsTokenCredential
            if self._credentials is None:
                from alibabacloud_credentials.client import Client
                from alibabacloud_credentials.models import Config
                # This SDK logger includes cached credential objects and raw
                # exceptions. The app reports only sanitized NLS reason codes.
                logging.getLogger("credentials").disabled = True
                # Explicit provider: never fall back to env AK/manual Token.
                # Only IMDSv2; do not weaken the instance's metadata protection.
                self._credentials = Client(Config(type="ecs_ram_role", role_name=self.role_name or None,
                    disable_imds_v1=True, connect_timeout=2000, timeout=2000))
            snapshot = self._credentials.get_credential()
            identity = (snapshot.get_access_key_id(), snapshot.get_access_key_secret(), snapshot.get_security_token())
            if not all(identity):
                raise ValueError("incomplete role credential")
            return AcsClient(region_id=self.region, credential=StsTokenCredential(*identity),
                             connect_timeout=3, timeout=5, auto_retry=False)
        except Exception:
            raise NlsFailure("NLS_ROLE_CREDENTIALS_UNAVAILABLE",
                             "无法取得 ECS RAM 角色凭证，请检查角色绑定、角色名称和 IMDSv2 可达性", fatal=True) from None


def token_provider_from_environment():
    mode = os.getenv("ALIYUN_NLS_AUTH_MODE", "auto").lower()
    if mode == "ecs_ram_role":
        return EcsRamRoleNlsTokenProvider()
    if mode not in {"auto", "access_key", "token"}:
        raise NlsFailure("NLS_AUTH_CONFIG_INVALID", "不支持的 NLS 认证模式", fatal=True)
    key, secret = os.getenv("ALIYUN_AK_ID", ""), os.getenv("ALIYUN_AK_SECRET", "")
    if mode != "token" and key and secret:
        return AliyunNlsTokenProvider(key, secret)
    if mode != "access_key" and os.getenv("ALIYUN_NLS_TOKEN"):
        return None
    raise NlsFailure("NLS_AUTH_CONFIG_INVALID", "未配置可用 NLS 身份，请配置 ECS RAM 角色认证", fatal=True)
