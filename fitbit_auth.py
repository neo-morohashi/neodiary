#!/usr/bin/env python3
"""
Fitbit Web API OAuth2 認証ヘルパー（Authorization Code + PKCE）

Access Token と Refresh Token を取得し .env に直接保存する。
Fitbit の refresh_token は「1回使うと新しいものに交換される」ローテート方式なので、
同期側（sync_biometrics.py）が毎回 .env を書き戻す。二重実行すると壊れるため、
複数機で同時に回さないこと（WHOOP と同じく .no_fitbit マーカーで無効化できる）。

事前準備:
  1. https://dev.fitbit.com/apps/new でアプリを登録
     - OAuth 2.0 Application Type: "Personal"（個人の詳細データを取れるのは Personal のみ）
     - Callback URL: http://localhost:8889/callback
  2. 発行された Client ID / Client Secret を控える

実行方法:
  .venv/bin/python3 fitbit_auth.py
"""
import os
import re
import base64
import hashlib
import secrets
import webbrowser
import urllib.parse
import requests
from pathlib import Path
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler

REDIRECT_URI = 'http://localhost:8889/callback'
AUTH_URL     = 'https://www.fitbit.com/oauth2/authorize'
TOKEN_URL    = 'https://api.fitbit.com/oauth2/token'
ENV_PATH     = Path(__file__).parent / '.env'

# 取得したい指標に対応する scope。ダッシュボードで使うのは
# heartrate(HRV/安静時心拍) / sleep / respiratory_rate / oxygen_saturation /
# temperature(皮膚温) / cardio_fitness(VO2max)。activity は日次サマリー用。
SCOPES = ('activity cardio_fitness heartrate oxygen_saturation '
          'profile respiratory_rate sleep temperature')

load_dotenv(ENV_PATH)
client_id     = os.environ.get('FITBIT_CLIENT_ID', '') or input('Fitbit Client ID: ').strip()
client_secret = os.environ.get('FITBIT_CLIENT_SECRET', '') or input('Fitbit Client Secret: ').strip()

# PKCE: code_verifier は 43〜128 文字。S256 でチャレンジを作る。
code_verifier  = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip('=')
code_challenge = base64.urlsafe_b64encode(
    hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip('=')
state = secrets.token_hex(16)

auth_url = AUTH_URL + '?' + urllib.parse.urlencode({
    'client_id':             client_id,
    'response_type':         'code',
    'code_challenge':        code_challenge,
    'code_challenge_method': 'S256',
    'scope':                 SCOPES,
    'redirect_uri':          REDIRECT_URI,
    'state':                 state,
})

print(f'\nCallback URL: {REDIRECT_URI}')
print('  ↑ この値が dev.fitbit.com のアプリ設定と一字一句一致していないと'
      ' invalid_request で落ちます。\n')
print(f'ブラウザを開きます: {auth_url}\n')
webbrowser.open(auth_url)


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code   = params.get('code', [''])[0]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write('認証完了。このタブを閉じてください。'.encode('utf-8'))

        if params.get('state', [''])[0] != state:
            self.server.error = 'state 不一致（CSRF の疑い）。やり直してください。'
            self.server.done  = True
            return
        if not code:
            self.server.error = f'code なし: {params}'
            self.server.done  = True
            return

        basic = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
        r = requests.post(TOKEN_URL, headers={
            'Authorization': f'Basic {basic}',
            'Content-Type':  'application/x-www-form-urlencoded',
        }, data={
            'client_id':     client_id,
            'grant_type':    'authorization_code',
            'code':          code,
            'code_verifier': code_verifier,
            'redirect_uri':  REDIRECT_URI,
        }, timeout=30)
        if not r.ok:
            self.server.error = f'Token 取得失敗: {r.status_code} {r.text}'
            self.server.done  = True
            return

        data = r.json()
        self.server.access_token  = data.get('access_token', '')
        self.server.refresh_token = data.get('refresh_token', '')
        self.server.scope         = data.get('scope', '')
        self.server.error = ''
        self.server.done  = True


server = HTTPServer(('localhost', 8889), CallbackHandler)
server.done          = False
server.access_token  = ''
server.refresh_token = ''
server.scope         = ''
server.error         = ''

print('認証待ち中 (localhost:8889)...')
while not server.done:
    server.handle_request()

if server.error:
    print(f'\nエラー: {server.error}')
    raise SystemExit(1)

env_text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''


def set_env(text, key, val):
    if re.search(rf'^{key}=', text, re.MULTILINE):
        return re.sub(rf'^{key}=.*', f'{key}={val}', text, flags=re.MULTILINE)
    return text.rstrip('\n') + f'\n{key}={val}\n'


env_text = set_env(env_text, 'FITBIT_CLIENT_ID',     client_id)
env_text = set_env(env_text, 'FITBIT_CLIENT_SECRET', client_secret)
env_text = set_env(env_text, 'FITBIT_ACCESS_TOKEN',  server.access_token)
env_text = set_env(env_text, 'FITBIT_REFRESH_TOKEN', server.refresh_token)
ENV_PATH.write_text(env_text, encoding='utf-8')

print('\n✅ .env を更新しました')
print(f'  ACCESS_TOKEN  : {server.access_token[:20]}...')
print(f'  REFRESH_TOKEN : {server.refresh_token[:20]}...')
print(f'  SCOPE         : {server.scope}')

missing = sorted(set(SCOPES.split()) - set(server.scope.split()))
if missing:
    print(f'\n⚠ 許可されなかった scope: {" ".join(missing)}')
    print('  該当指標は取得できません（同意画面でチェックを外すとこうなります）。')
