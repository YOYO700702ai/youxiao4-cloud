"""Conversation-first collection backed by the existing durable publisher.

The model interprets real user messages and, when requested, all saved images.
It never publishes directly. Publication uses ListingWorkflow's existing
version checks, operation keys, and verified public readback.
"""
from copy import deepcopy
from hashlib import sha256
import logging
import re
import time
from uuid import uuid4

from listing_workflow import (
    ListingWorkflow, WorkflowStorageError, MAX_IMAGE_BYTES, _LOCK, _TERMINAL,
    _https_url, _response, _seconds, normalize_listing_data,
)


class AssistantListingWorkflow(ListingWorkflow):
    CAPTURE_VERSION = 1
    CAPTURE_IDLE_SECONDS = 15 * 60
    PENDING_INTAKE_SECONDS = 120

    def __init__(self, *, analyze, identify=None, clock=time.time,
                 max_image_bytes=MAX_IMAGE_BYTES, return_chat_image=None, **kwargs):
        super().__init__(identify=identify, clock=clock,
                         max_image_bytes=max_image_bytes, **kwargs)
        self.analyze = analyze
        self.return_chat_image = return_chat_image
        self._running = set()
        self._known_active = set()
        self._inactive_until = {}
        self._intake_owners = {}

    def _queue_intake(self, key, job, event):
        pending = job.get('pendingIntake')
        if (not pending or self._intake_owners.get(tuple(key)) != pending.get('sourceMessageId')
                or self.clock() - pending.get('lastActivityAt', 0) >= self.PENDING_INTAKE_SECONDS):
            return None
        if event['messageId'] not in {item['messageId'] for item in pending['events']}:
            pending['events'].append(event)
            pending['lastActivityAt'] = self.clock()
            self._save(key, job)
        return _response(True, '', job=job, status='deferred')

    def _start_intake(self, key, job, source):
        previous = job.get('pendingIntake')
        if previous and previous.get('events') and previous.get('sourceMessageId') != source['messageId']:
            job['interruptedIntake'] = deepcopy(previous)
        self._intake_owners[tuple(key)] = source['messageId']
        job['pendingIntake'] = {'version': 1, 'sourceMessageId': source['messageId'],
                                'lastActivityAt': self.clock(), 'events': []}

    def _return_queued_chat_image(self, key, events):
        latest = next((event for event in reversed(events) if event['type'] == 'image'), None)
        if latest and self.return_chat_image:
            self.return_chat_image(tuple(key), latest['messageId'])

    def _drain_intake(self, key, source_id, outcome):
        """Only confirmed scope consumes queued events; candidate events never
        enter the opening intent prompt or download media before that decision.
        """
        try:
            while True:
                with _LOCK:
                    job = self._load(key)
                    pending = (job or {}).get('pendingIntake')
                    if not pending or pending.get('sourceMessageId') != source_id:
                        return outcome
                    expired = self.clock() - pending.get('lastActivityAt', 0) >= self.PENDING_INTAKE_SECONDS
                    failed = outcome and outcome.get('status') in {'analysis_failed', 'media_failed', 'needs_input'}
                    rejected = job['stage'] == 'cancelled' or (not self._capture_active(job)
                                and (self._capture(job) or {}).get('closedReason') != 'complete')
                    if expired or failed or rejected:
                        events = list(pending['events'])
                        job.pop('pendingIntake', None)
                        self._save(key, job)
                        self._return_queued_chat_image(key, events)
                        return outcome
                    if not pending['events']:
                        job.pop('pendingIntake', None)
                        self._save(key, job)
                        return outcome
                    event = deepcopy(pending['events'][0])
                    pending['processingMessageId'] = event['messageId']
                    pending['lastActivityAt'] = self.clock()
                    self._save(key, job)
                if event['type'] == 'image':
                    current = self.receive_image(key, event['messageId'], event.get('timestamp'),
                                                 event.get('imageSet'), _from_pending=True)
                    if current.get('status') == 'ignored':
                        self._return_queued_chat_image(key, [event])
                else:
                    current = self.handle_text(key, event['text'], event['messageId'],
                                               event.get('timestamp'), event.get('eligible', False),
                                               _from_pending=True)
                with _LOCK:
                    updated = self._load(key)
                    remaining = (updated or {}).get('pendingIntake')
                    if remaining and remaining.get('sourceMessageId') == source_id:
                        remaining['events'] = [item for item in remaining['events']
                                               if item['messageId'] != event['messageId']]
                        remaining.pop('processingMessageId', None)
                        self._save(key, updated)
                if current and current.get('status') not in {'ignored', 'duplicate', 'deferred'}:
                    outcome = current
        finally:
            if self._intake_owners.get(tuple(key)) == source_id:
                self._intake_owners.pop(tuple(key), None)

    def _pending_actionable_events(self, job):
        pending = job.get('pendingIntake', {})
        for event in pending.get('events', []):
            if event['messageId'] == pending.get('processingMessageId'):
                continue
            if event['type'] == 'image':
                if (event['messageId'] not in job['processedEvents'] and
                        self._late_image_allowed(job, event.get('timestamp'), event.get('imageSet'))):
                    return True
            elif (event.get('eligible') or self._late_image_allowed(job, event.get('timestamp'), None)):
                # A direct stop/cancel can arrive after "finished sending".
                # Interpret it before publication, without letting later
                # ordinary group chatter enter the task.
                return True
        return False

    def _capture(self, job):
        capture = (job or {}).get('captureSession')
        return capture if isinstance(capture, dict) and capture.get('version') == self.CAPTURE_VERSION else None

    def _capture_active(self, job):
        capture = self._capture(job)
        return bool(capture and capture.get('active') and
                    self.clock() - capture.get('lastActivityAt', 0) < self.CAPTURE_IDLE_SECONDS
                    and job['stage'] not in _TERMINAL and not job.get('publishRequested'))

    def has_active_capture(self, key):
        """Read-only evidence for whether an image-error receipt is appropriate."""
        try:
            with _LOCK:
                return self._capture_active(self._load(key))
        except (WorkflowStorageError, ValueError):
            return False

    def _close_capture(self, job, reason, *, completed_at=None, allow_late=False):
        capture = self._capture(job)
        if not capture:
            return
        was_active = capture.get('active', False)
        capture.update(active=False, closedReason=reason, closedAt=self.clock())
        if allow_late and was_active:
            capture['completedAt'] = completed_at
            capture['allowedImageSets'] = {
                ident: group['total'] for ident, group in job.get('imageSets', {}).items()
                if group.get('captureSessionId') == capture['id'] and len(group['indices']) < group['total']}
        elif not allow_late:
            capture['completedAt'] = None
            capture['allowedImageSets'] = {}

    def _expire_capture(self, key, job):
        capture = self._capture(job)
        expired = bool(capture and ((capture.get('active') and not self._capture_active(job)) or
                       (capture.get('closedReason') == 'complete' and
                        self.clock() - capture.get('closedAt', 0) >= self.CAPTURE_IDLE_SECONDS)))
        if expired:
            self._close_capture(job, 'idle')
            job['analysisPending'] = False
            self._known_active.discard(tuple(key))
            try:
                self._save(key, job)
            except WorkflowStorageError:
                # Expiry still excludes this unsolicited event when the store
                # is unavailable; do not turn it into a group error message.
                pass

    def _open_capture(self, key, job, source):
        if self._capture_active(job):
            job['captureSession']['lastActivityAt'] = self.clock()
            self._known_active.add(tuple(key))
            return
        job['captureSession'] = {
            'version': self.CAPTURE_VERSION, 'id': str(uuid4()), 'active': True,
            'openedAt': self.clock(), 'lastActivityAt': self.clock(),
            'startedAt': source.get('timestamp'), 'startMessageId': source['messageId'],
            'completedAt': None, 'allowedImageSets': {}, 'closedReason': None,
        }
        # Resuming a draft starts a new collection round, not an automatic
        # continuation of a previous "everything has been sent" instruction.
        job.setdefault('publishConsent', {})['complete'] = False
        job['publishConsent'].pop('completionEvidenceMessageId', None)
        job['analysisPending'] = False
        self._known_active.add(tuple(key))

    @staticmethod
    def _image_set_values(image_set):
        if not image_set:
            return None
        if not isinstance(image_set, dict):
            image_set = {field: getattr(image_set, field, None) for field in ('id', 'index', 'total')}
        ident, index, total = (image_set.get(field) for field in ('id', 'index', 'total'))
        if not isinstance(ident, str) or not ident or type(index) is not int or type(total) is not int or not 1 <= index <= total <= 100:
            return None
        return ident, index, total

    def _late_image_allowed(self, job, event_timestamp, image_set):
        capture = self._capture(job)
        if not capture or capture.get('active') or capture.get('closedReason') != 'complete':
            return False
        if self.clock() - capture.get('closedAt', 0) >= self.CAPTURE_IDLE_SECONDS:
            return False
        values = self._image_set_values(image_set)
        timestamp = _seconds(event_timestamp)
        start, completed = capture.get('startedAt'), capture.get('completedAt')
        if timestamp is None or start is None or completed is None or not start <= timestamp <= completed:
            return False
        if values:
            ident, index, total = values
            allowed_total = capture.get('allowedImageSets', {}).get(ident)
            group = job.get('imageSets', {}).get(ident, {})
            # A known album member is tied to this exact collection round.
            return (allowed_total == total and index not in group.get('indices', [])
                    and group.get('captureSessionId') == capture['id'])
        # A standalone delayed event can only be attributed when its LINE
        # timestamp is bounded by this round's explicit start and finish.
        return image_set is None

    def _seal_for_analysis(self, job):
        if not self._capture_active(job):
            return
        consent = job.get('publishConsent', {})
        source = next((message for message in job.get('messages', [])
                       if message['messageId'] == consent.get('completionEvidenceMessageId')), {})
        complete = bool(consent.get('complete'))
        self._close_capture(job, 'complete' if complete else 'organize',
                            completed_at=source.get('timestamp'), allow_late=complete)

    @staticmethod
    def _log_analysis_error(phase, error):
        error_type = getattr(error, 'error_type', '')
        safe_type = error_type if isinstance(error_type, str) and re.fullmatch(r'[A-Za-z_]{1,64}', error_type) else ''
        logging.getLogger(__name__).warning('listing_analysis_failed phase=%s class=%s type=%s',
                                           phase, type(error).__name__[:80], safe_type)

    def _new_job(self, data=None, kind='upload'):
        data = deepcopy(data or {})
        known = bool(data.get('名稱'))
        data.setdefault('名稱', '尚未命名的劇本')
        ident = str(uuid4())
        return {
            'schemaVersion': 1, 'assistant': True, 'jobId': ident, 'kind': kind,
            'data': data, 'requestedData': deepcopy(data), 'nameKnown': known,
            'createdAt': self.clock(), 'updatedAt': self.clock(),
            'stage': 'collecting', 'baseline': None, 'expectedVersion': None,
            'batches': [], 'images': [], 'processedEvents': [], 'messages': [],
            'publishRequested': False, 'publishOperationKey': f'{ident}:publish',
            'lastResult': None, 'publishConsent': {}, 'questions': [],
            'inputRevision': 0, 'analyzedRevision': -1, 'analysisPending': False,
            'imageSets': {}, 'imageSetIssues': [], 'resolvedIdentity': None,
        }

    def _migrate(self, key, job):
        if job.get('assistant') or job.get('publishRequested') or job['stage'] in _TERMINAL:
            return job
        job.update(assistant=True, nameKnown=True, messages=[], publishConsent={},
                   questions=[], inputRevision=0, analyzedRevision=-1,
                   analysisPending=False, imageSets={}, imageSetIssues=[])
        job['resolvedIdentity'] = ([job['kind'], job['data']['名稱']]
                                   if job['stage'] == 'collecting' else None)
        job['legacyBatches'] = deepcopy(job.get('batches', []))
        job['batches'] = []
        for entry in job['images']:
            # Old explicit labels remain useful context; manual assignments
            # remain authoritative even when a later model disagrees.
            entry['declaredPurpose'] = entry.get('purpose')
        self._save(key, job)
        return job

    def begin(self, key, data=None, kind='upload'):
        with _LOCK:
            if kind not in {'upload', 'cover', 'portraits'}:
                return _response(False, '不支援這種上架任務。')
            try:
                normalized = normalize_listing_data(data, require_name=False) if data else {}
            except ValueError as error:
                return _response(False, str(error))
            current = self._load(key)
            if current and current['stage'] not in _TERMINAL:
                if current.get('publishRequested'):
                    return super().status(key)
                current = self._migrate(key, current)
                if (current.get('nameKnown') and normalized.get('名稱')
                        and normalized['名稱'] != current['data']['名稱']):
                    return _response(False, '目前還有另一本劇本待完成，請先完成或取消。', job=current)
                if current.get('baseline') and any(current['data'].get(field) != value for field, value in normalized.items()):
                    return _response(False, '補圖任務沿用正式資料；這次沒有改動原本的文字。', job=current)
                current['data'].update(normalized)
                current['nameKnown'] = current.get('nameKnown') or bool(normalized.get('名稱'))
                current['inputRevision'] += 1
                job = current
            else:
                job = self._new_job(normalized, kind)
            self._inactive_until.pop(tuple(key), None)
            return self._record(key, job, True, '草稿已保留；叫我開始或繼續收件後，再傳文字和圖片。')

    def handle_text(self, key, text, message_id=None, event_timestamp=None, eligible=False, *, _from_pending=False):
        if not isinstance(text, str) or not text.strip():
            return None
        key = tuple(key)
        message_id = message_id or str(uuid4())
        with _LOCK:
            if not eligible and self._inactive_until.get(key, 0) > self.clock():
                return None
            try:
                job = self._load(key)
            except WorkflowStorageError:
                if not eligible:
                    return None
                raise
            if job and not _from_pending:
                queued = self._queue_intake(key, job, {'type': 'text', 'messageId': message_id or str(uuid4()),
                                                      'text': text, 'timestamp': _seconds(event_timestamp),
                                                      'eligible': bool(eligible)})
                if queued:
                    return queued
            if job:
                self._expire_capture(key, job)
            late_text = bool(_from_pending and self._late_image_allowed(job, event_timestamp, None))
            if not eligible and not self._capture_active(job) and not late_text:
                return None
            if job and message_id and any(m['messageId'] == message_id for m in job.get('messages', [])):
                return deepcopy(job['lastResult']) or _response(True, '這則資料已收到。', job=job)
            created = job is None
            if job and (job.get('publishRequested') or job['stage'] in _TERMINAL):
                if job['stage'] in _TERMINAL and not eligible:
                    return None
                closed_job = deepcopy(job)
            else:
                closed_job = None
            if closed_job is not None:
                # Interpret the new message before deciding whether this is a
                # status query, an explicit retry, or a genuinely new task.
                if not _from_pending:
                    self._start_intake(key, job, {'messageId': message_id})
                    self._save(key, job)
            elif not job:
                if not eligible:
                    self._inactive_until[key] = self.clock() + 30
                    return None
                job = self._new_job()
            else:
                job = self._migrate(key, job)
            if closed_job is None:
                self._inactive_until.pop(key, None)
                source = {'role': 'user', 'messageId': message_id or str(uuid4()),
                          'text': text, 'timestamp': _seconds(event_timestamp), 'addressed': bool(eligible)}
                job['messages'].append(source)
                job['inputRevision'] += 1
                if not _from_pending:
                    self._start_intake(key, job, source)
                self._save(key, job)
        if closed_job is not None:
            outcome = self._route_closed_text(key, closed_job, text, message_id, event_timestamp)
            return outcome if _from_pending else self._drain_intake(key, message_id, outcome)
        try:
            outcome = self._analyze(key, phase='intent', new_candidate=created, source_message_id=source['messageId'])
        except Exception:
            if not _from_pending:
                self._intake_owners.pop(key, None)
            raise
        return outcome if _from_pending else self._drain_intake(key, source['messageId'], outcome)

    def _route_closed_text(self, key, previous, text, message_id, event_timestamp):
        """A published receipt and a frozen publication never become blank jobs
        merely because someone asks about them. No model patch touches a
        publication that has already been submitted.
        """
        terminal = previous['stage'] in _TERMINAL
        context_job = self._new_job() if terminal else deepcopy(previous)
        context_job.setdefault('messages', [])
        if not terminal:
            context_job.setdefault('nameKnown', True)
        latest = {'role': 'user', 'messageId': message_id or str(uuid4()),
                  'text': text, 'timestamp': _seconds(event_timestamp), 'addressed': True}
        context_job['messages'].append(latest)
        context = self._context(context_job, 'intent', active_task=not terminal)
        context['priorTask'] = {field: deepcopy(previous.get(field)) for field in
                              ('stage', 'kind', 'publishRequested', 'lastResult')}
        context['priorTask']['name'] = previous['data']['名稱']
        try:
            plan = self._validate_plan(self.analyze(context, []), context_job, 'intent')
        except Exception as error:
            self._log_analysis_error('closed_intent', error)
            return _response(False, '這則指示尚未判讀成功，既有上架資料與收據都已保留，請再說明一次。',
                             job=previous, status='analysis_failed')
        with _LOCK:
            current = self._load(key)
            if not current or current['jobId'] != previous['jobId']:
                return _response(False, '目前任務已變更，這則指示沒有套用；請確認目前上架狀態。', job=current)
            action = plan['action']
            if action == 'cancel':
                return self.cancel(key)
            if action == 'retry':
                return super().retry(key)
            if action == 'status':
                return super().status(key)
            if current['stage'] not in _TERMINAL:
                # Checking a pending publication only reads its result. New
                # prose or a request to wait must never replay publish().
                result = super().status(key)
                if action != 'ignore' and result.get('status') != 'published':
                    result['message'] += '\n這筆已送出發布，內容保持原樣；只有明確要求重試才會重送。'
                return result
            if action == 'ignore':
                return None
            if action not in {'collect', 'publish'}:
                return super().status(key)
            job = self._new_job()
            job['messages'] = [latest]
            job['inputRevision'] = 1
            if current.get('pendingIntake'):
                job['pendingIntake'] = deepcopy(current['pendingIntake'])
            self._apply_plan(key, job, plan, 'intent')
            if action == 'collect' and plan.get('captureAction') == 'open':
                self._open_capture(key, job, latest)
            self._inactive_until.pop(key, None)
            organize = (action == 'publish' or plan.get('needsImages') is True
                        or job['publishConsent'].get('complete'))
            if not organize:
                message = ('已開始這輪收件，可以直接傳文字和圖片。' if self._capture_active(job)
                           else '新上架資料已保存；需要繼續傳圖時請叫我開始收件。')
                return self._record(key, job, True, message, status='collecting')
        return self._analyze(key, phase='organize')

    def _save_image(self, key, job, entry):
        if entry.get('asset'):
            return
        entry.update(stage='downloading', error=None)
        self._save(key, job)
        try:
            raw = self.download(entry['messageId'])
            self._check_bytes(raw)
            entry['checksum'] = sha256(raw).hexdigest()
        except Exception:
            entry.update(stage='download_failed', error='download_failed')
            self._save(key, job)
            return
        entry['stage'] = 'uploading'
        self._save(key, job)
        try:
            extension = 'png' if raw.startswith(b'\x89PNG') else 'webp' if raw.startswith(b'RIFF') else 'jpg'
            asset = self.backend.upload_media(raw, f"{job['jobId']}-{entry['index']}.{extension}",
                                              entry['operationKey'], kind=entry['mediaKind'])
            if not isinstance(asset, dict) or not _https_url(asset.get('url')):
                raise ValueError('invalid media asset')
            entry['asset'] = {field: asset[field] for field in ('url', 'path', 'mimeType', 'width', 'height') if field in asset}
            entry.update(stage='uploaded', error=None)
        except Exception:
            entry.update(stage='upload_failed', error='upload_failed')
        self._save(key, job)

    def _add_image_set(self, job, entry, image_set):
        if not image_set:
            return
        values = self._image_set_values(image_set)
        if not values:
            job['imageSetIssues'].append('LINE 相簿張數資訊不完整，請確認這批圖片是否已全部傳完。')
            return
        ident, index, total = values
        entry['imageSet'] = {'id': ident, 'index': index, 'total': total}
        group = job['imageSets'].setdefault(ident, {'total': total, 'indices': [],
                                                  'captureSessionId': entry['captureSessionId']})
        if group['total'] != total or index in group['indices']:
            job['imageSetIssues'].append('LINE 相簿張數有衝突，圖片已保留，請確認實際張數。')
        else:
            group['indices'].append(index)

    def _waiting_sets(self, job):
        capture = self._capture(job)
        if not capture or (not self._capture_active(job) and capture.get('closedReason') != 'complete'):
            return 0
        return sum(group['total'] - len(group['indices']) for group in job.get('imageSets', {}).values()
                   if group.get('captureSessionId') == capture['id'])

    def receive_image(self, key, message_id, event_timestamp=None, image_set=None, *, _from_pending=False):
        key = tuple(key)
        with _LOCK:
            try:
                job = self._load(key)
            except WorkflowStorageError:
                return _response(False, '', status='ignored')
            if job and not _from_pending:
                metadata = ({field: image_set.get(field) for field in ('id', 'index', 'total')}
                            if isinstance(image_set, dict) else
                            {field: getattr(image_set, field, None) for field in ('id', 'index', 'total')}
                            if image_set is not None else None)
                queued = self._queue_intake(key, job, {'type': 'image', 'messageId': message_id,
                                                      'timestamp': _seconds(event_timestamp), 'imageSet': metadata})
                if queued:
                    return queued
            if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                return _response(False, '目前沒有正在收件的上架任務。', job=job, status='ignored')
            self._expire_capture(key, job)
            active = self._capture_active(job)
            late = self._late_image_allowed(job, event_timestamp, image_set)
            if not active and not late:
                return _response(False, '這張圖片不屬於目前明確開啟的收件範圍。', job=job, status='ignored')
            start = job['captureSession'].get('startedAt')
            timestamp = _seconds(event_timestamp)
            if active and start is not None and timestamp is not None and timestamp < start:
                return _response(False, '這張圖片早於本輪收件開始。', job=job, status='ignored')
            job = self._migrate(key, job)
            if not isinstance(message_id, str) or not message_id:
                return _response(False, '圖片訊息無法識別，請再傳一次。', job=job, status='media_failed')
            if message_id in job['processedEvents']:
                return _response(True, '這張圖片已收到，沒有重複新增。', job=job, status='duplicate')
            entry = {'index': len(job['images']) + 1, 'messageId': message_id,
                     'timestamp': _seconds(event_timestamp), 'purpose': 'unknown',
                     'roleName': None, 'confirmed': False, 'recognition': None,
                     'stage': 'received', 'asset': None, 'error': None,
                     'mediaKind': 'character',
                     'captureSessionId': job['captureSession']['id'],
                     'operationKey': f"{job['jobId']}:media:{message_id}"}
            self._add_image_set(job, entry, image_set)
            job['images'].append(entry)
            job['processedEvents'].append(message_id)
            job['inputRevision'] += 1
            job['captureSession']['lastActivityAt'] = self.clock()
            self._save(key, job)
            self._save_image(key, job, entry)
            resume = bool(late and (job.get('analysisPending') or job.get('publishConsent', {}).get('complete')))
            waiting = self._waiting_sets(job)
            if not entry.get('asset'):
                self._close_capture(job, 'media_failed')
                job['analysisPending'] = False
                return self._record(key, job, False, f"第 {entry['index']} 張圖片尚未保存成功；訊息編號已保留，可以請我重試。", status='media_failed')
            if not resume or waiting:
                suffix = f'，這批還在接收 {waiting} 張' if waiting else ''
                return self._record(key, job, True, f"已保存 {len(job['images'])} 張圖片{suffix}。", status='collecting')
        return self._analyze(key, phase='organize')

    def _context(self, job, phase, active_task=True):
        return {'phase': phase, 'activeTask': active_task, 'messages': deepcopy(job['messages']),
                'explicitlyInvoked': bool(job['messages'] and job['messages'][-1].get('addressed')),
                'captureSession': deepcopy(self._capture(job)),
                'data': deepcopy(job['data']), 'nameKnown': job.get('nameKnown', False),
                'kind': job['kind'], 'authorization': deepcopy(job.get('publishConsent', {})),
                'questions': deepcopy(job.get('questions', [])),
                'images': [{**{field: deepcopy(entry.get(field)) for field in
                              ('index', 'messageId', 'timestamp', 'checksum', 'purpose', 'roleName', 'manuallyConfirmed', 'manualEvidenceMessageId', 'declaredPurpose', 'imageSet')},
                            'stored': bool(entry.get('asset'))} for entry in job['images']]}

    def _validate_plan(self, result, job, phase):
        if not isinstance(result, dict) or result.get('action') not in {'collect', 'publish', 'organize', 'status', 'retry', 'cancel', 'ignore'}:
            raise ValueError('invalid action')
        kind = result.get('kind', job['kind'])
        if kind not in {'upload', 'cover', 'portraits'}:
            raise ValueError('invalid kind')
        if result.get('captureAction', 'none') not in {'open', 'none'}:
            raise ValueError('invalid capture action')
        patch = result.get('data') or {}
        patch = normalize_listing_data(patch, require_name=False) if patch else {}
        questions = result.get('questions') or []
        if not isinstance(questions, list) or len(questions) > 30 or any(not isinstance(q, str) or not q.strip() or len(q) > 1000 for q in questions):
            raise ValueError('invalid questions')
        reply = result.get('reply') or ''
        if not isinstance(reply, str) or len(reply) > 3000:
            raise ValueError('invalid reply')
        consent = deepcopy(job.get('publishConsent', {}))
        given = result.get('authorization') or {}
        if not isinstance(given, dict):
            raise ValueError('invalid consent')
        message_ids = {m['messageId'] for m in job['messages'] if m.get('role') == 'user' and m.get('text', '').strip()}
        for flag, evidence_field in (('publish', 'publishEvidenceMessageId'), ('complete', 'completionEvidenceMessageId')):
            if flag in given and type(given[flag]) is not bool:
                raise ValueError('invalid consent flag')
            if flag in given:
                evidence = given.get(evidence_field) or given.get('evidenceMessageId')
                if evidence not in message_ids:
                    raise ValueError('consent must cite a real user text message')
                consent[flag] = given[flag]
                consent[evidence_field] = evidence
        assignments = result.get('assignments') or []
        if phase == 'organize':
            if not isinstance(assignments, list):
                raise ValueError('invalid assignments')
            expected = {entry['index'] for entry in job['images']}
            seen = set()
            for assignment in assignments:
                if not isinstance(assignment, dict):
                    raise ValueError('invalid assignment')
                index, purpose = assignment.get('index'), assignment.get('purpose')
                if type(index) is not int or index not in expected or index in seen or purpose not in {'cover', 'portraits', 'ignore', 'unknown'}:
                    raise ValueError('invalid image index or purpose')
                role = assignment.get('roleName')
                if role is not None and (not isinstance(role, str) or len(role) > 200):
                    raise ValueError('invalid role')
                evidence = assignment.get('correctionEvidenceMessageId')
                if evidence is not None and evidence not in message_ids:
                    raise ValueError('image correction must cite real user text')
                seen.add(index)
            required = {entry['index'] for entry in job['images'] if not entry.get('manuallyConfirmed')}
            if not required.issubset(seen):
                raise ValueError('an image was omitted')
        return {**result, 'kind': kind, 'data': patch, 'questions': questions,
                'reply': reply, 'consent': consent, 'assignments': assignments}

    def _apply_plan(self, key, job, plan, phase):
        patch = plan['data']
        if job.get('baseline') and (plan['kind'] != job['kind'] or any(job['data'].get(field) != value for field, value in patch.items())):
            raise ValueError('cannot change published baseline during an image update')
        job['data'].update(patch)
        job['nameKnown'] = job.get('nameKnown') or bool(patch.get('名稱'))
        job['kind'] = plan['kind']
        job['publishConsent'] = plan['consent']
        job['questions'] = plan['questions']
        if phase == 'intent' and plan['action'] in {'collect', 'publish', 'organize', 'retry'} and self._capture_active(job):
            job['captureSession']['lastActivityAt'] = self.clock()
        if phase == 'organize':
            assignments = {a['index']: a for a in plan['assignments']}
            message_order = {message['messageId']: index for index, message in enumerate(job['messages'])}
            for entry in job['images']:
                assignment = assignments.get(entry['index'])
                if assignment is None:
                    continue
                evidence = assignment.get('correctionEvidenceMessageId')
                previous = entry.get('manualEvidenceMessageId')
                if entry.get('manuallyConfirmed') and (not evidence or
                        (previous in message_order and message_order[evidence] <= message_order[previous])):
                    continue
                purpose = assignment['purpose']
                entry.update(purpose=purpose, roleName=assignment.get('roleName'),
                             confirmed=purpose != 'unknown', stage='ready', error=None)
                if evidence:
                    entry.update(manuallyConfirmed=True, manualEvidenceMessageId=evidence)
            job['analyzedRevision'] = job['inputRevision']
        self._save(key, job)

    def _ensure_identity(self, key, job):
        if not job.get('nameKnown'):
            return None
        identity = [job['kind'], job['data']['名稱']]
        if job.get('resolvedIdentity') == identity:
            return None
        # Existing-image tasks borrow canonical published text. A model may
        # extract only the visible subset of role names from newly sent art;
        # that is not a request to replace the published roster.
        job['requestedData'] = (deepcopy(job['data']) if job['kind'] == 'upload'
                                else {'名稱': job['data']['名稱']})
        job['stage'] = 'initializing'
        self._save(key, job)
        result = super()._initialize(key, job)
        if not result.get('ok'):
            return result
        job['resolvedIdentity'] = identity
        self._save(key, job)
        return None

    def _issues(self, job):
        clean = deepcopy(job)
        clean['batches'] = []
        clean['images'] = [entry for entry in job['images'] if entry['purpose'] in {'cover', 'portraits'}]
        issues = super()._issues(clean)
        if not job.get('nameKnown'):
            issues.insert(0, '還需要劇本名稱。')
        for entry in job['images']:
            if not entry.get('asset'):
                issues.append(f"第 {entry['index']} 張尚未保存成功，可以請我重試。")
            elif entry['purpose'] == 'unknown':
                issues.append(f"還無法確認第 {entry['index']} 張的用途，請說明這張圖片。")
        issues.extend(job.get('questions', []))
        issues.extend(job.get('imageSetIssues', []))
        waiting = self._waiting_sets(job)
        if waiting:
            issues.append(f'LINE 這批圖片還有 {waiting} 張正在接收。')
        return list(dict.fromkeys(issues))

    def _complete(self, key, job):
        consent = job.get('publishConsent', {})
        job['analysisPending'] = False
        blocked = self._ensure_identity(key, job)
        if blocked:
            return blocked
        issues = self._issues(job)
        if issues:
            self._close_capture(job, 'needs_input')
            return self._record(key, job, False, '資料與圖片已保留。\n' + '\n'.join(issues)
                                + '\n補答或繼續傳圖時，請先叫我接續這本。', status='needs_input')
        if not (consent.get('publish') and consent.get('complete')):
            job['analysisPending'] = False
            self._close_capture(job, 'ready')
            return self._record(key, job, True, '資料和圖片已整理好；確定全部傳完並要上架時告訴我即可。', status='ready')
        job['analysisPending'] = False
        self._close_capture(job, 'publishing')
        job['publishRequested'] = True
        job['stage'] = 'publishing'
        self._save(key, job)
        return self._publish(key, job)

    def _analyze(self, key, phase='organize', new_candidate=False, source_message_id=None):
        key = tuple(key)
        with _LOCK:
            if key in self._running:
                job = self._load(key)
                if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                    return super().status(key)
                job['analysisPending'] = True
                return self._record(key, job, True, '資料已保存，會一起整理最新收到的內容。', status='collecting')
            self._running.add(key)
        try:
            # A concurrent image/text receipt invalidates the snapshot. Process
            # the latest durable snapshot instead of committing stale mappings.
            for attempt in range(4):
                with _LOCK:
                    job = self._load(key)
                    if not job or job['stage'] in _TERMINAL:
                        return deepcopy(job['lastResult']) if job else _response(False, '目前沒有上架任務。')
                    if job.get('publishRequested'):
                        return super().status(key)
                    job = self._migrate(key, job)
                    if phase == 'organize':
                        self._seal_for_analysis(job)
                        self._save(key, job)
                        if self._pending_actionable_events(job):
                            job['analysisPending'] = True
                            self._save(key, job)
                            return _response(True, '', job=job, status='deferred')
                    if phase == 'organize' and job['kind'] in {'cover', 'portraits'}:
                        blocked = self._ensure_identity(key, job)
                        if blocked:
                            return blocked
                    if phase == 'organize' and self._waiting_sets(job):
                        job['analysisPending'] = True
                        return self._record(key, job, True, '已收到完成指示；LINE 圖片尚在接收，收齊後會接著整理。', status='collecting')
                    snapshot = deepcopy(job)
                try:
                    images = []
                    if phase == 'organize':
                        for entry in snapshot['images']:
                            if not entry.get('asset'):
                                raise ValueError('image storage incomplete')
                            raw = self.backend.read_media(deepcopy(entry['asset']))
                            self._check_bytes(raw)
                            entry['checksum'] = sha256(raw).hexdigest()
                            images.append({'index': entry['index'], 'bytes': raw, 'asset': deepcopy(entry['asset'])})
                    result = self.analyze(self._context(snapshot, phase, active_task=not new_candidate or phase == 'organize'), images)
                    plan = self._validate_plan(result, snapshot, phase)
                except Exception as error:
                    self._log_analysis_error(phase, error)
                    with _LOCK:
                        job = self._load(key)
                        if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                            return super().status(key)
                        job['analysisPending'] = False
                        self._close_capture(job, 'analysis_failed')
                        self._known_active.discard(key)
                        return self._record(key, job, False, '這次整理尚未成功，原文與圖片都已保留。請叫我重試即可，不用重新傳圖。', status='analysis_failed')
                with _LOCK:
                    job = self._load(key)
                    if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                        return deepcopy(job['lastResult']) if job else _response(False, '目前沒有上架任務。')
                    if job['jobId'] != snapshot['jobId']:
                        return _response(True, '目前任務已更換，原分析結果沒有套用。', job=job)
                    if job['inputRevision'] != snapshot['inputRevision']:
                        phase = 'intent' if phase == 'intent' else 'organize'
                        continue
                    if phase == 'organize' and self._pending_actionable_events(job):
                        job['analysisPending'] = True
                        self._save(key, job)
                        return _response(True, '', job=job, status='deferred')
                    for entry, analyzed in zip(job['images'], snapshot['images']):
                        if analyzed.get('checksum'):
                            entry['checksum'] = analyzed['checksum']
                    if plan['action'] == 'ignore':
                        ignored_id = snapshot['messages'][-1]['messageId'] if phase == 'intent' and snapshot['messages'] else source_message_id
                        job['messages'] = [message for message in job['messages'] if message['messageId'] != ignored_id]
                        job['inputRevision'] += 1
                        if new_candidate:
                            job['stage'] = 'cancelled'
                            self._close_capture(job, 'ignored')
                            self._known_active.discard(key)
                            if self.return_chat_image and job['images']:
                                self.return_chat_image(key, job['images'][-1]['messageId'])
                        self._save(key, job)
                        return None
                    if plan['action'] == 'cancel':
                        return self.cancel(key)
                    try:
                        self._apply_plan(key, job, plan, phase)
                    except ValueError:
                        self._close_capture(job, 'needs_input')
                        job['analysisPending'] = False
                        return self._record(key, job, False, '整理結果與目前劇本資料有衝突，原資料和圖片已保留，請說明要修改的內容。', status='needs_input')
                    if phase == 'intent':
                        latest = snapshot['messages'][-1] if snapshot['messages'] else {}
                        if (plan['action'] == 'collect' and plan.get('captureAction') == 'open'
                                and latest.get('addressed')):
                            self._open_capture(key, job, latest)
                            self._save(key, job)
                        if plan['action'] == 'status':
                            return self.status(key)
                        if plan['action'] == 'retry':
                            for entry in job['images']:
                                entry.setdefault('mediaKind', 'cover' if entry['purpose'] == 'cover' else 'character')
                                self._save_image(key, job, entry)
                        if (plan['action'] in {'publish', 'organize', 'retry'} or plan.get('needsImages') is True
                                or job['publishConsent'].get('complete') or job.get('analysisPending')):
                            if job['kind'] in {'cover', 'portraits'}:
                                blocked = self._ensure_identity(key, job)
                                if blocked:
                                    return blocked
                            phase = 'organize'
                            job['analysisPending'] = True
                            self._save(key, job)
                            continue
                        message = ('資料已保存，這輪可以繼續傳文字和圖片。' if self._capture_active(job)
                                   else '資料已保存；需要繼續傳圖時請叫我開始收件。')
                        if job.get('interruptedIntake', {}).get('events'):
                            message += '\n上一輪還有未處理完成的收件記錄，已保留；這些內容尚未加入上架資料。'
                        return self._record(key, job, True, message, status='collecting')
                    return self._complete(key, job)
            with _LOCK:
                job = self._load(key)
                if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                    return super().status(key)
                job['analysisPending'] = False
                self._close_capture(job, 'analysis_interrupted')
                return self._record(key, job, True, '新資料仍在加入，內容都已保存；傳完後再告訴我一次即可。', status='collecting')
        finally:
            with _LOCK:
                self._running.discard(key)

    def finish(self, key):
        # No invented user message or consent: API callers must first preserve
        # the actual user instruction through handle_text.
        return self._analyze(key, phase='organize')

    def retry(self, key):
        with _LOCK:
            job = self._load(key)
            if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                return super().retry(key)
            job = self._migrate(key, job)
            self._close_capture(job, 'retry')
            job['analysisPending'] = False
            self._save(key, job)
            for entry in job['images']:
                entry.setdefault('mediaKind', 'cover' if entry['purpose'] == 'cover' else 'character')
                self._save_image(key, job, entry)
        return self._analyze(key, phase='organize')

    def cancel(self, key):
        with _LOCK:
            job = self._load(key)
            if job and job['stage'] not in _TERMINAL:
                self._close_capture(job, 'cancelled')
                job['analysisPending'] = False
                self._known_active.discard(tuple(key))
                self._save(key, job)
            return super().cancel(key)

    def status(self, key):
        with _LOCK:
            job = self._load(key)
            if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                return super().status(key)
            job = self._migrate(key, job)
            saved = sum(bool(entry.get('asset')) for entry in job['images'])
            name = f"《{job['data']['名稱']}》" if job.get('nameKnown') else '這本劇本'
            lines = [f'{name}尚未上架，已保存 {saved}/{len(job["images"])} 張圖片。']
            lines.extend(job.get('questions', []))
            waiting = self._waiting_sets(job)
            if waiting:
                lines.append(f'LINE 這批還有 {waiting} 張正在接收。')
            return _response(True, '\n'.join(lines), job=job, images=[
                {field: deepcopy(entry.get(field)) for field in ('index', 'purpose', 'roleName', 'confirmed', 'stage')}
                for entry in job['images']])
