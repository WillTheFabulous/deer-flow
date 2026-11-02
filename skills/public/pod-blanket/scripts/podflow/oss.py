"""阿里云 OSS 的最小客户端：put / put_file / get / head / delete / 签名链接。

签名是 OSS V1（HMAC-SHA1，与 Mooya 用的 oss2.Auth 同一套，同一个桶已实测可用）；不引 oss2，只用 httpx。
请求都经 `http.make_client`，测试里会被整体替换成假的传输层。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import threading
import time
from collections.abc import Iterator
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from . import http

_CHUNK = 64 * 1024
# put_file 从磁盘读一块发一块：几百 MB 的交付包不整个读进内存
_FILE_CHUNK = 1024 * 1024


def content_disposition(filename: str, fallback: str | None = None) -> str:
    """下载用的 Content-Disposition：纯 ASCII 文件名照写；带中文时 filename 给 ASCII 兜底名（fallback，
    不给就把非 ASCII 字符去掉），filename* 给 UTF-8 原名（RFC 5987，浏览器优先用它），整串仍是 ASCII，可以直接签名。"""
    if filename.isascii():
        return f'attachment; filename="{filename}"'
    if not fallback or not fallback.isascii():
        fallback = re.sub(r"[^\x20-\x7e]+", "", filename)
        fallback = re.sub(r"[_\s]+(?=\.[^.]*$)|[_\s]+$", "", fallback) or "download"
    fallback = fallback.replace('"', "")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


class OSSError(RuntimeError):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"OSS {status} {code}{'：' + message if message else ''}")
        self.status = status
        self.code = code


class _Pacer:
    """进程内共享的上传限速：几个线程同时上传时，总速率也不超过上限。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, nbytes: int, rate_bps: float) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + nbytes / rate_bps
        if start > now:
            time.sleep(start - now)


_PACER = _Pacer()


def _error(resp: httpx.Response) -> OSSError:
    code = resp.headers.get("x-oss-ec", "")
    message = ""
    if resp.content:
        found = re.search(r"<Code>(.*?)</Code>", resp.text)
        code = found.group(1) if found else code
        found = re.search(r"<Message>(.*?)</Message>", resp.text)
        message = found.group(1) if found else ""
    return OSSError(resp.status_code, code or "Error", message)


class OSSClient:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str,
        access_key_id: str,
        access_key_secret: str,
        public_endpoint: str | None = None,
        timeout: float = 120.0,
        upload_rate_kbps: float = 0,
    ):
        self.bucket = bucket
        self.timeout = timeout
        self._ak = access_key_id
        self._sk = access_key_secret.encode("utf-8")
        self._rate_bps = float(upload_rate_kbps or 0) * 1024
        self._scheme, self._host = self._split(endpoint)
        self._public_scheme, self._public_host = self._split(
            public_endpoint or endpoint
        )

    def _split(self, endpoint: str) -> tuple[str, str]:
        parts = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
        return parts.scheme or "https", f"{self.bucket}.{parts.netloc}"

    def _url(self, key: str, *, public: bool = False) -> str:
        scheme, host = (
            (self._public_scheme, self._public_host)
            if public
            else (self._scheme, self._host)
        )
        return f"{scheme}://{host}/{quote(key, safe='/')}"

    def sign(
        self,
        verb: str,
        key: str,
        *,
        date: str,
        content_md5: str = "",
        content_type: str = "",
        oss_headers: dict[str, str] | None = None,
    ) -> str:
        canonical = "".join(
            f"{k}:{v}\n"
            for k, v in sorted(
                (k.lower(), str(v).strip()) for k, v in (oss_headers or {}).items()
            )
        )
        to_sign = f"{verb}\n{content_md5}\n{content_type}\n{date}\n{canonical}/{self.bucket}/{key}"
        return base64.b64encode(
            hmac.new(self._sk, to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode()

    def _paced(self, data: bytes) -> Iterator[bytes]:
        for i in range(0, len(data), _CHUNK):
            chunk = data[i : i + _CHUNK]
            _PACER.wait(len(chunk), self._rate_bps)
            yield chunk

    def _file_chunks(self, path: Path) -> Iterator[bytes]:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_FILE_CHUNK), b""):
                if self._rate_bps > 0:
                    _PACER.wait(len(chunk), self._rate_bps)
                yield chunk

    def _request(
        self,
        verb: str,
        key: str,
        *,
        body: bytes | None = None,
        file: Path | None = None,
        content_type: str = "",
        oss_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        date = formatdate(usegmt=True)
        headers: dict[str, str] = {"Date": date, **(oss_headers or {})}
        content_md5 = ""
        content: Any = None
        if file is not None:
            digest = hashlib.md5()
            size = 0
            with file.open("rb") as fh:
                for chunk in iter(lambda: fh.read(_FILE_CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
            content_md5 = base64.b64encode(digest.digest()).decode()
            headers.update(
                {
                    "Content-MD5": content_md5,
                    "Content-Type": content_type,
                    "Content-Length": str(size),
                }
            )
            content = self._file_chunks(file)
        elif body is not None:
            content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
            headers.update(
                {
                    "Content-MD5": content_md5,
                    "Content-Type": content_type,
                    "Content-Length": str(len(body)),
                }
            )
            # 给了 Content-Length，httpx 就按普通请求体流式发送（不会改成 chunked）
            content = self._paced(body) if self._rate_bps > 0 else body
        signature = self.sign(
            verb,
            key,
            date=date,
            content_md5=content_md5,
            content_type=content_type if body is not None or file is not None else "",
            oss_headers=oss_headers,
        )
        headers["Authorization"] = f"OSS {self._ak}:{signature}"
        with http.make_client(self.timeout) as client:
            return client.request(
                verb, self._url(key), headers=headers, content=content
            )

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        meta: dict[str, str] | None = None,
        forbid_overwrite: bool = True,
    ) -> None:
        oss_headers = {f"x-oss-meta-{k}": v for k, v in (meta or {}).items()}
        if forbid_overwrite:
            oss_headers["x-oss-forbid-overwrite"] = "true"
        resp = self._request(
            "PUT", key, body=data, content_type=content_type, oss_headers=oss_headers
        )
        if resp.status_code != 200:
            raise _error(resp)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str = "application/octet-stream",
        forbid_overwrite: bool = True,
    ) -> None:
        """从磁盘流式上传一个文件（单次 PUT，OSS 上限 5GB）。"""
        oss_headers = {"x-oss-forbid-overwrite": "true"} if forbid_overwrite else {}
        resp = self._request(
            "PUT", key, file=path, content_type=content_type, oss_headers=oss_headers
        )
        if resp.status_code != 200:
            raise _error(resp)

    def get(self, key: str) -> bytes:
        resp = self._request("GET", key)
        if resp.status_code != 200:
            raise _error(resp)
        return resp.content

    def head(self, key: str) -> dict[str, str] | None:
        resp = self._request("HEAD", key)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise _error(resp)
        return {k.lower(): v for k, v in resp.headers.items()}

    def delete(self, key: str) -> None:
        resp = self._request("DELETE", key)
        if resp.status_code not in (200, 204, 404):
            raise _error(resp)

    def sign_url(
        self,
        key: str,
        expires_seconds: int = 3600,
        *,
        filename: str | None = None,
        fallback: str | None = None,
    ) -> str:
        """公网 endpoint 的限时 GET 链接（浏览器、飞书可直接打开）。

        给了 filename 时让浏览器直接下载成这个文件名，不在页面里打开（fallback 是不认 filename* 时的 ASCII 名）；
        这个覆盖参数要一起签名（签原值）。
        """
        expires = int(time.time()) + int(expires_seconds)
        resource, extra = f"/{self.bucket}/{key}", ""
        if filename:
            disposition = content_disposition(filename, fallback)
            resource += f"?response-content-disposition={disposition}"
            extra = f"&response-content-disposition={quote(disposition, safe='')}"
        to_sign = f"GET\n\n\n{expires}\n{resource}"
        signature = base64.b64encode(
            hmac.new(self._sk, to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode()
        return (
            f"{self._url(key, public=True)}?OSSAccessKeyId={quote(self._ak, safe='')}"
            f"&Expires={expires}&Signature={quote(signature, safe='')}{extra}"
        )
