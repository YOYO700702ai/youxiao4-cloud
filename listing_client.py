"""Restricted BGLARP catalog API client. No Notion or database secrets here."""
import copy
import hashlib
import json
import re
import threading
from urllib.parse import quote, urlsplit

import requests


class ListingServiceError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status
        self.public_message = message


def _digest(value):
    return hashlib.sha256(value).hexdigest()


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

    def _api(self, method, path, *, body=None, request_id='lookup', operation=None, params=None):
        if not self.token:
            raise ListingServiceError('官網上架連線尚未設定，請管理員檢查；這次沒有發布。')
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json',
                   'X-BGLARP-Request-Id': request_id}
        if operation:
            headers['Idempotency-Key'] = operation
        try:
            response = self.session.request(method, self.base_url + '/api/admin/scripts/bot' + path,
                                            json=body, params=params, headers=headers,
                                            timeout=(5, 65), allow_redirects=False)
        except requests.RequestException:
            raise ListingServiceError('官網連線逾時，資料已保留。請傳「重試上架」接續，勿重新建立同名劇本。') from None
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = {}
        if not 200 <= response.status_code < 300:
            message = payload.get('error', {}).get('message') if isinstance(payload.get('error'), dict) else payload.get('error')
            if not isinstance(message, str):
                message = None
            # Server errors never include authentication headers or upstream bodies.
            if response.status_code in (401, 403):
                message = '官網上架連線驗證未通過，請管理員檢查；資料已保留。'
            elif response.status_code == 409:
                message = message or '同名劇本或資料版本有衝突，請先查「上架狀態」，不要重複建立。'
            raise ListingServiceError(message or '官網未完成這次操作，資料已保留，請稍後重試。', response.status_code)
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
            raise ListingServiceError('圖片需小於 8 MB，請改傳 JPG、PNG 或 WebP。')
        mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None)
        if not mime:
            raise ListingServiceError('只接受 JPG、PNG 或 WebP 圖片。')
        sha = _digest(data)
        request_id = operation_key.split(':')[0]
        grant = self._api('POST', '/media', request_id=request_id,
                          operation='media-' + _digest(operation_key.encode())[:64],
                          body={'contentType': mime, 'size': len(data), 'sha256': sha, 'kind': kind})
        grant = grant.get('upload', grant)
        public = grant['publicUrl']
        self._check_asset_url(public, '/storage/v1/object/public/script-covers/')
        try:
            if not grant.get('alreadyUploaded'):
                signed = grant['signedUrl']
                self._check_asset_url(signed, '/storage/v1/object/upload/sign/script-covers/')
                response = self.session.put(signed, data=data, headers={'Content-Type': mime}, timeout=(5, 30), allow_redirects=False)
            # A prior upload may have succeeded despite a timeout. Verify bytes,
            # never overwrite a supposedly immutable path with different data.
                if response.status_code not in (200, 201, 400, 409):
                    raise ListingServiceError('圖片保存尚未完成，請傳「重試上架」。')
            saved = self.read_media({'url': public})
            if _digest(saved) != sha:
                raise ListingServiceError('圖片檢查不一致，這張尚未採用，請重試。')
        except requests.RequestException:
            raise ListingServiceError('圖片保存連線中斷，請傳「重試上架」。') from None
        return {'url': public, 'path': grant['path'], 'sha256': sha, 'size': len(data)}

    @staticmethod
    def _check_asset_url(url, prefix='/storage/v1/object/public/script-covers/'):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'rnipzotldpbdlzxjnoip.supabase.co' or not parsed.path.startswith(prefix):
            raise ListingServiceError('圖片位置不符合官網儲存設定。')

    def read_media(self, asset):
        url = asset['url']
        self._check_asset_url(url)
        try:
            response = self.session.get(url, timeout=(5, 20), allow_redirects=False, stream=True)
            if response.status_code != 200:
                raise ListingServiceError('已保存的圖片暫時無法讀取，請稍後重試。')
            output = bytearray()
            for chunk in response.iter_content(65536):
                output.extend(chunk)
                if len(output) > 8 * 1024 * 1024:
                    raise ListingServiceError('圖片超過 8 MB。')
            return bytes(output)
        except requests.RequestException:
            raise ListingServiceError('圖片讀取暫時失敗，請稍後重試。') from None

    def publish(self, job):
        try:
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
            version = payload.get('operationVersionId') or script['draftVersionId']
            path = '/' + quote(script['id'], safe='')
            outcome = self._api('POST', path + '/publish', body={'expectedVersionId': version},
                                request_id=request_id, operation='publish')
            publication = outcome.get('publication') or {}
            # The website checks Notion sync and public readback before this flag.
            if publication.get('state') == 'live' and publication.get('verified') is True:
                return {'status': 'published', 'url': publication['url'], 'scriptId': script['id'],
                        'versionId': version, 'message': '官網已上架完成。'}
            return {'status': 'pending', 'scriptId': script['id'], 'versionId': version,
                    'message': '資料已保存，官網同步尚未確認完成。請稍後傳「重試上架」接續。'}
        except ListingServiceError as exc:
            return {'status': 'conflict' if exc.status == 409 else 'failed', 'message': str(exc)}


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
