"""Use the same HTTP bootstrap as browsers; never put credentials in WS URLs."""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit


@asynccontextmanager
async def authenticated_ws(client, url, **options):
    parsed=urlsplit(url)
    base=urlunsplit(('https' if parsed.scheme in {'wss','https'} else 'http', parsed.netloc, '/api/browser-session','',''))
    async with client.post(base) as response:
        if response.status not in (200,404,405):
            raise RuntimeError(f'Browser bootstrap rejected: HTTP {response.status}')
        cookies={name:value.value for name,value in response.cookies.items()}
        await response.read()
    async with client.ws_connect(url,headers={'Cookie':'; '.join(f'{k}={v}' for k,v in cookies.items())} if cookies else {}, **options) as ws:
        yield ws
