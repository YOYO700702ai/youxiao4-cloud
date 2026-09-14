"""Single-process, retryable script/cover workflow. No API clients at import time."""
import hashlib
import re
import threading
import time
from functools import wraps


SCRIPT_TOOLS = frozenset({'upload_script', 'update_script', 'remove_script', 'replace_cover'})


def result(ok, message, **extra):
    return {'ok': bool(ok), 'message': message, **extra}


def result_text(value):
    prefix = '✅ ' if value.get('ok') else ('⏳ ' if value.get('waiting_image') else '⚠️ ')
    return prefix + value['message']


class EventGate:
    """Suppress concurrent/completed webhook redeliveries in this process."""
    def __init__(self, ttl=86400, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self.seen = {}
        self.lock = threading.Lock()

    def wrap(self, fn):
        @wraps(fn)
        def handle(event):
            event_id = getattr(event, 'webhook_event_id', None) or getattr(getattr(event, 'message', None), 'id', None)
            if not event_id:
                return fn(event)
            with self.lock:
                now = self.clock()
                self.seen = {k: ts for k, ts in self.seen.items() if now - ts < self.ttl}
                if event_id in self.seen:
                    return None
                self.seen[event_id] = now
            try:
                return fn(event)
            except Exception:
                with self.lock:
                    self.seen.pop(event_id, None)
                raise
        return handle


class ScriptWorkflow:
    """One pending operation per (group, user); only consume state on success.

    State survives API failures, but not a process restart. A shared durable store
    is required before running multiple workers/replicas.
    """
    def __init__(self, *, images, validate, download, upload, create, replace,
                 ttl=900, clock=time.time):
        self.images = images
        self.validate, self.download, self.upload = validate, download, upload
        self.create, self.replace = create, replace
        self.ttl, self.clock = ttl, clock
        self.jobs, self.last_results, self.seen_images = {}, {}, {}
        self.lock = threading.RLock()

    def _prune(self):
        now = self.clock()
        for mapping in (self.last_results, self.seen_images):
            for key, (_, timestamp) in list(mapping.items()):
                if now - timestamp >= self.ttl:
                    mapping.pop(key, None)
        for key, (_, timestamp) in list(self.images.items()):
            if now - timestamp >= self.ttl:
                self.images.pop(key, None)
        for key, job in list(self.jobs.items()):
            if now - job['timestamp'] >= self.ttl:
                self.last_results[key] = (result(False, '待辦已超過15分鐘，請重新提供劇本資料與封面。'), now)
                self.jobs.pop(key, None)

    def start(self, key, kind, payload):
        with self.lock:
            self._prune()
            if not key or not all(key):
                return result(False, '無法辨識傳送者，請從群組重新發送。')
            if kind == 'upload':
                try:
                    payload = self.validate(payload)
                except ValueError as exc:
                    return result(False, str(exc))
                name = payload['名稱']
            elif kind == 'cover' and isinstance(payload, str) and payload.strip():
                payload = payload.strip()
                name = payload
            else:
                return result(False, '請提供完整劇本名稱。')
            job = self.jobs.get(key)
            if job and (job['kind'] != kind or job['payload'] != payload):
                return result(False, f"還有《{job['name']}》的待辦。請先完成，或傳「取消上架」再處理另一本，避免用錯封面。")
            if not job:
                self.jobs[key] = {'kind': kind, 'payload': payload, 'name': name,
                                  'timestamp': self.clock(), 'cover_url': None, 'image_id': None}
            return self.retry(key)

    def receive_image(self, key, message_id):
        with self.lock:
            self._prune()
            event_key = (*key, message_id)
            if event_key in self.seen_images:
                return None  # LINE redelivery: don't write or notify twice.
            self.seen_images[event_key] = (True, self.clock())
            self.images[key] = (message_id, self.clock())
            job = self.jobs.get(key)
            if not job:
                return None
            if job['image_id'] != message_id:
                job['cover_url'] = None
                job['image_id'] = message_id
            return self.retry(key)

    def retry(self, key):
        with self.lock:
            self._prune()
            job = self.jobs.get(key)
            if not job:
                return self.status(key)
            entry = self.images.get(key)
            if not entry and not job['cover_url']:
                return result(False, f"《{job['name']}》資料已保留，請在15分鐘內傳封面圖。傳「上架狀態」可查詢；「取消上架」可取消待辦。", waiting_image=True)
            stage = '下載 LINE 封面'
            try:
                if not job['cover_url']:
                    image_id = entry[0]
                    image_bytes = self.download(image_id)
                    if not image_bytes:
                        raise ValueError('empty image')
                    stage = '上傳 GitHub 封面'
                    # A new image has a new URL, avoiding stale raw GitHub caches
                    # and collisions caused by replacing punctuation in a title.
                    digest = hashlib.sha256(job['name'].encode() + image_bytes).hexdigest()[:16]
                    ext = 'png' if image_bytes.startswith(b'\x89PNG') else 'jpg'
                    safe_name = re.sub(r'[\\/*?:"<>|]', '_', job['name'])[:80]
                    job['cover_url'] = self.upload(image_bytes, f'{safe_name}-{digest}.{ext}')
                    if not isinstance(job['cover_url'], str) or not job['cover_url'].startswith('https://'):
                        job['cover_url'] = None
                        raise ValueError('invalid cover URL')
                    job['image_id'] = image_id
                stage = '新增 Notion 劇本' if job['kind'] == 'upload' else '更新 Notion 封面'
                fn = self.create if job['kind'] == 'upload' else self.replace
                ok, message = fn(job['payload'], job['cover_url'])
                if ok:
                    if job['kind'] == 'upload':
                        message = f"《{job['name']}》已新增到 Notion，封面已設定。\n{message}"
                    outcome = result(True, message)
                    self.jobs.pop(key, None)
                    self.images.pop(key, None)
                    self.last_results[key] = (outcome, self.clock())
                    return outcome
                outcome = result(False, f'{stage}未完成：{message}\n資料與已上傳封面保留15分鐘，可傳「重試上架」；也可傳「取消上架」。')
            except Exception as exc:
                # Never send a raw third-party exception/response to a LINE group.
                print(f'[script] stage={stage} error={type(exc).__name__}')
                outcome = result(False, f'{stage}失敗（{type(exc).__name__}）。資料已保留，請傳「重試上架」。若持續失敗，請管理員檢查該服務連線與憑證。')
            job['timestamp'] = self.clock()
            if key in self.images:
                self.images[key] = (self.images[key][0], self.clock())
            job['last_result'] = outcome
            return outcome

    def status(self, key):
        with self.lock:
            self._prune()
            job = self.jobs.get(key)
            if job:
                return job.get('last_result') or result(False, f"《{job['name']}》正在等封面，請在15分鐘內傳圖。", waiting_image=True)
            return self.last_results.get(key, (result(False, '目前沒有待上架資料；若服務曾重新啟動，請重傳資料與封面。'), 0))[0]

    def remember_result(self, key, outcome):
        with self.lock:
            self.last_results[key] = (outcome, self.clock())

    def take_chat_image(self, key, max_age=300):
        """Chat may consume a recent image only when no script owns it."""
        with self.lock:
            self._prune()
            if key in self.jobs:
                return None
            entry = self.images.get(key)
            if entry and self.clock() - entry[1] < max_age:
                return self.images.pop(key)
            return None

    def cancel(self, key):
        with self.lock:
            self.jobs.pop(key, None)
            self.images.pop(key, None)
            self.last_results.pop(key, None)
            return result(True, '已取消你的待辦資料。已經寫入的 Notion 頁面或封面檔案不會刪除。')
