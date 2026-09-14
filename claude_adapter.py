"""Small, manually dispatched Anthropic Messages bridge for the existing bot.

No credentials are loaded here. Chat history is committed only after a complete
response; raw assistant blocks retain Anthropic signatures during reconstruction.
"""
import base64
from copy import deepcopy
from dataclasses import dataclass
import json
import threading

import requests
from google.genai import types


MESSAGES_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-sonnet-5'


class ClaudeAPIError(RuntimeError):
    """Deliberately excludes request details, response bodies and credentials."""

    def __init__(self, error_type, status_code=None):
        self.error_type = error_type
        self.status_code = status_code
        suffix = f' (HTTP {status_code})' if status_code is not None else ''
        super().__init__(f'Claude API: {error_type}{suffix}')


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return deepcopy(obj)
    if hasattr(obj, 'model_dump'):
        return obj.model_dump(mode='json', exclude_none=True)
    raise ClaudeAPIError('unsupported_configuration')


def schema_to_json_schema(schema):
    """Translate Google Schema's supported subset, excluding provider fields."""
    source = _dict(schema)
    result = {}
    aliases = {'any_of': 'anyOf', 'min_items': 'minItems', 'max_items': 'maxItems',
               'min_length': 'minLength', 'max_length': 'maxLength',
               'min_properties': 'minProperties', 'max_properties': 'maxProperties',
               'additional_properties': 'additionalProperties'}
    scalar = {'title', 'description', 'format', 'default', 'enum', 'required',
              'minimum', 'maximum', 'minItems', 'maxItems', 'minLength',
              'maxLength', 'minProperties', 'maxProperties', 'pattern', '$ref'}
    for key, value in source.items():
        if value is None:
            continue
        key = aliases.get(key, key)
        if key == 'type':
            value = getattr(value, 'value', value)
            if isinstance(value, str) and value.lower() in (
                    'object', 'array', 'string', 'integer', 'number', 'boolean', 'null'):
                result[key] = value.lower()
        elif key in ('properties', '$defs', 'definitions'):
            result[key] = {name: schema_to_json_schema(child) for name, child in value.items()}
        elif key in ('items', 'additionalProperties'):
            result[key] = value if isinstance(value, bool) else schema_to_json_schema(value)
        elif key in ('anyOf', 'oneOf', 'allOf'):
            result[key] = [schema_to_json_schema(child) for child in value]
        elif key in scalar:
            result[key] = deepcopy(value)
    if source.get('nullable'):
        result = {'anyOf': [result, {'type': 'null'}]}
    return result


def _tools(config):
    result = []
    for tool in _value(config, 'tools', []) or []:
        declarations = _value(tool, 'function_declarations', None)
        if declarations is None:
            raise ClaudeAPIError('unsupported_tool')
        for declaration in declarations:
            name = _value(declaration, 'name')
            if not isinstance(name, str) or not name:
                raise ClaudeAPIError('invalid_tool_schema')
            parameters = _value(declaration, 'parameters_json_schema')
            if parameters is None:
                parameters = _value(declaration, 'parameters')
            schema = schema_to_json_schema(parameters) if parameters is not None else {
                'type': 'object', 'properties': {}}
            if schema.get('type') != 'object':
                raise ClaudeAPIError('invalid_tool_schema')
            item = {'name': name, 'input_schema': schema}
            description = _value(declaration, 'description')
            if description:
                item['description'] = description
            result.append(item)
    return result


def _image(blob):
    data = _value(blob, 'data')
    if isinstance(data, str):
        try:
            data = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            raise ClaudeAPIError('invalid_image') from None
    if not isinstance(data, (bytes, bytearray)):
        raise ClaudeAPIError('invalid_image')
    if data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data[:6] in (b'GIF87a', b'GIF89a'):
        mime = 'image/gif'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ClaudeAPIError('unsupported_image_format')
    return {'type': 'image', 'source': {'type': 'base64', 'media_type': mime,
            'data': base64.b64encode(data).decode('ascii')}}


def _parts(parts):
    blocks = []
    for part in parts:
        if isinstance(part, str):
            blocks.append({'type': 'text', 'text': part})
        elif _value(part, 'text') is not None:
            blocks.append({'type': 'text', 'text': _value(part, 'text')})
        elif _value(part, 'inline_data') is not None:
            blocks.append(_image(_value(part, 'inline_data')))
        elif _value(part, 'function_response') is not None:
            response = _value(part, 'function_response')
            identifier = _value(response, 'id')
            if not isinstance(identifier, str) or not identifier:
                raise ClaudeAPIError('missing_tool_use_id')
            data = _value(response, 'response')
            if not isinstance(data, dict):
                raise ClaudeAPIError('invalid_tool_result')
            try:
                content = json.dumps(data, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError):
                raise ClaudeAPIError('invalid_tool_result') from None
            block = {'type': 'tool_result', 'tool_use_id': identifier, 'content': content}
            if data.get('error') or data.get('ok') is False or (
                    isinstance(data.get('result'), dict) and data['result'].get('ok') is False):
                block['is_error'] = True
            blocks.append(block)
        elif _value(part, 'function_call') is not None:
            call = _value(part, 'function_call')
            identifier = _value(call, 'id')
            if not isinstance(identifier, str) or not identifier:
                raise ClaudeAPIError('missing_tool_use_id')
            blocks.append({'type': 'tool_use', 'id': identifier,
                           'name': _value(call, 'name'), 'input': _value(call, 'args') or {}})
        else:
            raise ClaudeAPIError('unsupported_content')
    return blocks


@dataclass
class ClaudeHistory:
    role: str
    blocks: list


def _messages(contents):
    if isinstance(contents, ClaudeHistory):
        return [{'role': 'assistant' if contents.role == 'model' else contents.role,
                 'content': deepcopy(contents.blocks)}]
    if _value(contents, 'role') is not None:
        role = _value(contents, 'role')
        if role == 'model':
            role = 'assistant'
        if role not in ('assistant', 'user'):
            raise ClaudeAPIError('unsupported_role')
        raw = _value(contents, 'content')
        if raw is not None:
            return [{'role': role, 'content': deepcopy(raw)}]
        return [{'role': role, 'content': _parts(_value(contents, 'parts', []) or [])}]
    if isinstance(contents, (list, tuple)):
        if any(isinstance(item, ClaudeHistory) or _value(item, 'role') is not None for item in contents):
            return [message for item in contents for message in _messages(item)]
        return [{'role': 'user', 'content': _parts(contents)}]
    return [{'role': 'user', 'content': _parts([contents])}]


def _validate_history(messages):
    pending = set()
    for message in messages:
        blocks = message['content']
        if isinstance(blocks, str):
            blocks = [{'type': 'text', 'text': blocks}]
        if not isinstance(blocks, list) or not blocks:
            raise ClaudeAPIError('invalid_history')
        if message['role'] == 'assistant':
            if pending:
                raise ClaudeAPIError('missing_tool_results')
            for block in blocks:
                if block.get('type') == 'tool_use':
                    identifier = block.get('id')
                    if not identifier or identifier in pending:
                        raise ClaudeAPIError('invalid_tool_use_id')
                    pending.add(identifier)
        else:
            results = [block.get('tool_use_id') for block in blocks if block.get('type') == 'tool_result']
            if len(set(results)) != len(results) or set(results) != pending:
                raise ClaudeAPIError('unmatched_tool_results')
            seen_other = False
            for block in blocks:
                if block.get('type') == 'tool_result' and seen_other:
                    raise ClaudeAPIError('tool_result_order')
                if block.get('type') != 'tool_result':
                    seen_other = True
            pending.clear()
    if pending:
        raise ClaudeAPIError('missing_tool_results')


def _system(config):
    instruction = _value(config, 'system_instruction')
    if isinstance(instruction, str):
        text = instruction
    elif instruction is None:
        text = ''
    else:
        parts = _value(instruction, 'parts', instruction if isinstance(instruction, list) else [])
        text = '\n'.join(part if isinstance(part, str) else (_value(part, 'text') or '') for part in parts)
    if _value(config, 'response_mime_type') == 'application/json':
        text += '\nReturn only valid JSON. Do not add Markdown fences or commentary.'
    return text.strip()


def _response(payload):
    if not isinstance(payload, dict) or payload.get('role') != 'assistant' or not isinstance(payload.get('content'), list):
        raise ClaudeAPIError('invalid_response')
    parts, identifiers = [], set()
    for block in payload['content']:
        if not isinstance(block, dict):
            raise ClaudeAPIError('invalid_response')
        if block.get('type') == 'text':
            if not isinstance(block.get('text'), str):
                raise ClaudeAPIError('invalid_response')
            parts.append(types.Part(text=block['text']))
        elif block.get('type') == 'tool_use':
            identifier = block.get('id')
            if (not isinstance(identifier, str) or not identifier or identifier in identifiers
                    or not isinstance(block.get('name'), str) or not block['name']
                    or not isinstance(block.get('input'), dict)):
                raise ClaudeAPIError('invalid_tool_call')
            if payload.get('stop_reason') == 'max_tokens':
                raise ClaudeAPIError('truncated_tool_call')
            identifiers.add(identifier)
            parts.append(types.Part(function_call=types.FunctionCall(
                id=identifier, name=block['name'], args=block['input'])))
    usage = payload.get('usage') or {}
    def count(name):
        value = usage.get(name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    prompt = count('input_tokens') + count('cache_creation_input_tokens') + count('cache_read_input_tokens')
    output = count('output_tokens')
    finish = {'max_tokens': 'MAX_TOKENS', 'refusal': 'SAFETY'}.get(payload.get('stop_reason'), 'STOP')
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role='model', parts=parts), finish_reason=finish)],
        model_version=payload.get('model'), response_id=payload.get('id'),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=output,
            cached_content_token_count=count('cache_read_input_tokens'), total_token_count=prompt + output))


class ClaudeChat:
    def __init__(self, client, model, config=None, history=None):
        self._client, self._model, self._config = client, model, _dict(config)
        self._history = _messages(history) if history else []
        self._lock = threading.RLock()

    def get_history(self, curated=False):
        with self._lock:
            return [ClaudeHistory('model' if m['role'] == 'assistant' else m['role'],
                                  deepcopy(m['content'])) for m in self._history]

    def send_message(self, message, config=None):
        with self._lock:
            settings = {**self._config, **_dict(config)}
            proposed = deepcopy(self._history) + _messages(message)
            response, assistant = self._client._generate(self._model, proposed, settings)
            self._history = proposed + [assistant]
            return response


class _Chats:
    def __init__(self, client):
        self._client = client

    def create(self, *, model=None, config=None, history=None):
        return ClaudeChat(self._client, model or self._client.model, config, history)


class _Models:
    def __init__(self, client):
        self._client = client

    def generate_content(self, *, model=None, contents, config=None):
        return self._client._generate(model or self._client.model, _messages(contents), _dict(config))[0]


class ClaudeClient:
    def __init__(self, api_key, model=DEFAULT_MODEL, session=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ClaudeAPIError('missing_api_key')
        self._api_key = api_key
        self.model = model
        self._session = session if session is not None else requests.Session()
        self.chats, self.models = _Chats(self), _Models(self)

    def close(self):
        self._session.close()

    def _generate(self, model, messages, config):
        _validate_history(messages)
        maximum = _value(config, 'max_output_tokens')
        if maximum is None:
            maximum = 8192
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
            raise ClaudeAPIError('invalid_max_tokens')
        payload = {'model': model, 'max_tokens': maximum, 'messages': messages,
                   'thinking': {'type': 'disabled'}}
        system = _system(config)
        if system:
            payload['system'] = system
        tools = _tools(config)
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = {'type': 'auto'}
        try:
            http_response = self._session.post(
                MESSAGES_URL, headers={'x-api-key': self._api_key,
                    'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                json=payload, timeout=(4, 20), allow_redirects=False)
        except requests.Timeout:
            raise ClaudeAPIError('timeout') from None
        except Exception:
            raise ClaudeAPIError('transport_error') from None
        if http_response.status_code != 200:
            errors = {400: 'invalid_request', 401: 'authentication_error', 403: 'permission_error',
                      404: 'not_found', 413: 'request_too_large', 429: 'rate_limit_error',
                      500: 'api_error', 529: 'overloaded_error'}
            raise ClaudeAPIError(errors.get(http_response.status_code, 'http_error'), http_response.status_code)
        try:
            data = http_response.json()
            response = _response(data)
        except ClaudeAPIError:
            raise
        except Exception:
            raise ClaudeAPIError('invalid_response') from None
        return response, {'role': 'assistant', 'content': deepcopy(data['content'])}
