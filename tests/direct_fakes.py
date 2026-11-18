"""Offline fakes: website bot API (jobs + catalog + media) and Anthropic HTTP."""
import copy
import hashlib
import json
import threading
import uuid

from listing_client import ListingServiceError, RemoteJobStore
from claude_adapter import ClaudeClient

PUBLIC = 'https://rnipzotldpbdlzxjnoip.supabase.co/storage/v1/object/public/script-covers/'
JPEG = b'\xff\xd8\xff' + b'test-image'


class FakeWebsite:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}          # id -> (revision, state)
        self.scripts = {}       # id -> script dict
        self.replays = {}       # (requestId, operation) -> response
        self.writes = []        # (method, path) of catalog mutations
        self.uploads = []
        self.fail = {}          # (method, path-kind) -> 'before' | 'after'
        self.job_get_hook = None
        self.on_call = None     # (method, path) -> None; runs before each request
        self.sync_fails = False

    # The real BotCatalogClient._api signature.
    def _api(self, method, path, *, body=None, request_id='lookup', operation=None, params=None, timeout=None):
        if self.on_call:
            self.on_call(method, path)
        if path.startswith('/jobs/'):
            return self._job(method, path[6:], body)
        kind = ('publish' if path.endswith('/publish') else 'sync' if path.endswith('/sync')
                else 'draft' if method in ('POST', 'PATCH') else 'read')
        mode = self.fail.pop((method, kind), None)
        if mode == 'before':
            raise ListingServiceError('官網連線暫時失敗', phase='catalog_api', code='timeout', retryable=True)
        with self.lock:
            result = self._catalog(method, path, body, request_id, operation, params)
        if mode == 'after':
            raise ListingServiceError('官網連線暫時失敗', phase='catalog_api', code='timeout', retryable=True)
        return result

    def _job(self, method, ident, body):
        if method == 'GET':
            if self.job_get_hook:
                self.job_get_hook(ident)
            with self.lock:
                revision, state = self.jobs.get(ident, (0, None))
                return {'state': copy.deepcopy(state), 'revision': revision}
        with self.lock:
            revision, _ = self.jobs.get(ident, (0, None))
            if body['expectedRevision'] != revision:
                raise ListingServiceError('案件剛被其他訊息更新', 409, phase='job_save', code='http_error')
            json.dumps(body['state'])
            self.jobs[ident] = (revision + 1, copy.deepcopy(body['state']))
            return {'state': body['state'], 'revision': revision + 1}

    def _catalog(self, method, path, body, request_id, operation, params):
        if method == 'GET' and path == '':
            found = [copy.deepcopy(s) for s in self.scripts.values() if s['name'] == params['name']]
            return {'scripts': found, 'publicMatches': []}
        replay = self.replays.get((request_id, operation))
        if replay is not None and method != 'GET':
            return copy.deepcopy(replay)
        if method == 'POST' and path == '':
            if any(s['name'] == body['content']['name'] for s in self.scripts.values()):
                raise ListingServiceError('已有同名劇本，沒有建立重複劇本。', 409)
            ident, version = str(uuid.uuid4()), str(uuid.uuid4())
            self.scripts[ident] = {'id': ident, 'status': 'draft', 'syncStatus': 'pending',
                                   'draftVersionId': version, 'publishedVersionId': None, **body['content']}
            response = {'script': copy.deepcopy(self.scripts[ident]), 'operationVersionId': version}
        elif method == 'PATCH':
            script = self.scripts[path[1:]]
            if script['draftVersionId'] != body['expectedVersionId']:
                raise ListingServiceError('草稿已被更新', 409)
            version = str(uuid.uuid4())
            script.update(body['content'], draftVersionId=version)
            response = {'script': copy.deepcopy(script), 'operationVersionId': version}
        elif method == 'POST' and path.endswith('/publish'):
            script = self.scripts[path[1:-8]]
            if script['draftVersionId'] != body['expectedVersionId']:
                raise ListingServiceError('草稿已被更新', 409)
            script.update(status='published', syncStatus='error' if self.sync_fails else 'synced',
                          publishedVersionId=script['draftVersionId'])
            response = {'script': copy.deepcopy(script), 'publication': self._publication(script)}
        elif method == 'POST' and path.endswith('/sync'):
            script = self.scripts[path[1:-5]]
            if script['publishedVersionId'] != body['expectedVersionId']:
                raise ListingServiceError('這個版本尚未發布', 409)
            script['syncStatus'] = 'synced'
            response = {'script': copy.deepcopy(script), 'publication': self._publication(script)}
        elif method == 'GET':
            script = self.scripts[path[1:]]
            return {'script': copy.deepcopy(script), 'publication': self._publication(script)}
        else:
            raise AssertionError((method, path))
        self.writes.append((method, path))
        self.replays[(request_id, operation)] = copy.deepcopy(response)
        return response

    @staticmethod
    def _publication(script):
        if script['status'] != 'published':
            return {'state': 'draft', 'verified': False, 'url': None}
        if script['syncStatus'] != 'synced':
            return {'state': 'sync_pending', 'verified': False, 'url': None}
        return {'state': 'live', 'verified': True, 'url': 'https://www.bglarp.com/scripts/' + script['name']}

    def upload_media(self, data, filename, operation_key, *, kind='cover'):
        sha = hashlib.sha256(data).hexdigest()
        path = f'bot/{operation_key}/{kind}/{sha}.jpg'
        self.uploads.append(operation_key)
        return {'url': PUBLIC + path, 'path': path, 'sha256': sha, 'size': len(data)}


class ScriptedAnthropic:
    """requests.Session stand-in; each step maps the request payload to content blocks."""

    def __init__(self, steps):
        self.steps, self.payloads = list(steps), []

    def post(self, url, *, headers, json, timeout, allow_redirects):
        self.payloads.append(copy.deepcopy(json))
        self.timeouts = getattr(self, 'timeouts', []) + [timeout]
        blocks = self.steps.pop(0)(json)
        stop = 'tool_use' if any(b['type'] == 'tool_use' for b in blocks) else 'end_turn'
        payload = {'id': 'msg', 'role': 'assistant', 'model': json['model'], 'content': blocks,
                   'stop_reason': stop, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        return type('R', (), {'status_code': 200, 'json': lambda self: payload})()

    def close(self):
        pass


def text(value):
    return lambda payload: [{'type': 'text', 'text': value}]


def tool(name, args, call_id=None):
    return {'type': 'tool_use', 'id': call_id or 'toolu_' + uuid.uuid4().hex[:12], 'name': name, 'input': args}


def last_user_blocks(payload):
    return payload['messages'][-1]['content']


def first_json(payload):
    return json.loads(payload['messages'][0]['content'][0]['text'])


def make_store(site):
    return RemoteJobStore(site)


def claude(steps):
    http = ScriptedAnthropic(steps)
    return ClaudeClient('test-key', model='claude-opus-5-5', session=http), http
