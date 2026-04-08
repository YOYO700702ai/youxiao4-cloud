from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, PushMessageRequest, TextMessage,
)
from apscheduler.schedulers.background import BackgroundScheduler
import os, json, re, time, datetime
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
import gspread
from google.oauth2.service_account import Credentials

# ── 設定（Railway 環境變數）────────────────────────────────
CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
CHANNEL_SECRET       = os.environ['LINE_CHANNEL_SECRET']
MY_USER_ID           = os.environ['LINE_MY_USER_ID']
GEMINI_API_KEY       = os.environ['GEMINI_API_KEY']
GEMMA_MODEL          = os.environ.get('GEMMA_MODEL', 'gemma-4-31b-it')
GOOGLE_SHEET_ID      = os.environ['GOOGLE_SHEET_ID']
_creds_dict          = json.loads(os.environ['GOOGLE_CREDENTIALS_JSON'])

# ── Google Sheets ─────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']

def get_sheet(name):
    creds = Credentials.from_service_account_info(_creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        return sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=name, rows=1000, cols=20)

# ── 記憶系統 ──────────────────────────────────────────────
MAX_FACTS      = 20
COMPRESS_EVERY = 5

def load_memory():
    try:
        raw = get_sheet('memory').cell(1, 1).value
        return json.loads(raw) if raw else {"facts": [], "summary": "", "msg_count": 0, "recent_log": []}
    except:
        return {"facts": [], "summary": "", "msg_count": 0, "recent_log": []}

def save_memory(data):
    try:
        get_sheet('memory').update('A1', [[json.dumps(data, ensure_ascii=False)]])
    except Exception as e:
        print(f"save_memory 錯誤：{e}")

def build_memory_context(mem):
    parts = []
    if mem.get("summary"):
        parts.append(f"【近期摘要】{mem['summary']}")
    if mem.get("facts"):
        parts.append("【關於悠悠姐姐的記憶】\n" + '\n'.join(f"- {f}" for f in mem["facts"]))
    return '\n'.join(parts) if parts else ""

def add_memory_fact(fact):
    mem = load_memory()
    if fact not in mem.setdefault("facts", []):
        mem["facts"].append(fact)
    if len(mem["facts"]) > MAX_FACTS:
        mem["facts"] = mem["facts"][-MAX_FACTS:]
    save_memory(mem)
    return f"記住了：{fact}"

def compress_memory(mem):
    log = mem.get("recent_log", [])
    if not log:
        return mem
    log_text = '\n'.join([f"悠悠姐姐：{r['user']}\n助理：{r['ai']}" for r in log])
    prompt = (
        f"分析以下對話，整理：\n1.「重要事實」關於悠悠姐姐（每條≤30字，最多10條）\n"
        f"2.「對話摘要」（≤100字）\n舊摘要：{mem.get('summary','')}\n對話：\n{log_text}\n\n"
        f"格式：\n[事實]\n- 事實1\n[摘要]\n摘要內容"
    )
    try:
        resp = gemini_client.models.generate_content(model=GEMMA_MODEL, contents=prompt)
        text = resp.text.strip()
        new_facts = re.findall(r'^- (.+)', text, re.MULTILINE)
        m = re.search(r'\[摘要\]\n(.+)', text, re.DOTALL)
        mem["summary"] = m.group(1).strip() if m else mem.get("summary", "")
        merged = list(dict.fromkeys(mem.get("facts", []) + new_facts))
        mem["facts"] = merged[-MAX_FACTS:]
        mem["recent_log"] = []
    except Exception as e:
        print(f"記憶壓縮失敗：{e}")
    return mem

def update_memory_log(user_msg, ai_reply):
    mem = load_memory()
    mem.setdefault("recent_log", []).append({"user": user_msg[:200], "ai": ai_reply[:200]})
    mem["msg_count"] = mem.get("msg_count", 0) + 1
    if len(mem["recent_log"]) >= COMPRESS_EVERY:
        mem = compress_memory(mem)
    save_memory(mem)

def detect_memory_cmd(msg):
    m = re.search(r'(記住|幫我記住)[：:\s]*(.+)', msg)
    if m:
        return 'add', m.group(2).strip()
    if re.search(r'你記得|你的記憶|你記了什麼', msg):
        return 'show', None
    return None, None

# ── 筆記系統 ──────────────────────────────────────────────
def save_note(content):
    try:
        ws = get_sheet('notes')
        nid = len(ws.get_all_values()) + 1
        t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        ws.append_row([nid, t, content])
        return f"筆記#{nid} 已儲存"
    except Exception as e:
        return f"儲存失敗：{e}"

def list_notes():
    try:
        rows = get_sheet('notes').get_all_values()
        if not rows:
            return "目前沒有筆記"
        return '\n'.join([f"#{r[0]} [{r[1]}] {r[2]}" for r in rows[-10:]])
    except:
        return "讀取筆記失敗"

def delete_note(idx):
    try:
        ws = get_sheet('notes')
        rows = ws.get_all_values()
        new_rows = [r for r in rows if str(r[0]) != str(idx)]
        ws.clear()
        if new_rows:
            ws.append_rows(new_rows)
        return f"筆記#{idx} 已刪除"
    except Exception as e:
        return f"刪除失敗：{e}"

def detect_note_action(msg):
    if re.search(r'幫我記|記下來', msg):
        return 'add', re.sub(r'幫我記|記下來|：|:', '', msg).strip()
    if re.search(r'看筆記|我的筆記', msg):
        return 'list', None
    m = re.search(r'刪.*(第(\d+)|#(\d+)).*筆記|筆記.*(第(\d+)|#(\d+)).*刪', msg)
    if m:
        return 'delete', int(next(x for x in m.groups() if x and x.isdigit()))
    return None, None

# ── 定時提醒 ──────────────────────────────────────────────
def save_reminder(t, msg):
    try:
        ws = get_sheet('reminders')
        ws.append_row([len(ws.get_all_values()) + 1, t, msg, 'False'])
        return f"好，我會在 {t} 提醒你：{msg}"
    except Exception as e:
        return f"設定失敗：{e}"

def check_reminders():
    now = datetime.datetime.now().strftime("%H:%M")
    try:
        ws = get_sheet('reminders')
        for i, row in enumerate(ws.get_all_values()):
            if len(row) < 4 or row[3] == 'True':
                continue
            if row[1].strip() == now:
                push_message(f"⏰ 提醒：{row[2]}")
                ws.update_cell(i + 1, 4, 'True')
    except Exception as e:
        print(f"check_reminders 錯誤：{e}")

def detect_reminder(msg):
    m = re.search(r'(\d{1,2})[點:：](\d{0,2})\s*提醒我?\s*(.+)', msg)
    if m:
        h, mi = int(m.group(1)), int(m.group(2)) if m.group(2) else 0
        return f"{h:02d}:{mi:02d}", m.group(3).strip()
    return None, None

# ── 爬網頁 ────────────────────────────────────────────────
def fetch_url(url):
    try:
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer']):
            tag.decompose()
        lines = [l for l in soup.get_text('\n', strip=True).split('\n') if len(l.strip()) > 10]
        return '\n'.join(lines)[:3000]
    except Exception as e:
        return f"抓取失敗：{e}"

def extract_urls(text):
    return re.findall(r'https?://[^\s）\)]+', text)

# ── 系統提示（暫定，等取名後再調整）─────────────────────────
SYSTEM_PROMPT = (
    "你是悠悠姐姐的雲端助理。"
    "悠悠姐姐是遊戲化講師兼創作者，開了一家劇本殺店和一家密室逃脫。"
    "她喜歡無糖綠茶，不喜歡運動。不喜歡浮誇的道歉，有問題直接說解決方法。\n\n"
    "你的能力：\n"
    "- 爬網頁整理摘要\n"
    "- 筆記（幫我記/看筆記/刪掉第N條）\n"
    "- 定時提醒（X點提醒我XX）\n"
    "- 記憶（記住XX／你記得什麼）\n\n"
    "用繁體中文回覆，語氣自然。"
)

# ── Gemini ────────────────────────────────────────────────
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
chat_session  = gemini_client.chats.create(
    model=GEMMA_MODEL,
    config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
)

def ask_ai(text, silent=False):
    for i in range(3):
        try:
            if silent:
                return gemini_client.models.generate_content(
                    model=GEMMA_MODEL, contents=text,
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
                ).text.strip()
            return chat_session.send_message(text).text.strip()
        except Exception as e:
            err = str(e)
            if ('503' in err or 'overloaded' in err.lower()) and i < 2:
                time.sleep(5 * (2 ** i))
            elif i < 2:
                time.sleep(3)
            else:
                return "目前連不上，請稍後再試。"

# ── LINE Push ─────────────────────────────────────────────
def push_message(text):
    with ApiClient(Configuration(access_token=CHANNEL_ACCESS_TOKEN)) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=MY_USER_ID, messages=[TextMessage(text=text)])
        )

# ── APScheduler ───────────────────────────────────────────
scheduler = BackgroundScheduler(timezone='Asia/Taipei')
scheduler.add_job(check_reminders, 'interval', minutes=1)
scheduler.start()

# ── Flask ─────────────────────────────────────────────────
app           = Flask(__name__)
handler       = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

@app.route("/health")
def health():
    return "OK"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    if event.source.user_id != MY_USER_ID:
        return
    with ApiClient(configuration) as api_client:
        image_data = MessagingApiBlob(api_client).get_message_content(event.message.id)
    try:
        reply = gemini_client.models.generate_content(
            model=GEMMA_MODEL,
            contents=[
                types.Part.from_bytes(data=image_data, mime_type='image/jpeg'),
                types.Part(text="悠悠姐姐傳了這張圖，請描述。")
            ],
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)
        ).text.strip()
    except Exception as e:
        reply = f"圖片收到，但無法分析：{e}"
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)])
        )

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    if event.source.user_id != MY_USER_ID:
        return

    user_msg   = event.message.text
    extra_info = []

    # 記憶指令
    mem_cmd, mem_data = detect_memory_cmd(user_msg)
    if mem_cmd == 'add' and mem_data:
        extra_info.append(f"[記憶]: {add_memory_fact(mem_data)}")
    elif mem_cmd == 'show':
        ctx = build_memory_context(load_memory())
        extra_info.append(f"[你目前記得]: {ctx or '還沒有記憶'}")

    # 定時提醒
    rt, rm = detect_reminder(user_msg)
    if rt and rm:
        extra_info.append(f"[提醒]: {save_reminder(rt, rm)}")

    # 爬網頁
    for url in extract_urls(user_msg)[:2]:
        extra_info.append(f"[網頁 {url}]:\n{fetch_url(url)}")

    # 筆記
    na, nd = detect_note_action(user_msg)
    if na == 'add' and nd:
        extra_info.append(f"[筆記]: {save_note(nd)}")
    elif na == 'list':
        extra_info.append(f"[筆記]: {list_notes()}")
    elif na == 'delete' and nd:
        extra_info.append(f"[筆記]: {delete_note(nd)}")

    # 組合 prompt
    mem = load_memory()
    ctx = build_memory_context(mem)
    prompt = (ctx + '\n\n---\n\n' if ctx else '') + user_msg
    if extra_info:
        prompt += '\n\n' + '\n\n'.join(extra_info)

    reply = ask_ai(prompt)

    try:
        update_memory_log(user_msg, reply)
    except Exception as e:
        print(f"記憶更新失敗：{e}")

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply)])
        )

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
