"""Explicit, deterministic listing commands. Ordinary chat is not a mutation."""
import html
import re


class ListingInputError(ValueError):
    pass


_FIELDS = {
    '劇本': '名稱', '劇本名稱': '名稱', '名稱': '名稱',
    '人數': '人數', '時長': '時長', '售價': '價格', '價格': '價格',
    '類型': '類型', '標籤': '類型標籤', '類型標籤': '類型標籤',
    '簡介': '簡介', '劇情簡介': '簡介', '角色': '角色', '人物角色': '角色',
}
_FIELD_RE = re.compile(r'(?<!\S)(' + '|'.join(sorted(_FIELDS, key=len, reverse=True)) + r')\s*[:：]')
_BOT_PREFIX = re.compile(r'^(?:陸傲天|陸總|傲天|小六|小6|小陸)[\s，,:：]*')
_QUOTE = re.compile(r'《([^》\r\n]+)》')


def _list(value):
    items = [x.strip() for x in re.split(r'[/／、,，\n]', value)]
    if not items or any(not x for x in items) or len(items) != len(set(items)):
        raise ListingInputError('清單有空白或重複項目，請確認人數、類型或角色的分隔。')
    return items


def parse_fields(text):
    matches = list(_FIELD_RE.finditer(html.unescape(text)))
    text = html.unescape(text)
    data = {}
    for index, match in enumerate(matches):
        field = _FIELDS[match.group(1)]
        value = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
        if field in data:
            raise ListingInputError(f'「{field}」出現兩次，請只保留一個值。')
        if not value:
            raise ListingInputError(f'「{field}」還沒有內容；未準備好的欄位可以整行先不填。')
        if field == '價格':
            clean = re.sub(r'^(?:NT\$|NTD|新臺幣|新台幣|\$)\s*', '', value, flags=re.I)
            clean = re.sub(r'(?:元|/人|／人|每人)\s*$', '', clean).replace(',', '').strip()
            if not re.fullmatch(r'\d{1,6}', clean) or int(clean) > 100000:
                raise ListingInputError('售價請填 0～100000 的整數，例如「售價：2300」。')
            value = int(clean)
        elif field in ('角色', '類型', '人數'):
            value = _list(value)
            if field == '人數':
                expanded = []
                for item in value:
                    interval = re.fullmatch(r'(\d+)\s*[~～\-–]\s*(\d+)人?', item)
                    if interval:
                        low, high = int(interval[1]), int(interval[2])
                        if not 1 <= low <= high <= 30:
                            raise ListingInputError('人數範圍請填 1～30 人，且下限不可大於上限。')
                        expanded.extend(f'{n}人' for n in range(low, high + 1))
                    else:
                        expanded.append(item + '人' if re.fullmatch(r'\d+', item) else item)
                value = expanded
                if any(not re.fullmatch(r'(?:[1-9]\d?|浮動)人', x) for x in value):
                    raise ListingInputError('人數請填「7」或「6～8人」；浮動人數需在發布前補明確範圍。')
        elif field == '時長':
            value = re.sub(r'\\+(?=[~～])', '', value)
            if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:[~～\-–]\s*\d+(?:\.\d+)?)?', value):
                value += '小時'
        data[field] = value
    return data


def parse_listing_command(message):
    """Return {action,...}, or None for chat. Publishing requires an exact command."""
    text = _BOT_PREFIX.sub('', message.strip(), count=1).strip()
    compact = re.sub(r'[\s，,。!！]', '', text)
    exact = {
        '資料傳完直接上架': 'finish', '資料傳完請直接上架': 'finish',
        '上架狀態': 'status', '重試上架': 'retry', '取消上架': 'cancel',
        '上架說明': 'help', '上架教學': 'help',
    }
    if compact in exact:
        return {'action': exact[compact]}
    manual = re.fullmatch(r'配對角色\s*(\d+)\s*[:：]\s*(.+)', text, re.S)
    if manual:
        return {'action': 'assign_role', 'index': int(manual[1]), 'name': manual[2].strip()}
    cover = re.fullmatch(r'確認圖片\s*(\d+)\s*為封面[。！!]*', text)
    if cover:
        return {'action': 'confirm_cover', 'index': int(cover[1])}

    first_line = text.splitlines()[0]
    name_match = re.match(r'^(?:上架|新增)(?:劇本)?\s*《([^》\r\n]+)》', first_line)
    name = name_match[1].strip() if name_match else None
    # Whole-message affirmative grammar keeps image discussion out of mutations.
    batch = re.fullmatch(
        r'(?:接下來(?:這)?\s*(?P<n1>[\d一二三四五六七八九十兩]+)?\s*張|'
        r'下一張|這\s*(?P<n2>[\d一二三四五六七八九十兩]+)\s*張|'
        r'這批(?:\s*(?P<n3>[\d一二三四五六七八九十兩]+)\s*張)?)'
        r'\s*(?:是|為)\s*(?:《(?P<title>[^》\r\n]+)》\s*(?:的\s*)?)?'
        r'(?P<purpose>封面|角色圖)[。！!]*', text)
    if batch:
        raw = batch['n1'] or batch['n2'] or batch['n3']
        chinese = {'一': 1, '二': 2, '兩': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        count = (int(raw) if raw.isdigit() else chinese.get(raw, 0)) if raw else None
        if batch['purpose'] == '角色圖':
            if count is None:
                raise ListingInputError('角色圖請先說張數，例如「接下來這 7 張是《魔女論破》的角色圖」。')
            if not 1 <= count <= 30:
                raise ListingInputError('一次角色圖請宣告 1～30 張。')
            return {'action': 'label', 'name': batch['title'], 'purpose': 'portraits', 'count': count}
        if count not in (None, 1):
            raise ListingInputError('封面一次只收 1 張。')
        return {'action': 'label', 'name': batch['title'], 'purpose': 'cover', 'count': 1}

    existing = re.fullmatch(r'(?:補上?|新增|更換|更新|替換)\s*《([^》\r\n]+)》\s*(?:的\s*)?(角色圖|封面)[。！!]*', text)
    if existing:
        return {'action': 'begin', 'kind': 'portraits' if existing[2] == '角色圖' else 'cover', 'data': {'名稱': existing[1].strip()}}
    if re.match(r'^(?:上架|新增)(?:劇本)?(?:\s|《|[:：]|$)', text):
        data = parse_fields(text)
        if name:
            if data.get('名稱') and data['名稱'] != name:
                raise ListingInputError('指令與劇本欄位的名稱不同，請統一名稱。')
            data['名稱'] = name
        if not data.get('名稱'):
            raise ListingInputError('請提供劇本名稱，例如「陸總，上架《魔女論破》」。')
        return {'action': 'begin', 'kind': 'upload', 'data': data}
    if re.match(r'^(?:(?:補充|修改|更新)上架資料|上架資料)(?:\s|[:：]|$)', text):
        data = parse_fields(text)
        if not data:
            raise ListingInputError('請在「上架資料」後貼上欄位，例如「售價：2300」。')
        return {'action': 'merge_fields', 'data': data}
    # Form-only follow-ups are unambiguous; prose containing a field stays chat.
    if _FIELD_RE.match(text):
        return {'action': 'merge_fields', 'data': parse_fields(text)}
    return None


LISTING_HELP = '''小六上架：
1. 陸總，上架《劇本名稱》＋劇本資料
2. 說「接下來這張是《劇本名稱》的封面」，收到收圖提示後傳 1 張圖。
3. 要補角色圖時，說「接下來這 7 張是《劇本名稱》的角色圖」，再傳圖。
4. 最後說「資料傳完，直接上架」。只收資料或圖片不會發布。
查進度：上架狀態
改資料：上架資料，再貼需修改的欄位。
配對不明：配對角色 3：角色完整名稱
確認封面：確認圖片 1 為封面
失敗續做：重試上架；取消：取消上架。
同一人完成同一本；不用固定等幾秒。正式成功會附官網連結。'''
