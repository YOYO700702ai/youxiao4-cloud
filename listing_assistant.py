"""Conversation-first collection backed by the existing durable publisher.

The model interprets real user messages and, when requested, all saved images.
It never publishes directly. Publication uses ListingWorkflow's existing
version checks, operation keys, and verified public readback.
"""
from copy import deepcopy
from hashlib import sha256
import time
from uuid import uuid4

from listing_workflow import (
    ListingWorkflow, WorkflowStorageError, MAX_IMAGE_BYTES, _LOCK, _TERMINAL,
    _https_url, _response, _seconds, normalize_listing_data,
)


class AssistantListingWorkflow(ListingWorkflow):
    def __init__(self, *, analyze, identify=None, clock=time.time,
                 max_image_bytes=MAX_IMAGE_BYTES, return_chat_image=None, **kwargs):
        super().__init__(identify=identify, clock=clock,
                         max_image_bytes=max_image_bytes, **kwargs)
        self.analyze = analyze
        self.return_chat_image = return_chat_image
        self._running = set()
        self._known_active = set()
        self._inactive_until = {}

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
            self._known_active.add(tuple(key))
            self._inactive_until.pop(tuple(key), None)
            return self._record(key, job, True, '可以直接貼上劇本資料和全部圖片；傳完後告訴我即可。')

    def handle_text(self, key, text, message_id=None, event_timestamp=None, eligible=False):
        if not isinstance(text, str) or not text.strip():
            return None
        key = tuple(key)
        with _LOCK:
            if not eligible and self._inactive_until.get(key, 0) > self.clock():
                return None
            try:
                job = self._load(key)
            except WorkflowStorageError:
                if not eligible and key not in self._known_active:
                    return None
                raise
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
                pass
            elif not job:
                if not eligible:
                    self._inactive_until[key] = self.clock() + 30
                    return None
                job = self._new_job()
            else:
                job = self._migrate(key, job)
            if closed_job is None:
                self._known_active.add(key)
                self._inactive_until.pop(key, None)
                job['messages'].append({'role': 'user', 'messageId': message_id or str(uuid4()),
                                        'text': text, 'timestamp': _seconds(event_timestamp)})
                job['inputRevision'] += 1
                self._save(key, job)
        if closed_job is not None:
            return self._route_closed_text(key, closed_job, text, message_id, event_timestamp)
        return self._analyze(key, phase='intent', new_candidate=created)

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
                  'text': text, 'timestamp': _seconds(event_timestamp)}
        context_job['messages'].append(latest)
        context = self._context(context_job, 'intent', active_task=not terminal)
        context['priorTask'] = {field: deepcopy(previous.get(field)) for field in
                              ('stage', 'kind', 'publishRequested', 'lastResult')}
        context['priorTask']['name'] = previous['data']['名稱']
        try:
            plan = self._validate_plan(self.analyze(context, []), context_job, 'intent')
        except Exception:
            return _response(False, '這則指示尚未判讀成功，既有上架資料與收據都已保留，請再說明一次。',
                             job=previous, status='analysis_failed')
        with _LOCK:
            current = self._load(key)
            if not current or current['jobId'] != previous['jobId']:
                return _response(False, '目前任務已變更，這則指示沒有套用；請確認目前上架狀態。', job=current)
            action = plan['action']
            if action == 'cancel':
                return super().cancel(key)
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
            self._apply_plan(key, job, plan, 'intent')
            self._known_active.add(key)
            self._inactive_until.pop(key, None)
            organize = (action == 'publish' or plan.get('needsImages') is True
                        or job['publishConsent'].get('complete'))
            if not organize:
                return self._record(key, job, True, '新上架資料已保存，可以直接傳文字和圖片。', status='collecting')
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
        if not isinstance(image_set, dict):
            image_set = {field: getattr(image_set, field, None) for field in ('id', 'index', 'total')}
        ident, index, total = (image_set.get(field) for field in ('id', 'index', 'total'))
        if not isinstance(ident, str) or not ident or type(index) is not int or type(total) is not int or not 1 <= index <= total <= 100:
            job['imageSetIssues'].append('LINE 相簿張數資訊不完整，請確認這批圖片是否已全部傳完。')
            return
        entry['imageSet'] = {'id': ident, 'index': index, 'total': total}
        group = job['imageSets'].setdefault(ident, {'total': total, 'indices': []})
        if group['total'] != total or index in group['indices']:
            job['imageSetIssues'].append('LINE 相簿張數有衝突，圖片已保留，請確認實際張數。')
        else:
            group['indices'].append(index)

    def _waiting_sets(self, job):
        return sum(group['total'] - len(group['indices']) for group in job.get('imageSets', {}).values())

    def receive_image(self, key, message_id, event_timestamp=None, image_set=None):
        key = tuple(key)
        with _LOCK:
            job = self._load(key)
            if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                return _response(False, '目前沒有正在收件的上架任務。', job=job, status='ignored')
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
                     'operationKey': f"{job['jobId']}:media:{message_id}"}
            self._add_image_set(job, entry, image_set)
            job['images'].append(entry)
            job['processedEvents'].append(message_id)
            job['inputRevision'] += 1
            self._save(key, job)
            self._save_image(key, job, entry)
            resume = bool(job.get('analysisPending') or job.get('publishConsent', {}).get('complete'))
            waiting = self._waiting_sets(job)
            if not entry.get('asset'):
                return self._record(key, job, False, f"第 {entry['index']} 張圖片尚未保存成功；訊息編號已保留，可以請我重試。", status='media_failed')
            if not resume or waiting:
                suffix = f'，這批還在接收 {waiting} 張' if waiting else ''
                return self._record(key, job, True, f"已保存 {len(job['images'])} 張圖片{suffix}。", status='collecting')
        return self._analyze(key, phase='organize')

    def _context(self, job, phase, active_task=True):
        return {'phase': phase, 'activeTask': active_task, 'messages': deepcopy(job['messages']),
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
        job['analysisPending'] = bool(consent.get('complete'))
        blocked = self._ensure_identity(key, job)
        if blocked:
            return blocked
        issues = self._issues(job)
        if issues:
            return self._record(key, job, False, '資料與圖片已保留。\n' + '\n'.join(issues), status='needs_input')
        if not (consent.get('publish') and consent.get('complete')):
            job['analysisPending'] = False
            return self._record(key, job, True, '資料和圖片已整理好；確定全部傳完並要上架時告訴我即可。', status='ready')
        job['analysisPending'] = False
        job['publishRequested'] = True
        job['stage'] = 'publishing'
        self._save(key, job)
        return self._publish(key, job)

    def _analyze(self, key, phase='organize', new_candidate=False):
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
                except Exception:
                    with _LOCK:
                        job = self._load(key)
                        if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                            return super().status(key)
                        job['analysisPending'] = True
                        return self._record(key, job, False, '這次整理尚未成功，原文與圖片都已保留。請告訴我重試即可，不用重新傳圖。', status='analysis_failed')
                with _LOCK:
                    job = self._load(key)
                    if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                        return deepcopy(job['lastResult']) if job else _response(False, '目前沒有上架任務。')
                    if job['jobId'] != snapshot['jobId']:
                        return _response(True, '目前任務已更換，原分析結果沒有套用。', job=job)
                    if job['inputRevision'] != snapshot['inputRevision']:
                        phase = 'intent' if phase == 'intent' else 'organize'
                        continue
                    for entry, analyzed in zip(job['images'], snapshot['images']):
                        if analyzed.get('checksum'):
                            entry['checksum'] = analyzed['checksum']
                    if plan['action'] == 'ignore':
                        if new_candidate:
                            job['stage'] = 'cancelled'
                            self._save(key, job)
                            self._known_active.discard(key)
                            if self.return_chat_image and job['images']:
                                self.return_chat_image(key, job['images'][-1]['messageId'])
                        return None
                    if plan['action'] == 'cancel':
                        return super().cancel(key)
                    try:
                        self._apply_plan(key, job, plan, phase)
                    except ValueError:
                        return self._record(key, job, False, '整理結果與目前劇本資料有衝突，原資料和圖片已保留，請說明要修改的內容。', status='needs_input')
                    if phase == 'intent':
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
                        return self._record(key, job, True, '資料已保存，可以繼續傳文字和圖片。', status='collecting')
                    return self._complete(key, job)
            with _LOCK:
                job = self._load(key)
                if not job or job['stage'] in _TERMINAL or job.get('publishRequested'):
                    return super().status(key)
                job['analysisPending'] = True
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
            for entry in job['images']:
                entry.setdefault('mediaKind', 'cover' if entry['purpose'] == 'cover' else 'character')
                self._save_image(key, job, entry)
        return self._analyze(key, phase='organize')

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
