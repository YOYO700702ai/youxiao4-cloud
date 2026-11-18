"""Restricted BGLARP catalog API client. No Notion or database secrets here."""
import copy
import hashlib
import json
import re
import threading
import time
from urllib.parse import quote, urlsplit

import requests


class ListingServiceError(RuntimeError):
    def __init__(self, message, status=0, *, phase='catalog_api', code='service_error', retryable=False):
        super().__init__(message)
        # These small fields are safe to log. Never retain a request, signed
        # URL, transport exception, response body, or authentication headers.
        self.status = status if type(status) is int and 100 <= status <= 599 else 0
        self.phase = phase if isinstance(phase, str) and re.fullmatch(r'[a-z_]{1,40}', phase) else 'catalog_api'
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,40}', code) else 'service_error'
        self.retryable = bool(retryable and (self.status == 0 or self.status >= 500))
        self.public_message = message


def _digest(value):
    return hashlib.sha256(value).hexdigest()


MEDIA_BUDGET_SECONDS = 25
MEDIA_RETRY_DELAY = 0.2


def _transport_error(error, phase):
    if isinstance(error, requests.exceptions.SSLError):
        code, retryable = 'tls_error', False
    elif isinstance(error, requests.Timeout):
        code, retryable = 'timeout', True
    elif isinstance(error, (requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        code, retryable = 'connection_error', True
    else:
        code, retryable = 'transport_error', False
    return ListingServiceError('圖片連線暫時失敗，已保留資料，可以請我重試。',
                               phase=phase, code=code, retryable=retryable)


def _media_timeout(deadline, phase, read_seconds=6):
    remaining = deadline - time.monotonic()
    if remaining <= 0.1:
        raise ListingServiceError('這次圖片處理已達等待上限，已保留資料，可以請我重試。',
                                   phase=phase, code='budget_exhausted')
    return (min(2, remaining / 4), min(read_seconds, remaining * 3 / 4))


def _media_retry_pause(deadline):
    if deadline - time.monotonic() <= MEDIA_RETRY_DELAY + 0.1:
        return False
    time.sleep(MEDIA_RETRY_DELAY)
    return True


def _parts(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r'[/／、,，\n]', str(value or '')) if item.strip()]


def chinese_content(content):
    people = []
    if content.get('playerMin') is not None and content.get('playerMax') is not None:
        people = [f'{number}人' for number in range(content['playerMin'], content['playerMax'] + 1)]
    return {'名稱': content.get('name', ''), '人數': people,
            '價格': content.get('price'), '時長': content.get('durationLabel') or f"{content.get('durationMinutes', '')}分鐘",
            '類型': content.get('genres', []), '類型標籤': '、'.join(content.get('customTags', [])),
            '角色': [c['name'] for c in content.get('characters', [])], '簡介': content.get('synopsis', '')}


def build_content(job):
    data = job['data']
    baseline = job.get('baseline') or {}
    source = baseline.get('rawContent') or {}
    content = {key: copy.deepcopy(source[key]) for key in (
        'name', 'synopsis', 'playerMin', 'playerMax', 'durationMinutes', 'durationLabel',
        'priceStatus', 'price', 'genres', 'customTags', 'characters', 'cover', 'sortOrder') if key in source}
    if job.get('kind') in ('cover', 'portraits'):
        if not source:
            raise ListingServiceError('找不到原本的正式資料，請先查「上架狀態」。')
        for image in job.get('images', []):
            if not image.get('confirmed'):
                continue
            asset = image['asset']
            if job['kind'] == 'cover' and image.get('purpose') == 'cover':
                content['cover'] = {'url': asset['url'], 'path': asset.get('path', ''), 'alt': source['name']}
            elif job['kind'] == 'portraits' and image.get('purpose') == 'portraits':
                role = next((c for c in content.get('characters', []) if c['name'] == image.get('roleName')), None)
                if role is None:
                    raise ListingServiceError('角色名稱不在原本的名單中，請先確認配對。')
                role.update(image={'url': asset['url'], 'path': asset.get('path', ''), 'alt': role['name']}, display='card')
        return content
    people = []
    for item in _parts(data.get('人數')):
        interval = re.fullmatch(r'(\d+)\s*[~～\-–]\s*(\d+)人?', item)
        if interval:
            low, high = int(interval[1]), int(interval[2])
            if not 1 <= low <= high <= 30:
                raise ListingServiceError('人數範圍請填 1～30 人。')
            people.extend(f'{n}人' for n in range(low, high + 1))
        else:
            people.append(item)
    if not people or any(not re.fullmatch(r'[1-9]\d?人', item) for item in people):
        raise ListingServiceError('官網需要明確人數，請填「7人」或「6人／7人」。')
    numbers = sorted({int(p[:-1]) for p in people})
    if numbers[-1] > 30 or numbers != list(range(numbers[0], numbers[-1] + 1)):
        raise ListingServiceError('人數請填 1～30 人，範圍中的人數須連續。')
    old_characters = {item['name']: item for item in source.get('characters', [])}
    portraits = {item['roleName']: item for item in job.get('images', [])
                 if item.get('purpose') == 'portraits' and item.get('confirmed') and item.get('roleName')}
    characters = []
    for name in _parts(data.get('角色')):
        character = copy.deepcopy(old_characters.get(name, {'name': name, 'description': ''}))
        if name in portraits:
            image = portraits[name]
            character.update(image={'url': image['asset']['url'], 'path': image['asset'].get('path', ''), 'alt': name}, display='card')
        characters.append(character)
    cover = copy.deepcopy(source.get('cover') or baseline.get('cover') or {})
    covers = [item for item in job.get('images', []) if item.get('purpose') == 'cover' and item.get('confirmed')]
    cover_url = (covers[-1]['asset']['url'] if covers else None) or baseline.get('coverUrl') or cover.get('url')
    if cover_url:
        candidate = next((item for item in job.get('images', []) if item.get('purpose') == 'cover' and item.get('confirmed') and item.get('asset', {}).get('url') == cover_url), {})
        cover.update(url=cover_url, path=candidate.get('asset', {}).get('path', cover.get('path', '')), alt=data['名稱'])
    price = data.get('價格')
    if type(price) is not int or not 0 <= price <= 100000:
        raise ListingServiceError('售價請填 0～100000 的整數。')
    content.update(name=data['名稱'], synopsis=data.get('簡介', ''),
                   playerMin=numbers[0], playerMax=numbers[-1],
                   durationLabel=data.get('時長', ''), priceStatus='free' if price == 0 else 'fixed',
                   price=price, genres=_parts(data.get('類型')), customTags=_parts(data.get('類型標籤')),
                   characters=characters, cover=cover, sortOrder=content.get('sortOrder', 0))
    content.setdefault('durationMinutes', None)
    return content


class BotCatalogClient:
    def __init__(self, base_url, token, *, session=None):
        self.base_url = (base_url or 'https://www.bglarp.com').rstrip('/')
        if self.base_url != 'https://www.bglarp.com':
            raise ValueError('Only the official BGLARP catalog endpoint is allowed')
        self.token = token or ''
        self.session = session or requests.Session()

    def _api(self, method, path, *, body=None, request_id='lookup', operation=None, params=None, timeout=(5, 65)):
        phase = ('media_grant' if path == '/media' else
                 'job_read' if path.startswith('/jobs/') and method == 'GET' else
                 'job_save' if path.startswith('/jobs/') else 'catalog_api')
        if not self.token:
            raise ListingServiceError('官網上架連線尚未設定，請管理員檢查；這次沒有發布。',
                                       phase=phase, code='missing_credentials')
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json',
                   'X-BGLARP-Request-Id': request_id}
        if operation:
            headers['Idempotency-Key'] = operation
        try:
            response = self.session.request(method, self.base_url + '/api/admin/scripts/bot' + path,
                                            json=body, params=params, headers=headers,
                                            timeout=timeout, allow_redirects=False)
        except requests.RequestException as error:
            details = _transport_error(error, phase)
            raise ListingServiceError('官網連線暫時失敗，資料已保留。可以請我重試，勿重新建立同名劇本。',
                                       phase=phase, code=details.code, retryable=details.retryable) from None
        try:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
        finally:
            response.close()
        if not 200 <= response.status_code < 300:
            body_error = payload.get('error') if isinstance(payload, dict) else None
            message = body_error.get('message') if isinstance(body_error, dict) else body_error
            if not isinstance(message, str):
                message = None
            # Server errors never include authentication headers or upstream bodies.
            if response.status_code in (401, 403):
                message = '官網上架連線驗證未通過，請管理員檢查；資料已保留。'
            elif response.status_code == 409:
                message = message or '同名劇本或資料版本有衝突，請先查「上架狀態」，不要重複建立。'
            if phase == 'media_grant':
                message = ('圖片上傳連線驗證未通過，請管理員檢查。' if response.status_code in (401, 403)
                           else '圖片上傳準備尚未完成，資料已保留，可以請我重試。')
            raise ListingServiceError(message or '官網未完成這次操作，資料已保留，請稍後重試。', response.status_code,
                                       phase=phase, code='http_error', retryable=response.status_code >= 500)
        if not isinstance(payload, dict):
            raise ListingServiceError('官網回應格式不完整，資料已保留，可以請我重試。',
                                       phase=phase, code='invalid_response')
        return payload

    def lookup(self, name):
        payload = self._api('GET', '', params={'name': name})
        scripts = [s for s in payload.get('scripts', []) if s.get('name') == name]
        matches = payload.get('publicMatches', [])
        if len(scripts) > 1 or len(matches) > 1:
            raise ListingServiceError('找到多筆同名劇本，請管理員先確認，避免更新錯本。', 409)
        if not scripts:
            if matches:
                raise ListingServiceError('這本目前只有舊版資料，請先由員工後台匯入後再補圖。', 409)
            return None
        script = scripts[0]
        version = script.get('publishedVersionId')
        return {'id': script['id'], 'name': name, 'content': chinese_content(script),
                'data': chinese_content(script), 'rawContent': script,
                'versionId': script.get('draftVersionId'), 'publishedVersionId': version,
                'hasUnpublishedChanges': not version or script.get('draftVersionId') != version,
                'cover': script.get('cover', {}), 'coverUrl': script.get('cover', {}).get('url'),
                'characters': script.get('characters', [])}

    def upload_media(self, data, filename, operation_key, *, kind='cover'):
        if not isinstance(data, bytes) or not 0 < len(data) <= 8 * 1024 * 1024:
            raise ListingServiceError('圖片需小於 8 MB，請改傳 JPG、PNG 或 WebP。',
                                       phase='media_input', code='invalid_media')
        mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None)
        if not mime:
            raise ListingServiceError('只接受 JPG、PNG 或 WebP 圖片。',
                                       phase='media_input', code='invalid_media')
        if kind not in {'cover', 'character'} or not isinstance(operation_key, str) or not operation_key:
            raise ListingServiceError('圖片儲存資訊不完整，資料已保留。',
                                       phase='media_input', code='invalid_request')
        deadline = time.monotonic() + MEDIA_BUDGET_SECONDS
        sha = _digest(data)
        request_id = operation_key.split(':')[0]
        for attempt in range(2):
            try:
                # Replaying this grant addresses the same immutable hash/path.
                grant = self._api('POST', '/media', request_id=request_id,
                                  operation='media-' + _digest(operation_key.encode())[:64],
                                  body={'contentType': mime, 'size': len(data), 'sha256': sha, 'kind': kind},
                                  timeout=_media_timeout(deadline, 'media_grant'))
                break
            except ListingServiceError as error:
                if attempt or not error.retryable or not _media_retry_pause(deadline):
                    raise
        grant = grant.get('upload', grant)
        if (not isinstance(grant, dict) or not isinstance(grant.get('path'), str)
                or not grant['path'] or not isinstance(grant.get('publicUrl'), str)):
            raise ListingServiceError('圖片儲存回應不完整，資料已保留。',
                                       phase='media_grant', code='invalid_response')
        public = grant['publicUrl']
        self._check_asset_url(public, phase='media_grant')

        def verify(attempts=2):
            saved = self.read_media({'url': public}, _deadline=deadline, _attempts=attempts)
            if _digest(saved) != sha:
                raise ListingServiceError('圖片檢查不一致，這張尚未採用，可以請我重試。',
                                           phase='hash_check', code='hash_mismatch')

        if grant.get('alreadyUploaded'):
            verify()
        else:
            signed = grant.get('signedUrl')
            self._check_asset_url(signed, '/storage/v1/object/upload/sign/script-covers/', phase='media_grant')
            for attempt in range(2):
                put_error = None
                response = None
                try:
                    response = self.session.put(
                        signed, data=data, headers={'Content-Type': mime},
                        timeout=_media_timeout(deadline, 'storage_put', 8), allow_redirects=False)
                    status = response.status_code
                    if status not in (200, 201):
                        put_error = ListingServiceError('圖片尚未確認存好，資料已保留，可以請我重試。',
                                                        status, phase='storage_put', code='http_error',
                                                        retryable=status >= 500)
                        # 400/409 can be an immutable object already present;
                        # inspect bytes, but never repeat a rejected PUT.
                        if status not in (400, 409) and not put_error.retryable:
                            raise put_error
                except requests.RequestException as error:
                    put_error = _transport_error(error, 'storage_put')
                    if not put_error.retryable:
                        raise put_error from None
                finally:
                    if response is not None:
                        response.close()
                try:
                    # A timeout/5xx can follow a committed upload. Check that
                    # immutable object BEFORE considering one more PUT. The
                    # entire upload uses no more than two readback requests.
                    verify(attempts=1 if put_error or attempt else 2)
                    break
                except ListingServiceError as read_error:
                    retry_put = (put_error and put_error.retryable and not attempt
                                 and (read_error.status == 404 or read_error.retryable))
                    if retry_put and _media_retry_pause(deadline):
                        continue
                    if put_error and read_error.status == 404:
                        raise put_error from None
                    raise
        return {'url': public, 'path': grant['path'], 'sha256': sha, 'size': len(data)}

    @staticmethod
    def _check_asset_url(url, prefix='/storage/v1/object/public/script-covers/', *, phase='storage_readback'):
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
        except ValueError:
            parsed = None
        if (parsed is None or parsed.scheme != 'https' or parsed.netloc != 'rnipzotldpbdlzxjnoip.supabase.co'
                or not parsed.path.startswith(prefix)):
            raise ListingServiceError('圖片位置不符合官網儲存設定。', phase=phase, code='invalid_url')

    def read_media(self, asset, *, _deadline=None, _attempts=2):
        url = asset.get('url') if isinstance(asset, dict) else None
        self._check_asset_url(url)
        deadline = _deadline if _deadline is not None else time.monotonic() + 16.2
        for attempt in range(min(2, max(1, _attempts))):
            response = None
            failure = None
            try:
                response = self.session.get(url, timeout=_media_timeout(deadline, 'storage_readback'),
                                            allow_redirects=False, stream=True)
                if response.status_code != 200:
                    raise ListingServiceError('已保存的圖片暫時無法讀取，可以請我重試。', response.status_code,
                                               phase='storage_readback', code='http_error',
                                               retryable=response.status_code >= 500)
                output = bytearray()
                for chunk in response.iter_content(16384):
                    _media_timeout(deadline, 'storage_readback')
                    output.extend(chunk)
                    if len(output) > 8 * 1024 * 1024:
                        raise ListingServiceError('圖片超過 8 MB。', phase='storage_readback', code='invalid_media')
                if not output:
                    raise ListingServiceError('儲存圖片內容為空，這張尚未採用。',
                                               phase='storage_readback', code='invalid_media')
                return bytes(output)
            except requests.RequestException as error:
                failure = _transport_error(error, 'storage_readback')
            except ListingServiceError as error:
                failure = error
            finally:
                if response is not None:
                    response.close()
            if attempt + 1 >= min(2, max(1, _attempts)) or not failure.retryable or not _media_retry_pause(deadline):
                raise failure from None

    def verify_publication(self, job, *, timeout=None):
        """Read back this job's immutable operation version without publishing."""
        target = job.get('publicationTarget') or {}
        script_id, version = target.get('scriptId'), target.get('versionId')
        try:
            if not script_id or not version:
                if not (job.get('publishRequested') and job.get('lastPublishStatus') == 'pending'):
                    return {'status': 'unknown'}
                # Older persisted jobs discarded their operation version. The
                # exact original operation key resolves it without publishing.
                # A name lookup could instead find an unrelated later version.
                script_id, version = self._save_draft(job, require_operation_version=True)
                target = {'scriptId': script_id, 'versionId': version}
            read_options = {'timeout': timeout} if timeout is not None else {}
            payload = self._api('GET', '/' + quote(script_id, safe=''), request_id=job['jobId'], **read_options)
            script = payload.get('script') or {}
            # A same-name script or a later staff version is never this receipt.
            if script.get('id') != script_id or script.get('draftVersionId') != version:
                return {'status': 'conflict', **target}
            published = script.get('publishedVersionId')
            if published and published not in (version, job.get('expectedVersion')):
                return {'status': 'conflict', **target}
            publication = payload.get('publication') or {}
            if (published == version and script.get('status') == 'published'
                    and script.get('syncStatus') == 'synced'
                    and publication.get('state') == 'live' and publication.get('verified') is True):
                return {'status': 'published', 'url': publication['url'],
                        'scriptId': script_id, 'versionId': version}
            return {'status': 'pending', 'scriptId': script_id, 'versionId': version,
                    'retryable': (published == version and script.get('status') == 'published'
                                  and script.get('syncStatus') == 'synced'
                                  and publication.get('state') == 'verification_pending')}
        except ListingServiceError as exc:
            if timeout is not None and exc.status == 0:
                # The publish POST already succeeded. A deliberately short
                # readback timing out does not turn that receipt into failure.
                return {'status': 'pending', 'retryable': False, **target}
            return {'status': 'conflict' if exc.status == 409 else 'failed', 'message': str(exc), **target}

    def _read_back_after_publish(self, job, target, started_at):
        # Route-handler revalidation is flushed after its response. A separate
        # GET can therefore observe the new public page without another write.
        # Keep this brief enough to leave time for the original LINE receipt;
        # a slow save/publish must not acquire another full 65-second timeout.
        deadline = min(started_at + 40, time.monotonic() + 8)
        result = {'status': 'pending', **target}
        for delay in (0.25, 0.75):
            remaining = deadline - time.monotonic()
            if remaining <= delay + 0.25:
                break
            time.sleep(delay)
            remaining = deadline - time.monotonic()
            if remaining <= 0.25:
                break
            timeout = (min(1, remaining / 4), min(5, remaining * 3 / 4))
            result = self.verify_publication({**job, 'publicationTarget': target}, timeout=timeout)
            if result.get('status') != 'pending' or not result.get('retryable'):
                break
        return result

    def _save_draft(self, job, *, require_operation_version=False):
        request_id = job['jobId']
        content = build_content(job)
        if job['kind'] == 'upload':
            payload = self._api('POST', '', body={'content': content}, request_id=request_id, operation='create')
        else:
            baseline = job['baseline']
            payload = self._api('PATCH', '/' + quote(baseline['id'], safe=''),
                                body={'content': content, 'expectedVersionId': job['expectedVersion']},
                                request_id=request_id, operation='update')
        script = payload['script']
        if require_operation_version and not payload.get('operationVersionId'):
            raise ListingServiceError('找不到這次操作原本的版本，資料已保留，請管理員確認。')
        return script['id'], payload.get('operationVersionId') or script['draftVersionId']

    def publish(self, job):
        target = {}
        started_at = time.monotonic()
        try:
            request_id = job['jobId']
            script_id, version = self._save_draft(job)
            target = {'scriptId': script_id, 'versionId': version}
            path = '/' + quote(script_id, safe='')
            outcome = self._api('POST', path + '/publish', body={'expectedVersionId': version},
                                request_id=request_id, operation='publish')
            publication = outcome.get('publication') or {}
            # The website checks Notion sync and public readback before this flag.
            if publication.get('state') == 'live' and publication.get('verified') is True:
                return {'status': 'published', 'url': publication['url'], 'scriptId': script_id,
                        'versionId': version, 'message': '官網已上架完成。'}
            if publication.get('state') == 'verification_pending':
                readback = self._read_back_after_publish(job, target, started_at)
                if readback.get('status') != 'pending':
                    return readback
            return {'status': 'pending', 'scriptId': script_id, 'versionId': version,
                    'message': '資料已保存，官網同步尚未確認完成。請稍後傳「重試上架」接續。'}
        except ListingServiceError as exc:
            return {'status': 'conflict' if exc.status == 409 else 'failed', 'message': str(exc), **target}


class RemoteJobStore:
    """CAS persistence; no secret or LINE raw identifiers enter the database."""
    def __init__(self, client):
        self.client = client
        self.revisions = {}
        self.lock = threading.RLock()

    @staticmethod
    def key_id(key):
        return _digest(json.dumps(list(key), ensure_ascii=False, separators=(',', ':')).encode())

    def get(self, key):
        ident = self.key_id(key)
        with self.lock:
            value = self.client._api('GET', '/jobs/' + ident, request_id='job-' + ident[:64])
            self.revisions[ident] = value['revision']
            return copy.deepcopy(value.get('state'))

    def put(self, key, state):
        ident = self.key_id(key)
        with self.lock:
            if ident not in self.revisions:
                self.get(key)
            # Only an opaque key belongs in remote state, never group/user IDs.
            state = copy.deepcopy(state)
            if state is not None:
                state['key'] = ident
            encoded = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
            if len(encoded) > 450000:
                raise ListingServiceError('這次上架資料過大，請縮短簡介或分批處理。')
            operation = f"save-{self.revisions[ident]}-{_digest(encoded)[:24]}"
            response = self.client._api('PUT', '/jobs/' + ident, request_id='job-' + ident[:64], operation=operation,
                                        body={'state': state, 'expectedRevision': self.revisions[ident]})
            self.revisions[ident] = response['revision']
