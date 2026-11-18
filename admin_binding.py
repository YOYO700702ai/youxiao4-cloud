"""One private administrator account, bound once with a configured code.

The code is never stored, logged, or shown to the model. Only a salted hash of
the LINE user ID is persisted; the store's revision check makes the first
writer win when two accounts race.
"""
import hashlib
import hmac
import threading
import time


MIN_CODE_LENGTH = 20


class PrivateAdminBinding:
    KEY = ('private-admin-binding-v1',)

    def __init__(self, store, code, *, clock=time.time, cache_seconds=60):
        code = (code or '').strip()
        self.code = code if len(code) >= MIN_CODE_LENGTH else ''
        self.store, self.clock, self.cache_seconds = store, clock, cache_seconds
        self._lock = threading.Lock()
        self._bound = None        # hash of the bound account, once known
        self._checked_at = None   # last time an unbound state was read

    @staticmethod
    def _identity(user_id):
        return hashlib.sha256(('line-private-admin:' + user_id).encode()).hexdigest()

    def mentions_code(self, text):
        """Any message containing the code is a binding attempt, never material."""
        return bool(self.code and isinstance(text, str) and self.code in text)

    def _matches(self, text):
        return bool(self.code and isinstance(text, str)
                    and hmac.compare_digest(text.strip().encode(), self.code.encode()))

    def _read(self):
        state = self.store.get(self.KEY) or {}
        bound = state.get('adminUserHash')
        self._bound = bound if isinstance(bound, str) and bound else None
        self._checked_at = self.clock()
        return self._bound

    def is_admin(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            return False
        identity = self._identity(user_id)
        with self._lock:
            if self._bound is None and (self._checked_at is None
                                        or self.clock() - self._checked_at >= self.cache_seconds):
                try:
                    self._read()
                except Exception as exc:
                    print(f'[admin] binding read error={type(exc).__name__}')
                    return False
            return self._bound is not None and hmac.compare_digest(self._bound, identity)

    def try_bind(self, user_id, text):
        """Return 'bound', 'already', or None (silent: wrong code, taken, or storage failure)."""
        if not isinstance(user_id, str) or not user_id or not self._matches(text):
            return None
        identity = self._identity(user_id)
        with self._lock:
            try:
                bound = self._read()
                if bound:
                    return 'already' if hmac.compare_digest(bound, identity) else None
                self.store.put(self.KEY, {'adminUserHash': identity, 'boundAt': int(self.clock())})
            except Exception as exc:
                print(f'[admin] binding write error={type(exc).__name__}')
            try:
                bound = self._read()  # Confirm the durable winner before acknowledging.
            except Exception:
                self._bound = None
                return None
            return 'bound' if bound and hmac.compare_digest(bound, identity) else None
