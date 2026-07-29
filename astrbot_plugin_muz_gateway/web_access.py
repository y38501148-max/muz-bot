# ruff: noqa: UP006, UP045 -- keep compatibility with bridge-side test runtime.

from __future__ import annotations

import asyncio
import base64
import html
import ipaddress
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from PIL import Image, UnidentifiedImageError

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARS = 12_000
MAX_REDIRECTS = 3
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
CURL_TRANSFER_ARGS = (
    "curl",
    "--silent",
    "--show-error",
    "--http1.1",
    "--proto",
    "=http,https",
    "--max-time",
    "20",
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
QQ_MEDIA_HOST_SUFFIXES = (
    ".qq.com",
    ".qq.com.cn",
    ".qpic.cn",
    ".gtimg.cn",
    ".tencent.com",
    ".tencent.com.cn",
)
_SENSITIVE_QUERY = re.compile(
    r"(?:(?:password|passwd|token|secret|api[_-]?key|cookie|sessionid|"
    r"authorization|验证码|密码|口令)\s*[:：=]\s*\S+|"
    r"(?<![A-Za-z0-9])(?:sk|rk|pk|ghp|github_pat|xox[baprs])"
    r"[-_][A-Za-z0-9_-]{8,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"Bearer\s+[A-Za-z0-9._~+/-]{16,}={0,2}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_SEARCH_INTENT = re.compile(
    r"^\s*(?:(?:请(?:你|帮我)?|麻烦(?:你)?|能否|能帮我|可以)\s*)?"
    r"(?:联网(?:搜索|查询|查一下|查找)|搜索|搜一下|搜一搜|查一下|查找)"
)
_SEARCH_NEGATION = re.compile(
    r"(?:不要|别|无需|不必|不用|禁止|请勿|不想).{0,20}"
    r"(?:联网|搜索|搜|查|最新|新闻)"
)
_LINK_INTENT = re.compile(
    r"(?:打开|访问|读取|分析|查看|看看|总结).{0,12}"
    r"(?:链接|网址|网页|https?://)",
    re.IGNORECASE,
)
_LINK_NEGATION = re.compile(
    r"(?:不要|别|无需|不必|不用|禁止|请勿|不想).{0,30}"
    r"(?:打开|访问|读取|分析|查看|点开|联网|网络请求|请求|抓取|下载|"
    r"链接|网址|网页|URL|https?://)",
    re.IGNORECASE,
)


def _address_is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_global)


async def validate_public_url(
    value: object,
    *,
    allowed_host_suffixes: Optional[Tuple[str, ...]] = None,
) -> str:
    raw = str(value or "").strip()
    if len(raw) > 2_048 or any(ord(character) < 32 for character in raw):
        raise ValueError("URL 长度或字符无效")
    parsed = urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL 必须是无内嵌凭据的 HTTP(S) 公网地址")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL 端口无效") from error
    if port not in {None, 80, 443}:
        raise ValueError("URL 只允许标准 HTTP(S) 端口")
    hostname = parsed.hostname.casefold()
    if allowed_host_suffixes and not any(
        hostname == suffix.lstrip(".") or hostname.endswith(suffix)
        for suffix in allowed_host_suffixes
    ):
        raise ValueError("媒体链接不属于受信任的 QQ CDN")

    loop = asyncio.get_running_loop()
    try:
        addresses = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(
                parsed.hostname,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
        )
    except socket.gaierror as error:
        raise ValueError("URL 域名无法解析") from error
    resolved = {str(item[4][0]) for item in addresses}
    if not resolved or any(not _address_is_public(address) for address in resolved):
        raise ValueError("URL 指向非公网地址，已拒绝访问")
    return parsed._replace(fragment="").geturl()


async def _resolve_public_target(
    value: object,
    *,
    allowed_host_suffixes: Optional[Tuple[str, ...]] = None,
) -> Tuple[str, str, int, str]:
    url = await validate_public_url(
        value,
        allowed_host_suffixes=allowed_host_suffixes,
    )
    parsed = urlparse(url)
    hostname = str(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    addresses = await loop.run_in_executor(
        None,
        lambda: socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        ),
    )
    public_addresses = sorted(
        {str(item[4][0]) for item in addresses if _address_is_public(str(item[4][0]))},
        key=lambda address: (":" in address, address),
    )
    if not public_addresses:
        raise ValueError("URL 没有可用的公网地址")
    return url, hostname, port, public_addresses[0]


def _proxy() -> Optional[str]:
    value = os.getenv("ASTRBOT_WEB_PROXY", "").strip()
    return value or None


def extract_user_urls(value: object, limit: int = 3) -> List[str]:
    result = []
    for match in _URL_PATTERN.findall(str(value or "")):
        url = match.rstrip(".,!?;:，。！？；：、'\"")
        parsed = urlparse(url)
        query = parsed.query.casefold()
        if _SENSITIVE_QUERY.search(url) or any(
            name in query
            for name in ("token=", "key=", "secret=", "password=", "code=")
        ):
            continue
        if url not in result:
            result.append(url[:2_048])
        if len(result) >= limit:
            break
    return result


def should_fetch_user_urls(value: object, *, directed: bool) -> bool:
    text = str(value or "")
    if _LINK_NEGATION.search(text):
        return False
    return bool(directed or _LINK_INTENT.search(text))


def safe_search_subject(value: object) -> str:
    text = _URL_PATTERN.sub(" ", str(value or ""))
    text = " ".join(text.split())
    if (
        not _SEARCH_INTENT.search(text)
        or _SEARCH_NEGATION.search(text)
        or _SENSITIVE_QUERY.search(text)
    ):
        return ""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[已省略]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[已省略]", text)
    text = re.sub(r"(?<!\d)\d{15}(?:\d{2}[\dXx])?(?!\d)", "[已省略]", text)
    text = re.sub(r"(?<!\d)\d{13,19}(?!\d)", "[已省略]", text)
    return text[:200]


async def _get_public_response(
    url: object,
    *,
    max_bytes: int,
    allowed_host_suffixes: Optional[Tuple[str, ...]] = None,
) -> Tuple[httpx.Response, bytes]:
    current = await validate_public_url(
        url,
        allowed_host_suffixes=allowed_host_suffixes,
    )
    async with httpx.AsyncClient(
        proxy=_proxy(),
        timeout=httpx.Timeout(20, connect=10),
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream(
                "GET", current, follow_redirects=False
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("网页重定向缺少目标")
                    current = await validate_public_url(
                        urljoin(current, location),
                        allowed_host_suffixes=allowed_host_suffixes,
                    )
                    continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    raise ValueError(f"网页返回 HTTP {response.status_code}") from error
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise ValueError("远程内容超过大小限制")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ValueError("远程内容超过大小限制")
                return response, bytes(content)
        raise ValueError("网页重定向次数过多")


@dataclass(frozen=True)
class _PinnedResponse:
    url: str
    status_code: int
    headers: dict
    content: bytes


def _parse_curl_headers(value: bytes) -> Tuple[int, dict]:
    blocks = re.split(rb"\r?\n\r?\n", value)
    response_block = next(
        (block for block in reversed(blocks) if block.startswith(b"HTTP/")),
        b"",
    )
    lines = response_block.decode("iso-8859-1", errors="replace").splitlines()
    if not lines:
        raise ValueError("网页响应头无效")
    try:
        status_code = int(lines[0].split()[1])
    except (IndexError, ValueError) as error:
        raise ValueError("网页状态码无效") from error
    headers = {}
    for line in lines[1:]:
        name, separator, header_value = line.partition(":")
        if separator:
            headers[name.strip().casefold()] = header_value.strip()
    return status_code, headers


async def _curl_pinned_fetch(
    url: object,
    *,
    max_bytes: int,
    allowed_host_suffixes: Optional[Tuple[str, ...]] = None,
) -> _PinnedResponse:
    current = str(url)
    for _ in range(MAX_REDIRECTS + 1):
        current, hostname, port, address = await _resolve_public_target(
            current,
            allowed_host_suffixes=allowed_host_suffixes,
        )
        connect_address = f"[{address}]" if ":" in address else address
        with tempfile.TemporaryDirectory(prefix="muz-fetch-") as directory:
            header_path = Path(directory) / "headers"
            body_path = Path(directory) / "body"
            command = [
                *CURL_TRANSFER_ARGS,
                "--max-filesize",
                str(max_bytes),
                "--connect-to",
                f"{hostname}:{port}:{connect_address}:{port}",
                "--dump-header",
                str(header_path),
                "--output",
                str(body_path),
            ]
            proxy = _proxy()
            if proxy:
                command.extend(["--proxy", proxy])
            command.append(current)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise ValueError("网页读取超时")
            except asyncio.CancelledError:
                process.kill()
                await asyncio.shield(process.communicate())
                raise
            if process.returncode != 0:
                detail = stderr.decode(errors="replace")[-300:].strip()
                raise ValueError(f"网页读取失败：{detail or 'curl error'}")
            loop = asyncio.get_running_loop()
            raw_headers, content = await asyncio.gather(
                loop.run_in_executor(None, header_path.read_bytes),
                loop.run_in_executor(None, body_path.read_bytes),
            )
        if len(content) > max_bytes:
            raise ValueError("远程内容超过大小限制")
        status_code, headers = _parse_curl_headers(raw_headers)
        if status_code in {301, 302, 303, 307, 308}:
            location = headers.get("location")
            if not location:
                raise ValueError("网页重定向缺少目标")
            current = urljoin(current, location)
            continue
        if status_code >= 400:
            raise ValueError(f"网页返回 HTTP {status_code}")
        return _PinnedResponse(
            url=current,
            status_code=status_code,
            headers=headers,
            content=content,
        )
    raise ValueError("网页重定向次数过多")


class _ReadableTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: List[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text)
            if self._in_title:
                self.title = f"{self.title} {text}".strip()


def html_to_readable_text(value: str) -> Tuple[str, str]:
    parser = _ReadableTextParser()
    parser.feed(value)
    text = re.sub(r"[ \t]+", " ", " ".join(parser.parts))
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return parser.title[:300], text[:MAX_PAGE_CHARS]


async def fetch_web_page(url: object) -> str:
    response = await _curl_pinned_fetch(
        url,
        max_bytes=MAX_PAGE_BYTES,
    )
    content_type = response.headers.get("content-type", "").casefold()
    if not any(
        allowed in content_type
        for allowed in ("text/", "application/json", "application/xhtml+xml")
    ):
        raise ValueError("链接不是可分析的文本网页")
    charset_match = re.search(r"charset=([\w-]+)", content_type)
    encoding = charset_match.group(1) if charset_match else "utf-8"
    decoded = response.content.decode(encoding, errors="replace")
    if "html" in content_type or "<html" in decoded[:500].casefold():
        title, text = html_to_readable_text(decoded)
    else:
        title, text = "", decoded[:MAX_PAGE_CHARS]
    return f"URL: {response.url}\n标题: {title}\n正文:\n{text}".strip()


def parse_duckduckgo_results(value: str, limit: int = 5) -> List[dict]:
    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>'
        r"(.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    results = []
    for raw_url, raw_title in pattern.findall(value):
        url = html.unescape(raw_url)
        parsed = urlparse(url)
        if parsed.netloc.endswith("duckduckgo.com"):
            url = unquote(parse_qs(parsed.query).get("uddg", [url])[0])
        title = re.sub(r"<[^>]+>", "", html.unescape(raw_title)).strip()
        if title and url.startswith(("http://", "https://")):
            results.append({"title": title[:300], "url": url[:2_048]})
        if len(results) >= limit:
            break
    return results


def _decode_bing_url(value: str) -> str:
    parsed = urlparse(html.unescape(value))
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if encoded.startswith("a1"):
        payload = encoded[2:]
        payload += "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload).decode()
        except (ValueError, UnicodeDecodeError):
            return value
        if decoded.startswith(("http://", "https://")):
            return decoded
    return value


def parse_bing_results(value: str, limit: int = 5) -> List[dict]:
    results = []
    blocks = re.findall(
        r'<li class="b_algo"[^>]*>(.*?)</li>',
        value,
        re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        heading = re.search(
            r"<h2[^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not heading:
            continue
        snippet_match = re.search(
            r"<p[^>]*>(.*?)</p>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        title = re.sub(r"<[^>]+>", "", html.unescape(heading.group(2))).strip()
        snippet = (
            re.sub(
                r"<[^>]+>",
                "",
                html.unescape(snippet_match.group(1)),
            ).strip()
            if snippet_match
            else ""
        )
        url = _decode_bing_url(heading.group(1))
        if title and url.startswith(("http://", "https://")):
            results.append(
                {
                    "title": title[:300],
                    "url": url[:2_048],
                    "snippet": " ".join(snippet.split())[:800],
                }
            )
        if len(results) >= limit:
            break
    return results


def parse_bing_rss_results(value: str, limit: int = 5) -> List[dict]:
    results = []
    for block in re.findall(
        r"<item>(.*?)</item>",
        value,
        re.IGNORECASE | re.DOTALL,
    ):
        title_match = re.search(
            r"<title>(.*?)</title>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        url_match = re.search(
            r"<link>(.*?)</link>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        snippet_match = re.search(
            r"<description>(.*?)</description>",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match or not url_match:
            continue
        title = re.sub(
            r"<[^>]+>",
            "",
            html.unescape(title_match.group(1)),
        ).strip()
        url = html.unescape(url_match.group(1)).strip()
        snippet = (
            re.sub(
                r"<[^>]+>",
                "",
                html.unescape(snippet_match.group(1)),
            ).strip()
            if snippet_match
            else ""
        )
        if title and url.startswith(("http://", "https://")):
            results.append(
                {
                    "title": title[:300],
                    "url": url[:2_048],
                    "snippet": " ".join(snippet.split())[:800],
                }
            )
        if len(results) >= limit:
            break
    return results


async def search_web(query: object) -> str:
    normalized = " ".join(str(query or "").split())[:200]
    if not normalized:
        raise ValueError("搜索词不能为空")
    search_url = (
        f"https://www.bing.com/search?q={quote_plus(normalized)}&ensearch=1&format=rss"
    )
    content = b""
    last_error = None
    for _ in range(2):
        try:
            _, content = await _get_public_response(
                search_url,
                max_bytes=MAX_PAGE_BYTES,
            )
            break
        except httpx.RequestError as error:
            last_error = error
    if not content:
        raise ValueError("搜索服务连接失败") from last_error
    results = parse_bing_rss_results(
        content.decode("utf-8", errors="replace"),
        limit=5,
    )
    if not results:
        raise ValueError("搜索服务没有返回可用结果")
    return "\n".join(
        f"{index}. {item['title']}\n{item['snippet']}\n{item['url']}"
        for index, item in enumerate(results, start=1)
    )


async def download_public_video(url: object, target: Path) -> None:
    response = await _curl_pinned_fetch(
        url,
        max_bytes=25 * 1024 * 1024,
        allowed_host_suffixes=QQ_MEDIA_HOST_SUFFIXES,
    )
    content_type = response.headers.get("content-type", "").casefold()
    if not content_type.startswith("video/"):
        raise ValueError("媒体链接不是视频")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(response.content)


async def download_public_image(url: object, target: Path) -> None:
    response = await _curl_pinned_fetch(
        url,
        max_bytes=10 * 1024 * 1024,
        allowed_host_suffixes=QQ_MEDIA_HOST_SUFFIXES,
    )
    content_type = response.headers.get("content-type", "").casefold()
    if not content_type.startswith("image/"):
        raise ValueError("媒体链接不是图片")
    descriptor = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(response.content)


async def download_public_file(url: object, target: Path) -> None:
    response = await _curl_pinned_fetch(
        url,
        max_bytes=20 * 1024 * 1024,
        allowed_host_suffixes=QQ_MEDIA_HOST_SUFFIXES,
    )
    content_type = response.headers.get("content-type", "").casefold()
    if content_type.startswith(("text/html", "application/xhtml+xml")):
        raise ValueError("文件链接返回了网页而不是文件")
    descriptor = os.open(
        str(target),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(response.content)


def _validate_image_dimensions(width: int, height: int, frames: int) -> None:
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS or frames != 1:
        raise ValueError("图片尺寸过大或不是受支持的单帧图片")


def validate_public_image_file(path: Path) -> None:
    try:
        with Image.open(path) as image:
            _validate_image_dimensions(
                int(image.width),
                int(image.height),
                int(getattr(image, "n_frames", 1)),
            )
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise ValueError("图片文件无法识别") from error
