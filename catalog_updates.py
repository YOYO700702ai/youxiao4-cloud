"""Version-checked updates through the official catalog, never legacy Notion."""
import copy
import math
import re
from urllib.parse import quote
from uuid import uuid4

from listing_client import ListingServiceError


_FIELDS = frozenset({'價格', '人數', '類型', '時長', '類型標籤', '新增類型標籤', '角色', '劇情簡介'})


def _text(value, field, maximum):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ListingServiceError(f'{field}必須是文字。')
    value = value.strip()
    try:
        length = len(value.encode('utf-16-le')) // 2
    except UnicodeEncodeError:
        raise ListingServiceError(f'{field}包含無法儲存的字元。') from None
    if length > maximum:
        raise ListingServiceError(f'{field}最多 {maximum} 個字。')
    return value


def _items(value, field, maximum=20, item_maximum=30):
    if value is None or value == '':
        return []
    if isinstance(value, str):
        value = re.split(r'[/／、,，\r\n]', value)
    if not isinstance(value, list):
        raise ListingServiceError(f'{field}必須是文字清單。')
    items = [_text(item, field, item_maximum) for item in value]
    if any(not item for item in items) or len(items) > maximum:
        raise ListingServiceError(f'{field}最多 {maximum} 項，且不能含空白項目。')
    return list(dict.fromkeys(items))


def build_update_patch(fields, source):
    """Send only requested fields; preserve character assets for retained names."""
    if not isinstance(fields, dict) or not fields or set(fields) - _FIELDS:
        raise ListingServiceError('請提供有效的劇本更新欄位。')
    if '類型標籤' in fields and '新增類型標籤' in fields:
        raise ListingServiceError('替換標籤與新增標籤請擇一，這次尚未修改。')
    patch = {}
    if '價格' in fields:
        price = fields['價格']
        if type(price) is float and math.isfinite(price) and price.is_integer():
            price = int(price)
        if price is not None and (type(price) is not int or not 0 <= price <= 100000):
            raise ListingServiceError('價格必須是 0 至 100000 的整數，或以空值設為待定。')
        patch.update(price=price, priceStatus='tbd' if price is None else 'free' if price == 0 else 'fixed')
    if '人數' in fields:
        people = _items(fields['人數'], '人數', 30, 30)
        numbers = set()
        for person in people:
            match = re.fullmatch(r'(\d{1,2})(?:\s*[~～至\-–]\s*(\d{1,2}))?人', person)
            if not match:
                raise ListingServiceError('官網人數請填 1 至 30 人或明確範圍（例如 5～8人）。')
            low, high = int(match[1]), int(match[2] or match[1])
            if not 1 <= low <= high <= 30:
                raise ListingServiceError('人數範圍請填 1 至 30 人。')
            numbers.update(range(low, high + 1))
        if not numbers or sorted(numbers) != list(range(min(numbers), max(numbers) + 1)):
            raise ListingServiceError('官網人數不可清空，範圍中的人數須連續。')
        patch.update(playerMin=min(numbers), playerMax=max(numbers))
    if '類型' in fields:
        patch['genres'] = _items(fields['類型'], '類型')
    if '時長' in fields:
        duration = _text(fields['時長'], '時長', 80)
        if not duration:
            raise ListingServiceError('已上架劇本的時長不可清空，這次尚未修改。')
        # A new label must not retain a contradictory old numeric duration.
        patch.update(durationLabel=duration, durationMinutes=None)
    if '類型標籤' in fields:
        patch['customTags'] = _items(fields['類型標籤'], '類型標籤')
    if '新增類型標籤' in fields:
        additions = _items(fields['新增類型標籤'], '新增類型標籤')
        if not additions:
            raise ListingServiceError('請提供要新增的標籤。')
        patch['customTags'] = _items(list(dict.fromkeys([*source.get('customTags', []), *additions])), '類型標籤')
    if '角色' in fields:
        names = _items(fields['角色'], '角色', 30, 80)
        existing = {character['name']: character for character in source.get('characters', [])}
        patch['characters'] = [copy.deepcopy(existing.get(name, {'name': name, 'description': ''})) for name in names]
    if '劇情簡介' in fields:
        summary = _text(fields['劇情簡介'], '簡介', 8000)
        if not summary:
            raise ListingServiceError('已上架劇本的簡介不可清空，這次尚未修改。')
        patch['synopsis'] = summary
    return patch


def update_catalog_script(client, name, fields):
    """Publish one scoped update and require the exact version's public receipt."""
    target = None
    try:
        name = _text(name, '劇本名稱', 120)
        if not name:
            raise ListingServiceError('請提供完整的劇本名稱。')
        baseline = client.lookup(name)
        if not baseline:
            raise ListingServiceError(f'官網找不到《{name}》，這次尚未修改。')
        source = baseline.get('rawContent') or {}
        version = baseline.get('versionId')
        if (not version or baseline.get('publishedVersionId') != version
                or baseline.get('hasUnpublishedChanges') or source.get('status') != 'published'):
            raise ListingServiceError('這本有尚未發布的草稿或目前未上架，請先由員工後台確認；這次沒有覆寫或發布。', 409)
        patch = build_update_patch(fields, source)
        request_id = 'update-' + uuid4().hex
        script_id = baseline['id']
        path = '/' + quote(script_id, safe='')
        changed = any(source.get(key) != value for key, value in patch.items())
        if changed:
            saved = client._api('PATCH', path,
                                body={'content': patch, 'expectedVersionId': version},
                                request_id=request_id, operation='update')
            saved_script = saved.get('script') or {}
            saved_version = saved.get('operationVersionId')
            # Never publish another script or a later staff draft from a replay.
            if saved_script.get('id') != script_id or not saved_version:
                raise ListingServiceError('官網回覆的資料版本無法確認，請到員工後台查看；尚未確認更新完成。')
            version = saved_version
        target = {'scriptId': script_id, 'versionId': version}
        if changed:
            client._api('POST', path + '/publish', body={'expectedVersionId': version},
                        request_id=request_id, operation='publish')
        # An unchanged version belongs to an earlier request (or staff). Never
        # claim ownership or republish it using this invocation's request ID.
        outcome = client.verify_publication({'jobId': request_id, 'publicationTarget': target})
        if outcome.get('status') == 'published':
            action = '已更新' if changed else '已是要求的內容'
            return {'ok': True, 'status': 'published', **target,
                    'message': f'《{name}》指定欄位{action}，並已確認官網發布完成。\n{outcome["url"]}'}
        if outcome.get('status') == 'conflict':
            return {'ok': False, 'status': 'conflict', **target,
                    'message': '官網資料版本已變動，尚未確認這次更新完成。請到員工後台查看，勿重複覆寫。'}
        return {'ok': False, 'status': 'pending', **target,
                'message': f'《{name}》資料已保存，官網同步或正式頁面讀回尚未確認完成，還不能算更新完成。請到員工後台查看；若僅同步待確認，可稍後重傳相同修改指令確認。'}
    except ListingServiceError as exc:
        message = str(exc)
        if '重試上架' in message:
            # This one-shot field edit is not a durable image/listing job.
            message = '官網連線或同步尚未確認完成，請到員工後台查看後再重試修改。'
        if target:
            return {'ok': False, 'status': 'conflict' if exc.status == 409 else 'pending', **target,
                    'message': '官網尚未確認更新完成，請到員工後台查看發布與同步狀態。' + message}
        return {'ok': False, 'status': 'conflict' if exc.status == 409 else 'failed', 'message': message}
