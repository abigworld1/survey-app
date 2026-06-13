"""HTTP・スラッグ化・安定IDなどの小道具（標準ライブラリのみ）。"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# 各APIの作法に合わせて、誰からのアクセスか分かる User-Agent を必ず付ける
USER_AGENT = (
    "survey-app/0.1 (+https://abigworld1.github.io/survey-app/; "
    "mailto:hirayama.h77@gmail.com)"
)

# ホストごとの最終アクセス時刻（簡易レート制限用）
_last_call = {}


def _throttle(host, min_interval):
    if min_interval <= 0:
        return
    now = time.time()
    wait = min_interval - (now - _last_call.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.time()


def http_get(url, headers=None, timeout=30, min_interval=0.0, expect="json"):
    """GET。expect='json' ならパースして返す。min_interval でホスト毎に間隔を空ける。"""
    _throttle(urllib.parse.urlparse(url).netloc, min_interval)
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if expect == "json":
        return json.loads(raw.decode("utf-8"))
    return raw.decode("utf-8", "replace")


def http_post_json(url, payload, headers=None, timeout=120):
    """JSON を POST して JSON を受け取る。"""
    h = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_slug_re = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(text, maxlen=80, fallback="item"):
    """ファイル名/URL に安全な文字だけに落とす。パストラバーサルや記号を排除。"""
    text = (text or "").strip().lower().replace("/", "-")
    text = _slug_re.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    if not text:
        return fallback
    return text[:maxlen].strip("-._") or fallback


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()
