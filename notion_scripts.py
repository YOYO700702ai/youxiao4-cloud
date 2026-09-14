"""Validated Notion script writes, pinned to the existing 2022-06-28 API.

No credentials are loaded here. Inject a plain requests-compatible session to
test without network. The session must not independently retry writes.

Text is preserved and split at 2,000 UTF-16 units (at most 100 rich-text items).
Optional None/empty values explicitly clear a property; omitted keys are not
changed. Multi-select delimiters/outer whitespace are normalized, not guessed.
MAX_PRICE is a local safety guard, not a Notion limit; validation accepts an
override. Creation is serialized per instance, not across processes. Keep one
instance for the workflow: uncertain creates remain blocked on that instance
until a subsequent exact query can establish the requested result. Notion has
no transactional unique-title constraint, so this is not exactly-once delivery.

References:
https://developers.notion.com/reference/request-limits
https://developers.notion.com/reference/post-database-query
https://developers.notion.com/reference/post-database-query-filter
"""

import json
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

import requests


API_ROOT = "https://api.notion.com/v1"
MAX_PRICE = 100_000
TYPE_OPTIONS = frozenset("恐怖/微恐/驚悚/沉浸/情感/演繹/推理/還原/機制/陣營/歡樂/撕逼/硬核/燒腦".split("/"))
PEOPLE_OPTIONS = frozenset("5人/6人/7人/8人/9人/10人/11人/浮動人".split("/"))
ALIASES = {"劇本名稱": "名稱", "劇情簡介": "簡介"}
TEXT_FIELDS = {"名稱": "劇本名稱", "簡介": "劇情簡介", "類型標籤": "類型標籤", "時長": "時長"}
MULTI_FIELDS = {"類型", "人數", "角色"}
ALLOWED_FIELDS = set(TEXT_FIELDS) | MULTI_FIELDS | {"價格"}


def _units(value):
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise ValueError("文字包含無法傳送的 Unicode 字元。") from None


def _rich_text(value):
    if value is None or value == "":
        return []
    chunks, chunk, size = [], [], 0
    for character in value:
        width = 2 if ord(character) > 0xFFFF else 1
        if size + width > 2000:
            chunks.append({"type": "text", "text": {"content": "".join(chunk)}})
            chunk, size = [], 0
        chunk.append(character)
        size += width
    if chunk:
        chunks.append({"type": "text", "text": {"content": "".join(chunk)}})
    if len(chunks) > 100:
        raise ValueError("文字超過 Notion 單一欄位可傳送的長度，請縮短後再試。")
    return chunks


def _validate_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("請提供非空白的劇本名稱。")
    if name != name.strip():
        raise ValueError("劇本名稱前後有空白，請確認名稱後再送出。")
    if _units(name) > 2000:
        raise ValueError("劇本名稱過長，請限制在 2000 字元以內。")
    return name


def _multi(value, field):
    # A second validation receives [] from normalized blank optional fields.
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[/、,，\n\r]", value)]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = [item.strip() for item in value]
    else:
        raise ValueError(f"{field}必須是文字或文字清單。")
    if not items or any(not item for item in items):
        raise ValueError(f"{field}含有空白選項，請確認分隔符號。")
    if len(items) > 100 or any(_units(item) > 100 for item in items):
        raise ValueError(f"{field}選項過多或名稱過長。")
    if len(set(items)) != len(items):
        raise ValueError(f"{field}含有重複選項，請確認後再送出。")
    if any(re.search(r"[/、,，\n\r]", item) for item in items):
        raise ValueError(f"{field}清單的單一選項不能包含分隔符號。")
    allowed = {"類型": TYPE_OPTIONS, "人數": PEOPLE_OPTIONS}.get(field)
    if allowed is not None and any(item not in allowed for item in items):
        raise ValueError(f"{field}含有不支援的選項，請使用既有選項。")
    return items


def validate_script_info(info, *, require_name=True, max_price=MAX_PRICE):
    """Return canonical creation keys; raise ValueError without echoing input.

    名稱/簡介 are canonical aliases for 劇本名稱/劇情簡介. Multi-select
    values become lists; a decimal-digit price string becomes an int. Optional
    None is an explicit empty value. Unknown/conflicting aliases are rejected.
    Use require_name=False for patches; title changes are supported explicitly.
    """
    if not isinstance(info, dict) or not info:
        raise ValueError("請提供需要寫入的劇本欄位。")
    normalized = {}
    for supplied_key, value in info.items():
        if not isinstance(supplied_key, str):
            raise ValueError("劇本欄位名稱必須是文字。")
        key = ALIASES.get(supplied_key, supplied_key)
        if key not in ALLOWED_FIELDS:
            raise ValueError("包含不支援的劇本欄位，請確認欄位名稱。")
        if key in normalized:
            raise ValueError("同一個劇本欄位使用了重複名稱或別名。")
        if key == "名稱":
            value = _validate_name(value)
        elif key in TEXT_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key}必須是文字或空值。")
            if value is not None:
                _units(value)
                _rich_text(value)
        elif key in MULTI_FIELDS:
            value = _multi(value, key)
        elif key == "價格":
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
                if len(value) > 10:
                    raise ValueError("價格超過可接受範圍。")
                value = int(value)
            if value is not None and (type(value) is not int or not 0 <= value <= max_price):
                raise ValueError(f"價格必須是 0 至 {max_price} 的整數或空值。")
        normalized[key] = value
    if require_name and "名稱" not in normalized:
        raise ValueError("請提供劇本名稱。")
    # Use requests' default ASCII JSON serialization for a conservative bound.
    if len(json.dumps(_properties(normalized)).encode("utf-8")) > 490_000:
        raise ValueError("劇本資料超過 Notion 請求大小限制，請縮短後再試。")
    return normalized


def _properties(info):
    result = {}
    for key, value in info.items():
        if key in TEXT_FIELDS:
            kind = "title" if key == "名稱" else "rich_text"
            result[TEXT_FIELDS[key]] = {kind: _rich_text(value)}
        elif key in MULTI_FIELDS:
            result[key] = {"multi_select": [{"name": item} for item in value]}
        else:
            result[key] = {"number": value}
    return result


def _validate_cover(url):
    if not isinstance(url, str) or not url or len(url) > 2000:
        raise ValueError("封面必須是長度不超過 2000 字元的 HTTPS 網址。")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or any(char.isspace() or ord(char) < 32 for char in url)):
            raise ValueError
        parsed.port  # Reject malformed ports without requesting the URL.
    except ValueError:
        raise ValueError("封面必須是有效、不含帳密的 HTTPS 網址。") from None
    return {"type": "external", "external": {"url": url}}


def _id(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


def _page_url(page):
    # Do not echo an arbitrary response URL, query string, or API error text.
    page_id = _id(page.get("id"))
    return "https://www.notion.so/" + page_id.replace("-", "") if page_id else ""


def _plain(items):
    if not isinstance(items, list):
        return None
    parts = []
    for item in items:
        if not isinstance(item, dict):
            return None
        value = item.get("plain_text")
        if value is None:
            text = item.get("text")
            value = text.get("content") if isinstance(text, dict) else None
        if not isinstance(value, str):
            return None
        parts.append(value)
    return "".join(parts)


def _same_property(actual, expected):
    if not isinstance(actual, dict):
        return False
    kind = next(iter(expected))
    if kind in ("title", "rich_text"):
        return _plain(actual.get(kind)) == _plain(expected[kind])
    if kind == "multi_select":
        values = actual.get(kind)
        if not isinstance(values, list) or not all(isinstance(v, dict) and isinstance(v.get("name"), str) for v in values):
            return False
        return sorted(v["name"] for v in values) == sorted(v["name"] for v in expected[kind])
    return kind in actual and type(actual[kind]) is not bool and actual[kind] == expected[kind]


class NotionScripts:
    def __init__(self, token, database_id, session=None, *, timeout=(4, 12),
                 read_attempts=2, sleeper=time.sleep, max_price=MAX_PRICE):
        self._token = token
        self._database_id = _id(database_id)
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._read_attempts = max(1, min(int(read_attempts), 3))
        self._sleep = sleeper
        self._max_price = max_price
        self._create_lock = threading.Lock()
        self._uncertain_creates = {}

    def _request(self, method, path, body, *, read_only=False):
        """Return (JSON, safe error, outcome uncertain); never retry writes."""
        if not isinstance(self._token, str) or not self._token or not self._database_id:
            return None, "Notion 連線設定不完整，請管理者確認。", False
        headers = {"Authorization": f"Bearer {self._token}",
                   "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
        attempts = self._read_attempts if read_only else 1
        for attempt in range(attempts):
            try:
                response = self._session.request(method, API_ROOT + path, headers=headers,
                                                 json=body, timeout=self._timeout,
                                                 allow_redirects=False)
            except requests.RequestException:
                if read_only and attempt + 1 < attempts:
                    self._sleep(1 + attempt)
                    continue
                return None, "Notion 連線未完成，請稍後查詢確認。", not read_only
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except (ValueError, TypeError):
                    data = None
                if not isinstance(data, dict):
                    return None, "Notion 回傳格式異常，無法確認結果。", not read_only
                return data, None, False
            if read_only and (status == 429 or status in (500, 502, 503, 504)) and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = int(retry_after) if retry_after is not None else 1 + attempt
                except (ValueError, TypeError):
                    delay = 1 + attempt
                if not 0 <= delay <= 5:
                    break  # Do not retry earlier than a longer Retry-After.
                self._sleep(delay)
                continue
            break
        errors = {400: "Notion 拒絕資料格式，請確認欄位與資料庫設定。",
                  401: "Notion 驗證失敗，請管理者確認連線設定。",
                  403: "Notion 權限不足，請確認資料庫已授權連線。",
                  404: "找不到 Notion 資料庫或頁面，請確認授權與設定。",
                  409: "Notion 發生寫入衝突，請先查詢目前資料。",
                  429: "Notion 目前請求過多，請稍後再查詢確認。"}
        return None, errors.get(status, "Notion 服務暫時無法完成請求，請稍後查詢確認。"), not read_only and status >= 500

    def _query(self, name, operator="equals"):
        body = {"filter": {"property": "劇本名稱", "title": {operator: name}}, "page_size": 2}
        data, error, _ = self._request("POST", f"/databases/{self._database_id}/query", body, read_only=True)
        if error:
            return None, error
        results = data.get("results")
        if not isinstance(results, list) or type(data.get("has_more")) is not bool:
            return None, "Notion 搜尋結果格式異常，無法確認唯一頁面。"
        if data["has_more"] or len(results) > 1:
            return None, "找到多筆或尚未完整的搜尋結果，請確認唯一劇本後再操作。"
        for page in results:
            if (not isinstance(page, dict) or not _id(page.get("id"))
                    or not isinstance(page.get("properties"), dict)):
                return None, "Notion 頁面格式異常，無法確認操作對象。"
            title_property = page["properties"].get("劇本名稱", {})
            title = _plain(title_property.get("title")) if isinstance(title_property, dict) else None
            if title is None or page.get("archived") or page.get("in_trash"):
                return None, "Notion 頁面內容或狀態異常，請先人工確認。"
            if operator == "equals" and title != name:
                return None, "搜尋到名稱相近但不完全相同的劇本，請確認名稱後再操作。"
        return results, None

    def find(self, name):
        """Find one exact match, then one contains match; refuse ambiguity."""
        try:
            _validate_name(name)
        except ValueError as error:
            return None, str(error)
        for operator in ("equals", "contains"):
            results, error = self._query(name, operator)
            if error:
                return None, error
            if results:
                return results[0], None
        return None, "找不到符合名稱的劇本，請確認名稱。"

    def _body_matches(self, page, body):
        parent = page.get("parent")
        if not isinstance(parent, dict) or _id(parent.get("database_id")) != self._database_id:
            return False
        props = page.get("properties", {})
        if not all(_same_property(props.get(key), expected) for key, expected in body["properties"].items()):
            return False
        expected_cover = body.get("cover")
        actual_cover = page.get("cover")
        if expected_cover is None:
            return actual_cover is None
        actual_external = actual_cover.get("external") if isinstance(actual_cover, dict) else None
        return (isinstance(actual_cover, dict) and actual_cover.get("type") == "external"
                and isinstance(actual_external, dict)
                and actual_external.get("url") == expected_cover["external"]["url"])

    def _reconcile_create(self, name, body):
        pages, error = self._query(name)
        if error or not pages or not self._body_matches(pages[0], body):
            return False, "新增結果尚未確認；已停止重送，請先到 Notion 核對同名劇本及完整內容。"
        self._uncertain_creates.pop(name, None)
        return True, _page_url(pages[0])

    def create(self, info, cover_url=None):
        """Create once; exact duplicate is an error, never an overwrite."""
        try:
            normalized = validate_script_info(info, max_price=self._max_price)
            body = {"parent": {"database_id": self._database_id}, "properties": _properties(normalized)}
            if cover_url is not None:
                body["cover"] = _validate_cover(cover_url)
        except ValueError as error:
            return False, str(error)
        name = normalized["名稱"]
        with self._create_lock:
            if name in self._uncertain_creates:
                if self._uncertain_creates[name] != body:
                    return False, "同名劇本前次新增結果尚未確認，請先人工核對後再修改資料。"
                return self._reconcile_create(name, body)
            pages, error = self._query(name)
            if error:
                return False, error
            if pages:
                return False, "已有同名劇本，未新增或覆寫：" + _page_url(pages[0])
            data, error, uncertain = self._request("POST", "/pages", body)
            if uncertain or (not error and not _page_url(data)):
                self._uncertain_creates[name] = body
                return self._reconcile_create(name, body)
            if error:
                return False, error
            return True, _page_url(data)

    def _patch(self, page, body, message):
        data, error, uncertain = self._request("PATCH", "/pages/" + _id(page["id"]), body)
        if uncertain or (not error and _id(data.get("id")) != _id(page["id"])):
            return False, "寫入結果尚未確認；沒有自動重送，請先到 Notion 核對。"
        return (False, error) if error else (True, message)

    def update(self, name, fields):
        try:
            normalized = validate_script_info(fields, require_name=False, max_price=self._max_price)
        except ValueError as error:
            return False, str(error)
        page, error = self.find(name)
        if error:
            return False, error
        if "名稱" in normalized and normalized["名稱"] != name:
            pages, error = self._query(normalized["名稱"])
            if error:
                return False, error
            if pages and _id(pages[0]["id"]) != _id(page["id"]):
                return False, "新名稱已有劇本使用，未修改：" + _page_url(pages[0])
        props = {key: value for key, value in _properties(normalized).items()
                 if not _same_property(page["properties"].get(key), value)}
        if not props:
            return True, "目前已是指定資料，無需重複寫入"
        return self._patch(page, {"properties": props}, "劇本已更新：" + "、".join(props))

    def replace_cover(self, name, cover_url):
        try:
            cover = _validate_cover(cover_url)
        except ValueError as error:
            return False, str(error)
        page, error = self.find(name)
        if error:
            return False, error
        return self._patch(page, {"cover": cover}, "劇本封面已更新。")

    def archive(self, name):
        page, error = self.find(name)
        if error:
            return False, error
        return self._patch(page, {"archived": True}, "劇本已下架（封存）。")
