#!/usr/bin/env python3
"""
WHOOP OAuth2 初回認証ヘルパー
1回だけ実行して Refresh Token を取得する。
取得した値を .env に追加すること。

実行方法:
  .venv/bin/python3 whoop_auth.py
"""
import webbrowser
import urllib.parse
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

REDIRECT_URI = 'http://localhost:8888/callback'
SCOPES       = 'offline read:recovery read:sleep read:workout read:profile'

client_id     = input('WHOOP Client ID: ').strip()
client_secret = input('WHOOP Client Secret: ').strip()

auth_url = (
    'https://api.prod.whoop.com/oauth/oauth2/auth'
    f'?client_id={urllib.parse.quote(client_id)}'
    f'&redirect_uri={urllib.parse.quote(REDIRECT_URI)}'
    '&response_type=code'
    f'&scope={urllib.parse.quote(SCOPES)}'
)
print(f'\nブラウザを開きます: {auth_url}\n')
webbrowser.open(auth_url)


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # ログ抑制

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code   = params.get('code', [''])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write('認証完了。このタブを閉じてください。'.encode('utf-8'))

        if not code:
            self.server.error = 'code パラメータが見つかりません'
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
        self.server.refresh_token = data.get('refresh_token', '')
        self.server.error = ''
        self.server.done  = True


server = HTTPServer(('localhost', 8888), CallbackHandler)
server.done          = False
server.refresh_token = ''
server.error         = ''

print('認証待ち中 (localhost:8888)...')
while not server.done:
    server.handle_request()

if server.error:
    print(f'\nエラー: {server.error}')
else:
    print('\n✅ 以下を .env に追加してください:\n')
    print(f'WHOOP_CLIENT_ID={client_id}')
    print(f'WHOOP_CLIENT_SECRET={client_secret}')
    print(f'WHOOP_REFRESH_TOKEN={server.refresh_token}')
