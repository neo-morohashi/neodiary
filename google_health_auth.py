#!/usr/bin/env python3
"""
Google Health API OAuth2 認証ヘルパー

Google Health API は Fitbit Web API の次世代版（2026-03-24 リリース）。
Fitbit / Pixel Watch / サードパーティ機器のデータを、Google アカウントに紐づく
1本の統合ストリームとして server-side OAuth で読める。

Fitbit Web API と違い **リフレッシュトークンはローテートしない**ので、
WHOOP/Fitbit で必要だった .env 書き戻しと多重実行の防止が要らない。

事前準備（ブラウザ作業）:
  1. https://console.cloud.google.com/ でプロジェクトを作る（既存のものでも可）
  2. 「Google Health API」を有効化
     https://console.cloud.google.com/apis/library/health.googleapis.com
  3. OAuth 同意画面を設定
     - User type: External / 公開ステータスは「テスト」のままでよい
     - **テストユーザーに自分の Google アカウントを追加**（これを忘れると 403）
     - Data Access で "Google Health API" を検索し、下の SCOPES を許可対象に入れる
  4. 認証情報 → OAuth クライアント ID を作成
     - アプリケーションの種類: **ウェブ アプリケーション**
     - 承認済みのリダイレクト URI: http://localhost:8889/callback
       （--manual で実行する場合は https://www.google.com を登録する）
  5. 作成直後の画面で **「JSON をダウンロード」** を押す
     client_secret は作成直後にしか表示されない（2025年6月以降マスクされる）。
     閉じてしまったら復旧できないのでクライアントを作り直すこと。

実行方法:
  .venv/bin/python3 google_health_auth.py
      → .env → ~/Downloads/client_secret*.json → 手入力 の順に認証情報を探す
  .venv/bin/python3 google_health_auth.py --json ~/Downloads/client_secret_xxx.json
  .venv/bin/python3 google_health_auth.py --manual   # localhost が使えない場合
"""
import os
import re
import sys
import json
import secrets
import datetime as dt
import webbrowser
import urllib.parse
import requests
from pathlib import Path
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler

AUTH_URL  = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
ENV_PATH  = Path(__file__).parent / '.env'

LOCAL_REDIRECT  = 'http://localhost:8889/callback'
MANUAL_REDIRECT = 'https://www.google.com'

# HRV・安静時心拍・SpO2・呼吸数・皮膚温 → health_metrics、睡眠 → sleep、
# VO2max → activity_and_fitness。読み取りだけなので全て .readonly。
SCOPES = [
    'https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly',
    'https://www.googleapis.com/auth/googlehealth.sleep.readonly',
    'https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly',
]

manual = '--manual' in sys.argv[1:]
redirect_uri = MANUAL_REDIRECT if manual else LOCAL_REDIRECT

def creds_from_json(path: Path):
    """Cloud Console の「JSON をダウンロード」で落ちるファイルから ID/Secret を読む。
    client_secret は作成直後の画面でしか表示されない（2025年6月以降マスクされる）ので、
    手で写すより JSON を渡すほうが確実で速い。"""
    try:
        blob = json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        raise SystemExit(f'JSON を読めませんでした: {path} ({e})')
    # 種別によって "web" か "installed" のどちらかに入っている
    node = blob.get('web') or blob.get('installed') or blob
    cid, sec = node.get('client_id', ''), node.get('client_secret', '')
    if not (cid and sec):
        raise SystemExit(f'client_id / client_secret が見つかりません: {path}')
    return cid, sec


load_dotenv(ENV_PATH)

json_arg = None
if '--json' in sys.argv:
    i = sys.argv.index('--json')
    if len(sys.argv) > i + 1:
        json_arg = Path(sys.argv[i + 1]).expanduser()

client_id     = os.environ.get('GHEALTH_CLIENT_ID', '')
client_secret = os.environ.get('GHEALTH_CLIENT_SECRET', '')

if json_arg:
    client_id, client_secret = creds_from_json(json_arg)
    print(f'認証情報を読み込みました: {json_arg.name}')
elif not (client_id and client_secret):
    # ~/Downloads に落ちたままの認証情報 JSON を拾う（最新のもの）
    found = sorted(Path.home().glob('Downloads/client_secret*.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if found:
        client_id, client_secret = creds_from_json(found[0])
        print(f'認証情報を読み込みました: ~/Downloads/{found[0].name}')
    else:
        client_id     = input('Google Client ID: ').strip()
        client_secret = input('Google Client Secret: ').strip()

# client_id の形を先に確かめる。ここが違うと Google 側では
# 「401 invalid_client / Client missing a project id」という原因の分かりにくい
# エラー画面になり、アカウントやスコープの問題と誤診しやすい。
if not client_id.endswith('.apps.googleusercontent.com'):
    print(f'\n❌ Client ID の形式が正しくありません: {client_id!r}')
    print('   正しい形: 123456789012-xxxxxxxxxxxx.apps.googleusercontent.com')
    print('   これは「クライアント」→ OAuth クライアント ID を作成したときに発行される値です。')
    print('   プロジェクト ID / プロジェクト番号 / API キーとは別物なので注意してください。')
    raise SystemExit(1)
if not client_secret:
    raise SystemExit('\n❌ Client Secret が空です。作成直後の画面の「JSON をダウンロード」から取得してください。')

state = secrets.token_hex(16)

auth_url = AUTH_URL + '?' + urllib.parse.urlencode({
    'client_id':     client_id,
    'redirect_uri':  redirect_uri,
    'response_type': 'code',
    'scope':         ' '.join(SCOPES),
    'access_type':   'offline',
    # 再認証時にも refresh_token を確実に返させる。これが無いと2回目以降
    # access_token しか返らず、常駐同期が翌日から動かなくなる。
    'prompt':        'consent',
    'state':         state,
})

print(f'\nリダイレクト URI: {redirect_uri}')
print('  ↑ Google Cloud Console の OAuth クライアント設定と完全一致が必要です。\n')
print(f'ブラウザを開きます:\n{auth_url}\n')
webbrowser.open(auth_url)


def exchange(code: str) -> dict:
    r = requests.post(TOKEN_URL, data={
        'code':          code,
        'client_id':     client_id,
        'client_secret': client_secret,
        'redirect_uri':  redirect_uri,
        'grant_type':    'authorization_code',
    }, timeout=30)
    if not r.ok:
        raise SystemExit(f'\nToken 取得失敗: {r.status_code} {r.text}')
    return r.json()


if manual:
    print('同意後に https://www.google.com/?code=... へ飛ぶので、')
    print('アドレスバーの code= の値（&scope= の手前まで）を貼ってください。')
    raw = input('code: ').strip()
    # URL ごと貼られても拾えるようにする
    m = re.search(r'[?&]code=([^&\s]+)', raw)
    code = urllib.parse.unquote(m.group(1)) if m else raw
    data = exchange(code)
else:
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('認証完了。このタブを閉じてください。'.encode('utf-8'))
            if q.get('state', [''])[0] != state:
                self.server.error = 'state 不一致（CSRF の疑い）。やり直してください。'
            elif q.get('error'):
                self.server.error = f"認可拒否: {q['error'][0]}"
            elif not q.get('code'):
                self.server.error = f'code なし: {q}'
            else:
                self.server.code = q['code'][0]
                self.server.error = ''
            self.server.done = True

    server = HTTPServer(('localhost', 8889), CallbackHandler)
    server.done, server.code, server.error = False, '', ''
    print('認証待ち中 (localhost:8889)...')
    while not server.done:
        server.handle_request()
    if server.error:
        raise SystemExit(f'\nエラー: {server.error}')
    data = exchange(server.code)

access  = data.get('access_token', '')
refresh = data.get('refresh_token', '')
if not refresh:
    raise SystemExit('\nrefresh_token が返りませんでした。Google Cloud Console で'
                     'このアプリのアクセス権を一度削除してからやり直してください。')

text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''


def set_env(t, key, val):
    if re.search(rf'^{key}=', t, re.MULTILINE):
        return re.sub(rf'^{key}=.*', f'{key}={val}', t, flags=re.MULTILINE)
    return t.rstrip('\n') + f'\n{key}={val}\n'


# 外部＋テストモードの refresh_token は発行から7日で失効する。いつ取ったかを
# 残しておかないと「あと何日もつか」が分からず、切れて初めて気づくことになる。
authorized_at = dt.datetime.now().strftime('%Y-%m-%d')

for k, v in [('GHEALTH_CLIENT_ID', client_id), ('GHEALTH_CLIENT_SECRET', client_secret),
             ('GHEALTH_ACCESS_TOKEN', access), ('GHEALTH_REFRESH_TOKEN', refresh),
             ('GHEALTH_AUTHORIZED_AT', authorized_at)]:
    text = set_env(text, k, v)
ENV_PATH.write_text(text, encoding='utf-8')

expires = (dt.datetime.now() + dt.timedelta(days=7)).strftime('%-m/%-d')
print('\n✅ .env を更新しました')
print(f'  ⏳ テストモードのため {expires} 頃に失効します（再実行で延長）')
print(f'  ACCESS_TOKEN  : {access[:24]}...')
print(f'  REFRESH_TOKEN : {refresh[:24]}...')
print(f'  SCOPE         : {data.get("scope", "")}')

granted = set(data.get('scope', '').split())
missing = [s for s in SCOPES if s not in granted]
if missing:
    print('\n⚠ 許可されなかった scope:')
    for s in missing:
        print(f'   {s.rsplit("/", 1)[-1]}')
    print('  該当指標は取得できません。同意画面でチェックを外すとこうなります。')
