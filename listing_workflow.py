"""Durable, explicitly finished LINE script listings.

No clients or credentials are created here. ``store.get(key)`` returns a JSON
state or None; ``store.put(key, state)`` must durably save (and use CAS when
shared by multiple processes). A put failure is fatal before the next side
effect. The backend owns publication idempotency, the published-version
precondition, Notion synchronization, and public readback.

Images never enter a task without an explicit label. Bytes are not put in the
state store: upload them first, save the permanent asset, then identify them.
After a restart, ``backend.read_media(asset)`` supplies already uploaded bytes.
An upload whose response was lost repeats the SAME operation key; the backend
must resolve that key to the same asset. The same rule applies to jobId for
publication. Only a backend status of ``published`` completes a task.
"""

from copy import deepcopy
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

_LOCK = threading.RLock()
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_STATE_BYTES = 480 * 1024
FINISH_COMMAND = '資料傳完，直接上架'
_TERMINAL = frozenset({'published', 'cancelled'})
_ALIASES = {'劇本名稱': '名稱', '劇情簡介': '簡介'}
_TEXT_FIELDS = frozenset({'名稱', '時長', '類型標籤', '簡介'})
_LIST_FIELDS = frozenset({'人數', '類型', '角色'})
_FLOATING_PEOPLE = frozenset({'浮動', '浮動人', '浮動人數'})
_TEXT_LIMITS = {'名稱': 120, '時長': 80, '簡介': 8000}


def _units(value):
    try:
        # The website validates JavaScript string.length (UTF-16 code units).
        return len(value.encode('utf-16-le')) // 2
    except UnicodeEncodeError:
        raise ValueError('文字包含無法儲存的字元。') from None


def _people_bounds(value):
    fixed = re.fullmatch(r'(\d{1,2})人', value)
    ranged = re.fullmatch(r'(\d{1,2})\s*[~～至\-–]\s*(\d{1,2})人', value)
    if fixed:
        minimum = maximum = int(fixed[1])
    elif ranged:
        minimum, maximum = int(ranged[1]), int(ranged[2])
    else:
        return None
    return (minimum, maximum) if 1 <= minimum <= maximum <= 30 else None


def normalize_listing_data(data, *, require_name=True):
    """Validate the workflow contract without the legacy Notion option lists.

    A supplied empty optional value stays empty; missing fields remain missing.
    Names/genres are not guessed or translated. Numeric player bounds are
    explicit (1-30); a bare floating label may be saved but cannot be finished.
    """
    if not isinstance(data, dict) or not data:
        raise ValueError('請提供需要儲存的劇本欄位。')
    normalized = {}
    for supplied_key, value in data.items():
        if not isinstance(supplied_key, str):
            raise ValueError('劇本欄位名稱必須是文字。')
        key = _ALIASES.get(supplied_key, supplied_key)
        if key not in _TEXT_FIELDS | _LIST_FIELDS | {'價格'}:
            raise ValueError('包含不支援的劇本欄位，請確認欄位名稱。')
        if key in normalized:
            raise ValueError('同一欄位使用了重複名稱或別名。')
        if key in _TEXT_FIELDS:
            if value is not None and not isinstance(value, str):
                raise ValueError(f'{key}必須是文字或空值。')
            if key == '名稱':
                if not isinstance(value, str) or not value.strip():
                    raise ValueError('請提供劇本名稱。')
                value = value.strip()
            if value is not None and key in _TEXT_LIMITS and _units(value.strip()) > _TEXT_LIMITS[key]:
                raise ValueError(f'{key}最多 {_TEXT_LIMITS[key]} 個字。')
            if key == '類型標籤' and value:
                tags = [part.strip() for part in re.split(r'[/／、,，\r\n]', value) if part.strip()]
                if len(tags) > 20 or any(_units(tag) > 30 for tag in tags):
                    raise ValueError('類型標籤最多 20 項，每項最多 30 個字。')
        elif key in _LIST_FIELDS:
            if value is None or value == '' or value == []:
                value = []
            elif isinstance(value, str):
                value = [part.strip() for part in re.split(r'[/、,，\r\n]', value)]
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                value = [item.strip() for item in value]
            else:
                raise ValueError(f'{key}必須是文字清單。')
            max_items, item_max = (20, 30) if key == '類型' else (30, 80)
            if any(not item for item in value) or len(value) > max_items or any(_units(item) > item_max for item in value):
                raise ValueError(f'{key}含有空白或過長選項。')
            if len(set(value)) != len(value):
                raise ValueError(f'{key}含有重複選項。')
            if key == '人數' and any(item not in _FLOATING_PEOPLE and _people_bounds(item) is None for item in value):
                raise ValueError('人數請填 1 至 30 人、明確範圍（例如 5～8人），或先註明浮動。')
            if key == '人數':
                expanded = []
                for item in value:
                    bounds = _people_bounds(item)
                    if bounds:
                        expanded.extend(f'{number}人' for number in range(bounds[0], bounds[1] + 1))
                    else:
                        expanded.append(item)
                if len(set(expanded)) != len(expanded):
                    raise ValueError('人數範圍含有重複選項。')
                value = expanded
        else:
            if isinstance(value, str) and re.fullmatch(r'[0-9]{1,6}', value):
                value = int(value)
            if value is not None and (type(value) is not int or not 0 <= value <= 100_000):
                raise ValueError('價格必須是 0 至 100000 的整數。')
        normalized[key] = value
    if require_name and '名稱' not in normalized:
        raise ValueError('請提供劇本名稱。')
    try:
        if len(json.dumps(normalized, ensure_ascii=False).encode('utf-8')) > 200 * 1024:
            raise ValueError('劇本資料過長，請縮短後再試。')
    except UnicodeEncodeError:
        raise ValueError('文字包含無法儲存的字元。') from None
    return normalized


class WorkflowStorageError(RuntimeError):
    """The caller must report failure; no additional side effect is allowed."""


def _response(ok, message, *, job=None, status=None, **extra):
    value = {'ok': bool(ok), 'message': message,
             'status': status or (job or {}).get('stage', 'idle')}
    if job:
        value.update(jobId=job['jobId'], name=job['data']['名稱'])
    value.update(extra)
    return value


def _https_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        return False


def _public_url(value):
    if not _https_url(value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.hostname == 'www.bglarp.com' and parsed.port in (None, 443)
    except ValueError:
        return False


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    # LINE sends Unix milliseconds; tests and other callers may use seconds.
    return value / 1000 if value > 100_000_000_000 else value


class ListingWorkflow:
    """Single active task per (group, user), with injected durable storage.

    Public image indexes are one-based. ``identify`` returns only the three
    trusted shape fields roleName/confidence/matchesScript; its raw response
    and exceptions are never echoed into a group or stored.
    """

    def __init__(self, *, store, backend, download, identify, clock=time.time,
                 max_image_bytes=MAX_IMAGE_BYTES):
        self.store, self.backend = store, backend
        self.download, self.identify, self.clock = download, identify, clock
        self.max_image_bytes = max_image_bytes
        # Public for the dispatcher's atomic inspect/auto-begin/label sequence.
        self.lock = _LOCK

    def _load(self, key):
        if (not isinstance(key, (tuple, list)) or len(key) != 2
                or not all(isinstance(part, str) and part for part in key)):
            raise ValueError('無法辨識群組與傳送者，請從原群組重新操作。')
        try:
            state = self.store.get(tuple(key))
        except Exception:
            raise WorkflowStorageError('上架資料暫時讀取失敗，這次沒有繼續操作。') from None
        if state is not None and (not isinstance(state, dict)
                                  or state.get('schemaVersion') != 1):
            raise WorkflowStorageError('上架資料版本不符，請請管理員檢查。')
        return deepcopy(state)

    def _save(self, key, job):
        job['updatedAt'] = self.clock()
        try:
            encoded = json.dumps(job, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode('utf-8')) > MAX_STATE_BYTES:
                raise ValueError('state too large')
            self.store.put(tuple(key), deepcopy(job))
        except Exception:
            raise WorkflowStorageError('上架資料尚未安全儲存，這次沒有繼續操作。') from None

    def _record(self, key, job, ok, message, status=None, **extra):
        answer = _response(ok, message, job=job, status=status, **extra)
        job['lastResult'] = answer
        self._save(key, job)
        return answer

    @staticmethod
    def _blocked(job):
        if not job:
            return _response(False, '目前沒有上架任務，請先提供劇本名稱。')
        if job['stage'] in _TERMINAL:
            return deepcopy(job['lastResult'])
        if job['stage'] in {'initializing', 'lookup_failed'}:
            return _response(False, '目前正式資料還沒讀取完成，請傳「重試上架」。', job=job)
        if job['stage'] == 'conflict':
            return deepcopy(job['lastResult'])
        if job.get('publishRequested'):
            return _response(False, '這筆資料已送出發布，請傳「重試上架」確認結果；期間不再改動內容。', job=job)
        return None

    @staticmethod
    def _roles(job):
        return job['data'].get('角色') or []

    def begin(self, key, data, kind='upload'):
        with _LOCK:
            if kind not in {'upload', 'cover', 'portraits'}:
                return _response(False, '不支援這種上架任務。')
            try:
                normalized = normalize_listing_data(data)
                current = self._load(key)
            except ValueError as exc:
                return _response(False, str(exc))
            if current and current['stage'] not in _TERMINAL:
                if (current['data']['名稱'] == normalized['名稱']
                        and current['kind'] == kind):
                    if current['stage'] in {'initializing', 'lookup_failed', 'conflict'}:
                        return self.status(key)
                    return self.merge_fields(key, normalized)
                if current['stage'] != 'published':
                    return _response(False, f"還有《{current['data']['名稱']}》的任務；請先完成，或傳「取消上架」。", job=current)
            job_id = str(uuid4())
            now = self.clock()
            job = {
                'schemaVersion': 1, 'jobId': job_id, 'kind': kind,
                'data': normalized, 'requestedData': deepcopy(normalized),
                'createdAt': now, 'updatedAt': now, 'stage': 'initializing',
                'baseline': None, 'expectedVersion': None, 'batches': [],
                'images': [], 'processedEvents': [], 'publishRequested': False,
                'publishOperationKey': f'{job_id}:publish', 'lastResult': None,
            }
            self._save(key, job)  # Persist the task BEFORE lookup or any write.
            return self._initialize(key, job)

    def _initialize(self, key, job):
        try:
            found = self.backend.lookup(job['data']['名稱'])
        except Exception as error:
            job['stage'] = 'lookup_failed'
            # Only the backend's explicit public contract may reach staff;
            # arbitrary exception text can contain private request details.
            public_message = getattr(error, 'public_message', None)
            if isinstance(public_message, str) and public_message.strip() and len(public_message) <= 1000:
                message = public_message.strip() + '\n資料已保留，完成處理後請傳「重試上架」。'
            else:
                message = '讀取目前正式版本失敗，資料已保留，請傳「重試上架」。'
            return self._record(key, job, False, message)
        if found is not None and not isinstance(found, dict):
            job['stage'] = 'lookup_failed'
            return self._record(key, job, False, '正式版本回應不完整，資料已保留，請傳「重試上架」。')
        if job['kind'] == 'upload':
            if found is not None:
                job['stage'] = 'conflict'
                return self._record(key, job, False, '已有完全同名的正式劇本，沒有覆寫；若要補圖片，請取消後使用補封面或角色圖。')
        else:
            if not found:
                job['stage'] = 'conflict'
                return self._record(key, job, False, '找不到完全同名的正式劇本，沒有建立或修改資料。')
            version = found.get('publishedVersionId') or found.get('versionId')
            content = found.get('content')
            if (not found.get('id') or not version or not isinstance(content, dict)
                    or content.get('名稱') != job['data']['名稱']):
                job['stage'] = 'conflict'
                return self._record(key, job, False, '目前資料不是可鎖定的同名正式版本，沒有修改；請管理員檢查。')
            if found.get('hasUnpublishedChanges'):
                job['stage'] = 'conflict'
                return self._record(key, job, False, '這本劇本另有未發布草稿，沒有把草稿一起上架；請先處理既有草稿。')
            try:
                published = normalize_listing_data(content)
            except ValueError:
                job['stage'] = 'conflict'
                return self._record(key, job, False, '目前正式資料格式不完整，沒有修改；請管理員檢查。')
            for field, value in job['requestedData'].items():
                if published.get(field) != value:
                    job['stage'] = 'conflict'
                    return self._record(key, job, False, '補圖任務只沿用目前正式資料，請只提供劇本名稱；這次沒有更動文字。')
            job['data'], job['baseline'] = published, deepcopy(found)
            job['expectedVersion'] = version
        job['stage'] = 'collecting'
        return self._record(key, job, True,
                            f"《{job['data']['名稱']}》資料已保留。傳圖前請先標記「接下來是封面」或「接下來是角色圖 N 張」；最後傳「{FINISH_COMMAND}」才會上架。")

    def merge_fields(self, key, data):
        with _LOCK:
            job = self._load(key)
            blocked = self._blocked(job)
            if blocked:
                return blocked
            try:
                patch = normalize_listing_data(data, require_name=False)
            except ValueError as exc:
                return _response(False, str(exc), job=job)
            if '名稱' in patch and patch['名稱'] != job['data']['名稱']:
                return _response(False, '不能把目前任務改成另一本劇本，請取消後重新開始。', job=job)
            if job['kind'] != 'upload':
                if any(job['data'].get(field) != value for field, value in patch.items()):
                    return _response(False, '補圖任務會保留正式文字資料，這次沒有修改欄位。', job=job)
                return self.status(key)
            updated = {**job['data'], **patch}
            roles = updated.get('角色') or []
            if any(image.get('confirmed') and image['purpose'] == 'portraits'
                   and image.get('roleName') not in roles for image in job['images']):
                return _response(False, '新的角色名單會移除已配對角色，請先確認角色名稱；這次沒有更動。', job=job)
            job['data'] = updated
            return self._record(key, job, True, '劇本資料已補上；仍需最後傳「資料傳完，直接上架」。')

    def label_images(self, key, name=None, purpose=None, count=1):
        with _LOCK:
            job = self._load(key)
            blocked = self._blocked(job)
            if blocked:
                return blocked
            name = job['data']['名稱'] if name is None else name
            if name != job['data']['名稱']:
                return _response(False, '劇本名稱與目前任務不符，沒有開始收圖。', job=job)
            if purpose not in {'cover', 'portraits'} or type(count) is not int or not 1 <= count <= 100:
                return _response(False, '請標記封面 1 張，或角色圖 1 至 100 張。', job=job)
            if purpose == 'cover' and count != 1:
                return _response(False, '封面每次只能指定 1 張。', job=job)
            if ((job['kind'] == 'cover' and purpose != 'cover')
                    or (job['kind'] == 'portraits' and purpose != 'portraits')):
                return _response(False, '這批圖片用途與目前補圖任務不同，沒有開始收圖。', job=job)
            active = next((batch for batch in job['batches'] if not batch['closed']), None)
            if active:
                if active['purpose'] == purpose and active['expected'] == count:
                    return _response(True, f"這批仍在收圖，已收到 {active['received']}/{count} 張；不用重新標記。", job=job)
                return _response(False, f"上一批還差 {active['expected'] - active['received']} 張，請先傳完。", job=job)
            if purpose == 'cover' and any(image['purpose'] == 'cover' for image in job['images']):
                return _response(False, '任務已有封面候選，不會再用另一張覆蓋；可確認現有封面，或取消重開。', job=job)
            if purpose == 'portraits':
                roles = self._roles(job)
                previous = sum(batch['expected'] for batch in job['batches'] if batch['purpose'] == 'portraits')
                if not roles:
                    return _response(False, '請先補上角色名單，再開始收角色圖片。', job=job)
                if previous + count > len(roles):
                    return _response(False, '宣告的角色圖片超過角色人數，請確認張數；沒有開始收圖。', job=job)
            job['batches'].append({'batchId': str(uuid4()), 'purpose': purpose,
                                   'expected': count, 'received': 0,
                                   'openedAt': self.clock(), 'closed': False})
            return self._record(key, job, True, f"接下來只收《{name}》的{'封面' if purpose == 'cover' else '角色圖'} {count} 張；收滿就停止收圖。")

    def receive_image(self, key, message_id, event_timestamp=None):
        with _LOCK:
            job = self._load(key)
            if not job or job['stage'] != 'collecting' or job.get('publishRequested'):
                return _response(False, '這張圖片沒有加入上架任務。', job=job, status='ignored')
            if not isinstance(message_id, str) or not message_id:
                return _response(False, '圖片訊息無法識別，沒有加入上架任務。', job=job, status='ignored')
            if message_id in job['processedEvents']:
                return _response(True, '這張圖片已收過，沒有重複處理。', job=job, status='duplicate')
            batch = next((entry for entry in job['batches'] if not entry['closed']), None)
            timestamp = _seconds(event_timestamp)
            if not batch or (timestamp is not None and timestamp < batch['openedAt']):
                return _response(False, '這張圖片不在已標記的收圖批次內，沒有加入上架任務。', job=job, status='ignored')
            if event_timestamp is not None and timestamp is None:
                return _response(False, '圖片時間無法確認，沒有加入上架任務。', job=job, status='ignored')
            image = {
                'index': len(job['images']) + 1, 'messageId': message_id,
                'purpose': batch['purpose'], 'batchId': batch['batchId'],
                'stage': 'received', 'recognition': None, 'confirmed': False,
                'roleName': None, 'asset': None,
                'operationKey': f"{job['jobId']}:media:{message_id}",
                'error': None,
            }
            job['images'].append(image)
            job['processedEvents'].append(message_id)
            batch['received'] += 1
            batch['closed'] = batch['received'] == batch['expected']
            self._save(key, job)  # Claim the event before download/upload.
            self._process_image(key, job, image)
            return self._image_receipt(key, job, image)

    def _process_image(self, key, job, image):
        if image.get('asset') and image.get('confirmed'):
            return
        raw = None
        if not image.get('asset'):
            image['stage'], image['error'] = 'downloading', None
            self._save(key, job)
            try:
                raw = self.download(image['messageId'])
                self._check_bytes(raw)
            except Exception:
                image['stage'], image['error'] = 'download_failed', 'download_failed'
                self._save(key, job)
                return
            image['stage'] = 'uploading'
            self._save(key, job)
            try:
                extension = 'png' if raw.startswith(b'\x89PNG') else 'webp' if raw.startswith(b'RIFF') else 'jpg'
                filename = f"{job['jobId']}-{image['index']}.{extension}"
                asset = self.backend.upload_media(raw, filename, image['operationKey'],
                                                  kind='cover' if image['purpose'] == 'cover' else 'character')
                if not isinstance(asset, dict) or not _https_url(asset.get('url')):
                    raise ValueError('invalid asset')
                # Keep only contract fields; third-party metadata may be unsafe.
                image['asset'] = {field: asset[field] for field in ('url', 'path', 'mimeType', 'width', 'height') if field in asset}
            except Exception:
                image['stage'], image['error'] = 'upload_failed', 'upload_failed'
                self._save(key, job)
                return
            image['stage'], image['error'] = 'uploaded', None
            self._save(key, job)  # Permanent asset is safe before model work.
        if image.get('recognition') is not None:
            return
        image['stage'], image['error'] = 'identifying', None
        self._save(key, job)
        try:
            if raw is None:
                raw = self.backend.read_media(deepcopy(image['asset']))
                self._check_bytes(raw)
            result = self.identify(raw, job['data']['名稱'], list(self._roles(job)), image['purpose'])
            if not isinstance(result, dict):
                raise ValueError('invalid recognition')
            confidence = result.get('confidence')
            if (type(confidence) not in (float, int) or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1):
                confidence = 0
            role_name = result.get('roleName')
            role_name = role_name if isinstance(role_name, str) and len(role_name) <= 200 else None
            recognition = {'roleName': role_name, 'confidence': confidence,
                           'matchesScript': result.get('matchesScript') is True}
            image['recognition'] = recognition
            confident = recognition['matchesScript'] and confidence >= 0.95
            if image['purpose'] == 'cover':
                image['confirmed'] = confident
            elif confident and role_name in self._roles(job):
                used = any(other is not image and other['purpose'] == 'portraits'
                           and other.get('confirmed') and other.get('roleName') == role_name
                           for other in job['images'])
                if used:
                    image['error'] = 'duplicate_role'
                else:
                    image['confirmed'], image['roleName'] = True, role_name
            image['stage'] = 'ready'
        except Exception:
            image['stage'], image['error'] = 'identification_failed', 'identification_failed'
        self._save(key, job)

    def _check_bytes(self, raw):
        if not isinstance(raw, bytes) or not raw or len(raw) > self.max_image_bytes:
            raise ValueError('invalid image bytes')

    def _image_receipt(self, key, job, image):
        index = image['index']
        if not image.get('asset'):
            return self._record(key, job, False,
                                f'第 {index} 張圖片尚未存好，已保留訊息編號，請儘快傳「重試上架」；若 LINE 原圖已過期，才需要取消後重傳。',
                                status='media_failed', imageIndex=index)
        if image.get('confirmed'):
            label = '封面' if image['purpose'] == 'cover' else image['roleName']
            message = f'第 {index} 張已存好並配對為「{label}」。'
        elif image['purpose'] == 'cover':
            message = f'第 {index} 張封面已存好，但無法確定劇本名稱。請傳「確認圖片 {index} 為封面」，不用重傳。'
        else:
            prefix = '辨識到重複角色，' if image.get('error') == 'duplicate_role' else ''
            message = f'第 {index} 張角色圖已存好，{prefix}請傳「配對角色 {index}：角色完整名稱」，不用重傳。'
        if all(batch['closed'] for batch in job['batches']):
            message += f'\n本批已收滿；整理完成後傳「{FINISH_COMMAND}」。'
        return self._record(key, job, True, message, imageIndex=index)

    def assign_role(self, key, index, name):
        with _LOCK:
            job = self._load(key)
            blocked = self._blocked(job)
            if blocked:
                return blocked
            image = self._image(job, index, 'portraits')
            if not image or not image.get('asset'):
                return _response(False, '找不到已存好的這張角色圖，請先傳「上架狀態」確認編號。', job=job)
            if name not in self._roles(job):
                return _response(False, '角色名稱必須與名單完全相同，這次沒有配對。', job=job)
            if any(other is not image and other['purpose'] == 'portraits'
                   and other.get('confirmed') and other.get('roleName') == name for other in job['images']):
                return _response(False, '另一張圖片已配對這個角色，這次沒有覆蓋。', job=job)
            image.update(roleName=name, confirmed=True, stage='ready', error=None,
                         manuallyConfirmed=True)
            return self._record(key, job, True, f'第 {index} 張已配對「{name}」；仍需最後的上架指令。')

    def confirm_cover(self, key, index):
        with _LOCK:
            job = self._load(key)
            blocked = self._blocked(job)
            if blocked:
                return blocked
            image = self._image(job, index, 'cover')
            if not image or not image.get('asset'):
                return _response(False, '找不到已存好的這張封面，請先傳「上架狀態」確認編號。', job=job)
            image.update(confirmed=True, stage='ready', error=None, manuallyConfirmed=True)
            return self._record(key, job, True, f'第 {index} 張已確認為《{job["data"]["名稱"]}》封面；仍需最後的上架指令。')

    @staticmethod
    def _image(job, index, purpose):
        if type(index) is not int or not 1 <= index <= len(job['images']):
            return None
        image = job['images'][index - 1]
        return image if image['purpose'] == purpose else None

    def _issues(self, job):
        issues = []
        data = job['data']
        if job['kind'] == 'upload':
            required = ('名稱', '人數', '時長', '價格', '類型', '簡介')
            missing = [field for field in required
                       if data.get(field) is None or data.get(field) == '' or data.get(field) == []
                       or isinstance(data.get(field), str) and not data[field].strip()]
            if missing:
                issues.append('還缺欄位：' + '、'.join(missing))
        roles, people = self._roles(job), data.get('人數') or []
        bounds = [_people_bounds(item) for item in people if _people_bounds(item)]
        if any(item in _FLOATING_PEOPLE for item in people):
            issues.append('浮動人數還缺明確範圍，請把浮動標記改成例如 5～8人')
        numbers = sorted({number for low, high in bounds for number in range(low, high + 1)})
        if numbers and numbers != list(range(numbers[0], numbers[-1] + 1)):
            issues.append('人數範圍必須連續，請補齊範圍內的人數')
        if roles and len(people) == 1:
            match = re.fullmatch(r'(\d+)人', people[0])
            if match and len(roles) != int(match[1]):
                issues.append(f'角色有 {len(roles)} 位，與固定人數 {people[0]} 不符')
        elif roles and numbers and not numbers[0] <= len(roles) <= numbers[-1]:
            issues.append(f'角色有 {len(roles)} 位，與人數範圍 {numbers[0]}～{numbers[-1]}人不符')
        for batch in job['batches']:
            if not batch['closed']:
                issues.append(f"{'封面' if batch['purpose'] == 'cover' else '角色圖'}這批還差 {batch['expected'] - batch['received']} 張")
        cover = [image for image in job['images'] if image['purpose'] == 'cover']
        portraits = [image for image in job['images'] if image['purpose'] == 'portraits']
        if job['kind'] in {'upload', 'cover'} and len(cover) != 1:
            issues.append('需要 1 張已確認的封面')
        if job['kind'] == 'portraits' and not portraits:
            issues.append('尚未收到要補上的角色圖')
        seen = set()
        for image in job['images']:
            index = image['index']
            if not image.get('asset'):
                issues.append(f'第 {index} 張尚未存好，請重試上架')
            elif not image.get('confirmed'):
                issues.append(f'第 {index} 張還需要' + ('確認封面' if image['purpose'] == 'cover' else '人工配對角色'))
            elif image['purpose'] == 'portraits':
                role = image.get('roleName')
                if role not in roles:
                    issues.append(f'第 {index} 張角色不在目前名單')
                elif role in seen:
                    issues.append(f'角色「{role}」配到多張圖片')
                seen.add(role)
        return issues

    def finish(self, key):
        with _LOCK:
            job = self._load(key)
            if not job:
                return _response(False, '目前沒有上架任務，請先提供劇本名稱。')
            if job['stage'] in _TERMINAL:
                return deepcopy(job['lastResult'])
            if job.get('publishRequested'):
                return _response(False, '這筆已送出過發布，請傳「重試上架」查核並接續；不會重建一筆。', job=job)
            blocked = self._blocked(job)
            if blocked:
                return blocked
            issues = self._issues(job)
            if issues:
                return self._record(key, job, False, '尚未上架：\n' + '\n'.join(issues)
                                    + f'\n補齊後再傳「{FINISH_COMMAND}」。', status='needs_input')
            job['publishRequested'] = True
            job['stage'] = 'publishing'
            self._save(key, job)
            return self._publish(key, job)

    def _publish(self, key, job):
        job['stage'] = 'publishing'
        self._save(key, job)
        try:
            result = self.backend.publish(deepcopy(job))
        except Exception:
            result = {'status': 'failed'}
        if not isinstance(result, dict):
            result = {'status': 'failed'}
        if result.get('status') == 'published':
            job['stage'] = 'published'
            job['publishedResult'] = {field: result[field] for field in ('status', 'url', 'scriptId', 'versionId')
                                      if field in result and isinstance(result[field], str)}
            url = result.get('url')
            extra = {'url': url} if _public_url(url) else {}
            message = f"《{job['data']['名稱']}》已上架，資料同步與官網讀回已確認。"
            if extra:
                message += '\n' + url
            return self._record(key, job, True, message, **extra)
        job['stage'] = 'publish_failed'
        job['lastPublishStatus'] = result.get('status') if result.get('status') in {'pending', 'conflict', 'failed'} else 'failed'
        if job['lastPublishStatus'] == 'conflict':
            message = '正式版本已變動或已有其他草稿，沒有覆蓋他人的資料；任務已保留，請管理員確認後再重試。'
        elif job['lastPublishStatus'] == 'pending':
            message = '發布尚在同步或等待官網讀回，還不能算上架完成。資料與圖片已保留，請傳「重試上架」。'
        else:
            message = '發布尚未確認成功。資料與圖片已保留，請傳「重試上架」，不用重新傳圖。'
        return self._record(key, job, False, message)

    def retry(self, key):
        with _LOCK:
            job = self._load(key)
            if not job:
                return _response(False, '目前沒有可重試的上架任務。')
            if job['stage'] in _TERMINAL:
                return deepcopy(job['lastResult'])
            if job['stage'] in {'initializing', 'lookup_failed'}:
                return self._initialize(key, job)
            if job['stage'] == 'conflict':
                return deepcopy(job['lastResult'])
            if job.get('publishRequested'):
                return self._publish(key, job)
            for image in job['images']:
                if not image.get('asset') or (not image.get('confirmed') and image.get('recognition') is None):
                    self._process_image(key, job, image)
            return self.status(key)

    def status(self, key):
        with _LOCK:
            job = self._load(key)
            if not job:
                return _response(False, '目前沒有上架任務；請先提供劇本名稱。')
            if job['stage'] in _TERMINAL or job['stage'] in {'lookup_failed', 'conflict', 'publish_failed'}:
                return deepcopy(job['lastResult'])
            if job['stage'] in {'initializing', 'publishing'}:
                return _response(False, '任務上次處理尚未完成，資料已保留；請傳「重試上架」。', job=job)
            lines = [f"《{job['data']['名稱']}》尚未上架。"]
            for batch in job['batches']:
                lines.append(f"{'封面' if batch['purpose'] == 'cover' else '角色圖'}：{batch['received']}/{batch['expected']} 張")
            for image in job['images']:
                if not image.get('asset'):
                    label = '儲存失敗，請重試'
                elif image.get('confirmed'):
                    label = '封面已確認' if image['purpose'] == 'cover' else image['roleName']
                else:
                    label = '待確認封面' if image['purpose'] == 'cover' else '待人工配對'
                lines.append(f"第 {image['index']} 張：{label}")
            lines.append(f'整理完成後傳「{FINISH_COMMAND}」。')
            return _response(True, '\n'.join(lines), job=job,
                             images=[{'index': image['index'], 'purpose': image['purpose'],
                                      'stage': image['stage'], 'confirmed': image['confirmed'],
                                      'roleName': image.get('roleName')}
                                     for image in job['images']])

    def cancel(self, key):
        with _LOCK:
            job = self._load(key)
            if not job:
                return _response(True, '目前沒有上架任務。', status='cancelled')
            if job['stage'] == 'published':
                return _response(False, '這筆已上架；取消任務不會把官網資料下架。', job=job)
            if job['stage'] == 'cancelled':
                return deepcopy(job['lastResult'])
            job['stage'] = 'cancelled'
            return self._record(key, job, True, '已取消目前任務；已儲存的圖片或已寫入的草稿不會自動刪除。')
