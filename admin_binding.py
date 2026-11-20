"""Private administrator accounts, each bound with the same configured code.

The code is never stored, logged, or shown to the model. Only salted hashes of
LINE user IDs are persisted. Each bind merges into the current member list
under the store's revision check, so concurrent binds never overwrite each
other, and the first administrator's legacy field is kept unchanged.
"""
import hashlib
import hmac
import threading
import time


MIN_CODE_LENGTH = 20
BIND_ATTEMPTS = 4


class PrivateAdminBinding:
    KEY = ('private-admin-binding-v1',)

    def __init__(self, store, code, *, clock=time.time, cache_seconds=60):
        code = (code or '').strip()
        self.code = code if len(code) >= MIN_CODE_LENGTH else ''
        self.store, self.clock, self.cache_seconds = store, clock, cache_seconds
        self._lock = threading.Lock()
        self._members = frozenset()  # hashes known to be bound; members are never removed
        self._checked_at = None      # last successful read of the stored members

    @staticmethod
    def _identity(user_id):
        return hashlib.sha256(('line-private-admin:' + user_id).encode()).hexdigest()

    @staticmethod
    def _members_of(state):
        state = state if isinstance(state, dict) else {}
        listed = state.get('adminUserHashes')
        members = {h for h in listed if isinstance(h, str) and h} if isinstance(listed, list) else set()
        legacy = state.get('adminUserHash')  # The single-admin format before multi-admin.
        if isinstance(legacy, str) and legacy:
            members.add(legacy)
        return members

    def mentions_code(self, text):
        """Any message containing the code is a binding attempt, never material."""
        return bool(self.code and isinstance(text, str) and self.code in text)

    def _matches(self, text):
        return bool(self.code and isinstance(text, str)
                    and hmac.compare_digest(text.strip().encode(), self.code.encode()))

    def _read(self):
        state = self.store.get(self.KEY)
        members = self._members_of(state)
        self._members = frozenset(members)
        self._checked_at = self.clock()
        return state if isinstance(state, dict) else {}, members

    def is_admin(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            return False
        identity = self._identity(user_id)
        with self._lock:
            if identity in self._members:
                return True
            # Another worker may have bound this account since our last read.
            if self._checked_at is None or self.clock() - self._checked_at >= self.cache_seconds:
                try:
                    self._read()
                except Exception as exc:
                    print(f'[admin] binding read error={type(exc).__name__}')
                    return False
            return identity in self._members

    def try_bind(self, user_id, text):
        """Return 'bound', 'already', or None (silent: wrong code or unconfirmed storage)."""
        if not isinstance(user_id, str) or not user_id or not self._matches(text):
            return None
        identity = self._identity(user_id)
        with self._lock:
            wrote = False
            for _ in range(BIND_ATTEMPTS):
                try:
                    state, members = self._read()
                except Exception as exc:
                    print(f'[admin] binding read error={type(exc).__name__}')
                    continue
                if identity in members:
                    # A write whose response was lost may still have landed.
                    return 'bound' if wrote else 'already'
                merged = dict(state)
                merged['adminUserHashes'] = sorted(members | {identity})
                if not members:  # The first administrator also fills the legacy field.
                    merged['adminUserHash'] = identity
                    merged['boundAt'] = int(self.clock())
                try:
                    wrote = True
                    self.store.put(self.KEY, merged)
                except Exception as exc:
                    # Conflict or unknown outcome: reread, merge again, retry.
                    print(f'[admin] binding write error={type(exc).__name__}')
            # Acknowledge only after a read confirms durable membership.
            try:
                _, members = self._read()
            except Exception:
                return None
            return 'bound' if identity in members else None
