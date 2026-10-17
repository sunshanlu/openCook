"""Evidence-based access checks and bounded, best-effort failure snapshots."""

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError

from .runtime import read_json, redact
from .storage import write_json

OBSERVE_ACCESS = r"""() => {
    const visible = el => {
        if (!el || !el.getClientRects().length) return false;
        for (let p = el; p; p = p.parentElement) {
            const s = getComputedStyle(p);
            if (p.hidden || s.display === 'none' || s.visibility === 'hidden' ||
                s.visibility === 'collapse' || Number(s.opacity) === 0) return false;
        }
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
            r.top < innerHeight && r.left < innerWidth;
    };
    const matched = [];
    const collect = (selector, kind, pattern) => {
        for (const el of document.querySelectorAll(selector)) {
            if (visible(el) && pattern.test(el.innerText || '')) {
                matched.push({kind, selector, text: (el.innerText || '').slice(0, 240)});
            }
        }
    };
    collect('.login-container, .login-modal, [role="dialog"]:has(input[type="tel"]), ' +
        '[role="dialog"]:has(input[type="password"])', 'login',
        /扫码登录|手机号登录|登录后查看|登录后即可|登录后使用/);
    collect('.captcha-container, .captcha-modal, .verify-container, .verify-modal', 'verification',
        /请完成验证|安全验证|拖动滑块|点击完成验证/);
    for (const el of document.querySelectorAll('iframe')) {
        if (visible(el) && /captcha|verify|challenge/i.test(el.src)) {
            matched.push({kind: 'verification', selector: 'iframe', text: '',
                source: el.src});
        }
    }
    const account = [...document.querySelectorAll('a[href*="/user/profile/"]')]
        .some(el => visible(el) && el.innerText.trim() === '我');
    return {
        url: location.href, ready_state: document.readyState, account, matched,
        login: matched.some(m => m.kind === 'login'),
        verify: matched.some(m => m.kind === 'verification'),
        restricted: location.pathname === '/website-login/error' ||
            location.pathname.startsWith('/website-login/error/')
    };
}"""


def observe_access(page):
    try:
        return page.evaluate(OBSERVE_ACCESS)
    except PlaywrightError as error:
        return {"url": page.url, "ready_state": "unavailable", "account": False,
                "login": False, "verify": False, "restricted": False,
                "matched": [], "observation_error": redact(error)}


def safe_url(url):
    parts = urlsplit(url)
    # Query/fragment can contain credentials, session IDs and shared-note tokens.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def safe_html(html):
    # Hydration scripts may contain credentials not represented in visible controls.
    html = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", html, flags=re.I | re.S)
    html = re.sub(r"\svalue\s*=\s*([\"']).*?\1", ' value="<REDACTED>"', html,
                  flags=re.I | re.S)
    return redact(html)


def safe_state(state):
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return redact(value) if isinstance(value, str) else value

    state = clean(state)
    state['url'] = safe_url(state.get('url', ''))
    for match in state.get('matched', []):
        if 'source' in match:
            match['source'] = safe_url(match['source'])
    return state


class AccessDiagnostics:
    def __init__(self, directory: Path, *, clock=time.time, max_bytes=200_000_000,
                 retention_seconds=7 * 86400):
        self.directory, self.clock = Path(directory), clock
        self.max_bytes, self.retention_seconds = max_bytes, retention_seconds

    def record(self, scope, code, state, page):
        """Never let diagnostic IO replace the access failure or interrupt recovery."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            state = safe_state(state)
            fingerprint = hashlib.sha256(json.dumps(
                {"code": code, "state": state}, sort_keys=True,
            ).encode()).hexdigest()
            journal_path = self.directory / f"last-{scope}.json"
            previous = read_json(journal_path)
            self.prune()
            if previous.get("fingerprint") == fingerprint and Path(
                previous.get('path', '')
            ).is_dir():
                return previous.get("path")
            stamp = self.clock()
            target = self.directory / f"{int(stamp)}-{scope}-{uuid4().hex[:8]}"
            target.mkdir(mode=0o700)
            evidence = {"code": code, "observed_at": stamp, **state, "capture_errors": []}
            for name, capture in (
                ("page.png", lambda: page.screenshot(path=str(target / "page.png"),
                                                    full_page=False, timeout=5000)),
                ("page.html", lambda: (target / "page.html").write_text(
                    safe_html(page.content()), encoding="utf-8")),
            ):
                try:
                    capture()
                except Exception as error:
                    evidence["capture_errors"].append({"artifact": name, "error": redact(error)})
            write_json(target / "evidence.json", evidence)
            write_json(journal_path, {"fingerprint": fingerprint, "path": str(target),
                                     "condition": state.get('condition')})
            self.prune()
            return str(target) if (target / 'evidence.json').is_file() else None
        except Exception as error:
            print(f"访问诊断保存失败：{redact(error)}", flush=True)
            return None

    def resolved(self, scope, *, access_only=False):
        try:
            path = self.directory / f"last-{scope}.json"
            previous = read_json(path)
            protected = {'editor_unavailable', 'account_mismatch',
                         'account_identity_unavailable', 'account_identity_ambiguous'}
            if not access_only or previous.get('condition') not in protected:
                path.unlink(missing_ok=True)
            if self.directory.is_dir():
                self.prune()
        except OSError as error:
            print(f"访问诊断状态保存失败：{redact(error)}", flush=True)

    def prune(self):
        snapshots = []
        for target in self.directory.iterdir():
            if not target.is_dir() or not re.fullmatch(r"\d+-[\w-]+-[0-9a-f]{8}", target.name):
                continue
            stamp = int(target.name.split("-", 1)[0])
            if not (target / 'evidence.json').is_file() and (
                stamp >= self.clock() - self.retention_seconds
            ):
                continue  # Another stage may still be capturing this snapshot.
            files = [p for p in target.iterdir() if p.is_file()]
            size = sum(p.stat().st_size for p in files)
            snapshots.append((stamp, target, files, size))
        remaining = sum(row[3] for row in snapshots)
        for stamp, target, files, size in sorted(snapshots):
            if stamp >= self.clock() - self.retention_seconds and remaining <= self.max_bytes:
                continue
            for path in files:
                path.unlink()
            target.rmdir()
            remaining -= size
