#!/usr/bin/env python3
"""
Google Health API クライアント（読み取り専用）

認証は google_health_auth.py で1回だけ済ませる。
Google の refresh_token はローテートしないので、.env の書き戻しは
access_token だけ・失効時のみ。WHOOP/Fitbit のような多重実行事故が起きない。

⚠️ OAuth 同意画面が「外部 ＋ テスト」のため、**refresh_token は発行から7日で失効する**。
本番公開すれば無期限になるが、Google Health API のスコープは restricted 区分で、
本番公開には有償のセキュリティ評価(CASA, $500〜$4,500・2〜6週間)が要るため見送っている。
そのため週1回の再認証が前提の運用。切れたことに気づけないのが一番困るので、
--check を毎朝の同期から叩いて Slack に上げる。

実データの形を確認する:
  .venv/bin/python3 ghealth.py --probe            # 各データ型の最新3件を生JSONで出す
  .venv/bin/python3 ghealth.py --probe sleep      # 型を絞る

トークンの寿命を見る:
  .venv/bin/python3 ghealth.py --check            # 有効なら0、要再認証なら1で終了
"""
# venv の python は 3.9。`dict | None` を実行時に評価させないため必須。
from __future__ import annotations

import os
import re
import sys
import json
import datetime
import requests
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / '.env'
load_dotenv(ENV_PATH)

BASE      = 'https://health.googleapis.com/v4'
TOKEN_URL = 'https://oauth2.googleapis.com/token'

CLIENT_ID     = os.environ.get('GHEALTH_CLIENT_ID', '')
CLIENT_SECRET = os.environ.get('GHEALTH_CLIENT_SECRET', '')
REFRESH_TOKEN = os.environ.get('GHEALTH_REFRESH_TOKEN', '')
ACCESS_TOKEN  = os.environ.get('GHEALTH_ACCESS_TOKEN', '')

# ダッシュボードで使う指標 → Google Health API のデータ型 ID
DATA_TYPES = {
    'hrv':            'daily-heart-rate-variability',
    'rhr':            'daily-resting-heart-rate',
    'breathing_rate': 'daily-respiratory-rate',
    'spo2':           'daily-oxygen-saturation',
    'skin_temp':      'daily-sleep-temperature-derivations',
    'vo2max':         'daily-vo2-max',
    'sleep':          'sleep',
}


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)


def _set_env(key: str, val: str):
    text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''
    if re.search(rf'^{key}=', text, re.MULTILINE):
        text = re.sub(rf'^{key}=.*', f'{key}={val}', text, flags=re.MULTILINE)
    else:
        text = text.rstrip('\n') + f'\n{key}={val}\n'
    ENV_PATH.write_text(text, encoding='utf-8')


_refreshed = False


def refresh_access_token() -> str:
    """access_token は1時間で切れる。refresh_token は使い回せる（ローテートしない）。"""
    global ACCESS_TOKEN, _refreshed
    if not REFRESH_TOKEN:
        return ''
    try:
        r = requests.post(TOKEN_URL, data={
            'client_id':     CLIENT_ID,
            'client_secret': CLIENT_SECRET,
            'refresh_token': REFRESH_TOKEN,
            'grant_type':    'refresh_token',
        }, timeout=30)
    except Exception as e:
        print(f'  [GHealth] token refresh 通信失敗: {e}')
        return ''
    if not r.ok:
        print(f'  [GHealth] token refresh 失敗: {r.status_code} {r.text[:200]}')
        print('    → google_health_auth.py を再実行してください。')
        return ''
    ACCESS_TOKEN = r.json().get('access_token', '')
    if ACCESS_TOKEN:
        _set_env('GHEALTH_ACCESS_TOKEN', ACCESS_TOKEN)
        _refreshed = True
    return ACCESS_TOKEN


def get(data_type: str, params: dict | None = None):
    """保存済み access_token をまず使い、401 のときだけ更新して1回だけ再試行する。"""
    global ACCESS_TOKEN
    url = f'{BASE}/users/me/dataTypes/{data_type}/dataPoints'
    for attempt in (0, 1):
        if not ACCESS_TOKEN:
            if not refresh_access_token():
                return None
        try:
            r = requests.get(url, headers={
                'Authorization': f'Bearer {ACCESS_TOKEN}',
                'Accept': 'application/json',
            }, params=params or {}, timeout=30)
        except Exception as e:
            print(f'  [GHealth] GET {data_type} 失敗: {e}')
            return None
        if r.status_code == 401 and attempt == 0:
            ACCESS_TOKEN = ''
            continue
        if r.status_code == 403:
            print(f'  [GHealth] {data_type} → 403。OAuth 同意画面のテストユーザー登録と'
                  ' scope 付与を確認してください。')
            return None
        if r.status_code == 404:
            return None
        if not r.ok:
            print(f'  [GHealth] {data_type} → {r.status_code} {r.text[:200]}')
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def probe(only: str | None = None):
    """各データ型の最新数件を生のまま出す。フィルタ構文もレスポンス形も
    実物を見てから書くための下見用。"""
    if not configured():
        raise SystemExit('GHEALTH_* が .env に未設定です。google_health_auth.py を先に実行してください。')
    for key, dt in DATA_TYPES.items():
        if only and only not in (key, dt):
            continue
        print(f'\n===== {key}  ({dt}) =====')
        # sleep/exercise は pageSize 上限が 25、他は 1440。まずは3件だけ見る。
        data = get(dt, {'pageSize': 3})
        print(json.dumps(data, indent=2, ensure_ascii=False)[:4000] if data else '(なし)')


TOKEN_LIFETIME_DAYS = 7  # 外部＋テストモードの refresh_token の寿命


def check() -> int:
    """refresh_token がまだ生きているか確かめ、残り日数を返す。
    毎朝の同期から叩いて、切れる前・切れた直後に気づけるようにする。
    終了コード: 0=問題なし / 1=要再認証 / 2=まもなく失効"""
    if not configured():
        # まだセットアップ前。「失効」ではないので警告は出さない
        # （出すと接続するまで毎朝 Slack が鳴り続ける）。
        print('[GHealth] 未設定のためスキップ（google_health_auth.py 未実行）')
        return 0

    authorized_at = os.environ.get('GHEALTH_AUTHORIZED_AT', '')
    remaining = None
    if authorized_at:
        try:
            d0 = datetime.date.fromisoformat(authorized_at)
            remaining = TOKEN_LIFETIME_DAYS - (datetime.date.today() - d0).days
        except ValueError:
            pass

    # 実際に refresh してみるのが唯一確実な判定。成功すれば access_token も更新される。
    if not refresh_access_token():
        print('[GHealth] ❌ refresh_token が失効しています。再認証してください:')
        print('    cd ~/AI\\ Dev/diary-web && .venv/bin/python3 google_health_auth.py')
        return 1

    if remaining is not None and remaining <= 2:
        print(f'[GHealth] ⚠️ 残り約{remaining}日で失効します。早めに再認証を:')
        print('    cd ~/AI\\ Dev/diary-web && .venv/bin/python3 google_health_auth.py')
        return 2

    left = f'（残り約{remaining}日）' if remaining is not None else ''
    print(f'[GHealth] ✅ トークン有効{left}')
    return 0


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--check' in args:
        sys.exit(check())
    elif '--probe' in args:
        i = args.index('--probe')
        probe(args[i + 1] if len(args) > i + 1 else None)
    else:
        print(__doc__)
