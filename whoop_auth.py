#!/usr/bin/env python3
"""
WHOOP OAuth2 認証ヘルパー
Access Token と Refresh Token を取得し .env に直接保存する。
Token が期限切れになったら再実行すること。

実行方法:
  .venv/bin/python3 whoop_auth.py
"""
import os
import re
import webbrowser
import urllib.parse
import requests
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

REDIRECT_URI = 'http://localhost:8888/callback'
SCOPES       = 'offline read:recovery read:cycles read:sleep read:workout read:profile'
ENV_PATH     = Path(__file__).parent / '.env'

client_id     = input('WHOOP Client ID: ').strip()
client_secret = input('WHOOP Client Secret: ').strip()
state         = os.urandom(16).hex()

auth_url = (
    'https://api.prod.whoop.com/oauth/oauth2/auth'
    f'?client_id={urllib.parse.quote(client_id)}'
    f'&redirect_uri={urllib.parse.quote(REDIRECT_URI)}'
    '&response_type=code'
    f'&scope={urllib.parse.quote(SCOPES)}'
    f'&state={state}'
)
print(f'\nブラウザを開きます: {auth_url}\n')
webbrowser.open(auth_url)


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code   = params.get('code', [''])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write('認証完了。このタブを閉じてください。'.encode('utf-8'))

        if not code:
            self.server.error = f'code なし: {params}'
            self.server.done  = True
            return

        r = requests.post('https://api.prod.whoop.com/oauth/oauth2/token', data={
            'grant_type':    'authorization_code',
            'code':          code,
            'redirect_uri':  REDIRECT_URI,
            'client_id':     client_id,
            'client_secret': client_secret,
        })
        if not r.ok:
            self.server.error = f'Token 取得失敗: {r.status_code} {r.text}'
            self.server.done  = True
            return

        data = r.json()
        self.server.access_token  = data.get('access_token', '')
        self.server.refresh_token = data.get('refresh_token', '')
        self.server.error = ''
        self.server.done  = True


server = HTTPServer(('localhost', 8888), CallbackHandler)
server.done          = False
server.access_token  = ''
server.refresh_token = ''
server.error         = ''

print('認証待ち中 (localhost:8888)...')
while not server.done:
    server.handle_request()

if server.error:
    print(f'\nエラー: {server.error}')
else:
    # .env を直接更新
    env_text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''

    def set_env(text, key, val):
        if re.search(rf'^{key}=', text, re.MULTILINE):
            return re.sub(rf'^{key}=.*', f'{key}={val}', text, flags=re.MULTILINE)
        return text + f'\n{key}={val}'

    env_text = set_env(env_text, 'WHOOP_CLIENT_ID',     client_id)
    env_text = set_env(env_text, 'WHOOP_CLIENT_SECRET', client_secret)
    env_text = set_env(env_text, 'WHOOP_ACCESS_TOKEN',  server.access_token)
    env_text = set_env(env_text, 'WHOOP_REFRESH_TOKEN', server.refresh_token)
    ENV_PATH.write_text(env_text, encoding='utf-8')

    print(f'\n✅ .env を更新しました')
    print(f'  ACCESS_TOKEN  : {server.access_token[:20]}...')
    print(f'  REFRESH_TOKEN : {server.refresh_token[:20]}...' if server.refresh_token else '  REFRESH_TOKEN : (なし)')
