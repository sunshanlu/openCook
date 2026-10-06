import re
from pathlib import Path
from urllib.parse import urlsplit

from .errors import TaskError

HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}
NOTE_PATH = re.compile(
    r"^/(?:explore|discovery/item|user/profile/[0-9a-f]{24})/([0-9a-f]{24})(?:/|$)"
)


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
        ):
            raise ValueError("unsupported URL")
    except ValueError as exc:
        raise TaskError(
            "link_invalid", "仅支持 https 小红书笔记链接和 xhslink.com 短链接", retryable=False,
        ) from exc
    return url


def note_id(url: str) -> str | None:
    match = NOTE_PATH.search(urlsplit(url).path)
    return match.group(1) if match else None


def read_links(path: Path) -> list[str]:
    return [
        line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()
    ]
