#!/usr/bin/env python3
"""
Oura + WHOOP + Google Health バイオメトリクス同期
前日のデータを取得し、SQLite に保存して diary に書き込む。

必要な .env 変数:
  OURA_TOKEN             # Oura Personal Access Token
  WHOOP_CLIENT_ID        # WHOOP OAuth2 Client ID
  WHOOP_CLIENT_SECRET    # WHOOP OAuth2 Client Secret
  WHOOP_REFRESH_TOKEN    # WHOOP OAuth2 Refresh Token (whoop_auth.py で取得)
  GHEALTH_CLIENT_ID      # Google OAuth2 Client ID
  GHEALTH_CLIENT_SECRET  # Google OAuth2 Client Secret
  GHEALTH_REFRESH_TOKEN  # Google OAuth2 Refresh Token (google_health_auth.py で取得)

Google Health API (2026-03-24 GA) は Fitbit Web API の次世代版。Fitbit / Pixel Watch /
サードパーティ機器を1本の統合ストリームで返す。Google Fit REST API は2026年末停止・
2024年5月以降は新規登録不可、Health Connect は Android 端末内ローカル API なので
どちらも Mac の launchd からは使えない。
"""
import os
import re
import sys
import json
import time
import sqlite3
import requests
import ghealth
import datetime as dt
from pathlib import Path
from diary_utils import DIARY_DIR, ensure_diary
from datetime import date, timedelta
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / '.env'
load_dotenv(ENV_PATH)

OURA_TOKEN           = os.environ.get('OURA_TOKEN', '')
WHOOP_CLIENT_ID      = os.environ.get('WHOOP_CLIENT_ID', '')
WHOOP_CLIENT_SECRET  = os.environ.get('WHOOP_CLIENT_SECRET', '')
WHOOP_REFRESH_TOKEN  = os.environ.get('WHOOP_REFRESH_TOKEN', '')
WHOOP_ACCESS_TOKEN   = os.environ.get('WHOOP_ACCESS_TOKEN', '')
# Google Health の認証情報は ghealth.py 側が .env から読む

DB_PATH   = Path(__file__).parent / 'data/biometrics.db'

OURA_BASE  = 'https://api.ouraring.com/v2/usercollection'
WHOOP_BASE = 'https://api.prod.whoop.com/developer/v2'
WHOOP_TOKEN_URL = 'https://api.prod.whoop.com/oauth/oauth2/token'


def _set_env(key: str, val: str):
    """.env の1行を書き換える。キーが無ければ末尾に足す。"""
    text = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''
    if re.search(rf'^{key}=', text, re.MULTILINE):
        text = re.sub(rf'^{key}=.*', f'{key}={val}', text, flags=re.MULTILINE)
    else:
        text = text.rstrip('\n') + f'\n{key}={val}\n'
    ENV_PATH.write_text(text, encoding='utf-8')


# ── SQLite ──────────────────────────────────────────────────────────────────

# (カラム名, 型, fetch_ghealth() の戻り値キー) — DB migration と INSERT の両方で使う単一の正本
GHEALTH_FIELDS = [
    ('ghealth_rhr',            'INTEGER', 'rhr'),
    ('ghealth_hrv',            'REAL',    'hrv'),
    ('ghealth_sleep_hours',    'REAL',    'sleep_hours'),
    ('ghealth_sleep_eff',      'REAL',    'sleep_eff'),
    ('ghealth_spo2',           'REAL',    'spo2'),
    ('ghealth_breathing_rate', 'REAL',    'breathing_rate'),
    ('ghealth_skin_temp',      'REAL',    'skin_temp'),
    ('ghealth_vo2max',         'REAL',    'vo2max'),
]
GHEALTH_COLUMNS = [(name, typ) for name, typ, _ in GHEALTH_FIELDS]


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS biometrics (
            date              TEXT PRIMARY KEY,
            oura_readiness    INTEGER,
            oura_sleep_score  INTEGER,
            oura_sleep_hours  REAL,
            oura_hrv          REAL,
            oura_rhr          INTEGER,
            oura_body_temp    REAL,
            whoop_recovery    INTEGER,
            whoop_sleep_perf  REAL,
            whoop_sleep_hours REAL,
            whoop_hrv         REAL,
            whoop_rhr         INTEGER,
            whoop_strain      REAL
        )
    """)
    # 既存DBへの後付けカラム（Google Health）。CREATE TABLE は既存DBでは走らないので
    # PRAGMA で実列を見てから足りない分だけ ALTER する。
    have = {row[1] for row in conn.execute('PRAGMA table_info(biometrics)')}
    for name, typ in GHEALTH_COLUMNS:
        if name not in have:
            conn.execute(f'ALTER TABLE biometrics ADD COLUMN {name} {typ}')
    conn.commit()
    return conn


# ── Oura ────────────────────────────────────────────────────────────────────

def _get(url, **kwargs):
    """タイムアウト・接続エラー時に None を返す"""
    try:
        return requests.get(url, timeout=30, **kwargs)
    except Exception as e:
        print(f'    [warn] GET {url} 失敗: {e}')
        return None


def fetch_oura(date_str: str) -> dict:
    if not OURA_TOKEN:
        print('  [Oura] OURA_TOKEN が未設定。スキップ。')
        return {}
    headers = {'Authorization': f'Bearer {OURA_TOKEN}'}
    params  = {'start_date': date_str, 'end_date': date_str}
    result  = {}

    # Readiness score + body temperature deviation
    r = _get(f'{OURA_BASE}/daily_readiness', headers=headers, params=params)
    if r and r.ok:
        data = r.json().get('data', [])
        if data:
            d = data[0]
            result['readiness']  = d.get('score')
            result['body_temp']  = d.get('temperature_deviation')

    # Sleep score
    r = _get(f'{OURA_BASE}/daily_sleep', headers=headers, params=params)
    if r and r.ok:
        data = r.json().get('data', [])
        if data:
            result['sleep_score'] = data[0].get('score')

    # Detailed sleep: HRV (ms), RHR (bpm), total duration (sec)
    # Oura /sleep returns empty when start_date == end_date; query from day-1 and filter
    target_date = date.fromisoformat(date_str)
    r = _get(f'{OURA_BASE}/sleep', headers=headers, params={
        'start_date': (target_date - timedelta(days=1)).isoformat(),
        'end_date': date_str,
    })
    if r and r.ok:
        data = [x for x in r.json().get('data', []) if x.get('day') == date_str]
        if data:
            # 複数ある場合は最長の long_sleep を優先
            long_sleeps = [x for x in data if x.get('type') == 'long_sleep']
            main = max(long_sleeps or data, key=lambda x: x.get('total_sleep_duration') or 0)
            result['hrv']         = main.get('average_hrv')
            result['rhr']         = main.get('lowest_heart_rate')
            total_sec = main.get('total_sleep_duration') or 0
            result['sleep_hours'] = round(total_sec / 3600, 1) if total_sec else None

    return result


# ── WHOOP ────────────────────────────────────────────────────────────────────

_whoop_refreshed = False


def get_whoop_access_token() -> str:
    """Access Token を取得。Refresh Token で更新し、失敗時は保存済み Access Token を使う。

    WHOOP の refresh_token は使うたびにローテートするので、1プロセス内では
    最初の1回だけ更新して以後は使い回す。--from の一括同期で日数ぶん
    ローテートさせると、途中で1回失敗しただけでトークンが壊れる。"""
    global WHOOP_REFRESH_TOKEN, WHOOP_ACCESS_TOKEN, _whoop_refreshed
    env_path = Path(__file__).parent / '.env'

    if _whoop_refreshed and WHOOP_ACCESS_TOKEN:
        return WHOOP_ACCESS_TOKEN

    if WHOOP_REFRESH_TOKEN:
        r = requests.post(WHOOP_TOKEN_URL, data={
            'grant_type':    'refresh_token',
            'client_id':     WHOOP_CLIENT_ID,
            'client_secret': WHOOP_CLIENT_SECRET,
            'refresh_token': WHOOP_REFRESH_TOKEN,
        }, timeout=10)
        if r.ok:
            data = r.json()
            new_access  = data.get('access_token', '')
            new_refresh = data.get('refresh_token', '')
            if new_access:
                WHOOP_ACCESS_TOKEN = new_access
                env_text = env_path.read_text(encoding='utf-8')
                env_text = re.sub(r'WHOOP_ACCESS_TOKEN=.*', f'WHOOP_ACCESS_TOKEN={new_access}', env_text)
                if new_refresh:
                    WHOOP_REFRESH_TOKEN = new_refresh
                    env_text = re.sub(r'WHOOP_REFRESH_TOKEN=.*', f'WHOOP_REFRESH_TOKEN={new_refresh}', env_text)
                env_path.write_text(env_text, encoding='utf-8')
                _whoop_refreshed = True
                return new_access

    # Refresh 失敗 → 保存済み Access Token で試みる
    if WHOOP_ACCESS_TOKEN:
        print('  [WHOOP] Refresh Token 無効。保存済み Access Token を使用。')
        return WHOOP_ACCESS_TOKEN

    raise RuntimeError('WHOOP token 取得失敗。whoop_auth.py を再実行してください。')


def _whoop_pages(path: str, headers: dict, limit: int = 25, max_pages: int = 400):
    """WHOOP のページングを辿る。

    ⚠️ リクエストのパラメータ名は nextToken、レスポンスのフィールド名は next_token と
    綴りが違う。レスポンス側の綴りで投げると黙って1ページ目が返るため、
    気づかないまま無限ループになる。"""
    token = None
    for _ in range(max_pages):
        params = {'limit': limit}
        if token:
            params['nextToken'] = token
        r = _get(f'{WHOOP_BASE}{path}', headers=headers, params=params)
        if not r or not r.ok:
            return
        data = r.json()
        for rec in data.get('records', []):
            yield rec
        token = data.get('next_token')
        if not token:
            return


def _whoop_local_date(ts: str, offset: str) -> str:
    """RFC3339(UTC) + '+09:00' 形式のオフセットからローカル日付を出す。"""
    if not ts:
        return ''
    try:
        base = dt.datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return ''
    delta = dt.timedelta(0)
    m = re.match(r'([+-])(\d{2}):?(\d{2})', offset or '')
    if m:
        sign = 1 if m.group(1) == '+' else -1
        delta = sign * dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return (base + delta).date().isoformat()


_whoop_index = None
_whoop_index_from = None


def build_whoop_index(oldest: str) -> dict:
    """起床日 → 指標 の対応表を作る。

    WHOOP の cycle は「就寝〜翌日の就寝」で、cycle 先頭の睡眠が recovery を決める。
    日付は **起床日**（その睡眠が終わったローカル日）に寄せる。Oura の day、
    Garmin、Google Health と揃うので、ソース比較がそのまま成立する。

    旧実装は日付ごとに /cycle を期間指定で叩いて records[0] を採っていた。
    しかし WHOOP は期間に *重なる* cycle を全部返すので、多くの日で
    「その晩に始まる＝翌日の」cycle を掴み、値が1日前倒しになっていた
    （2026-09-20 に検出。隣接日に同じ値が入る症状で表面化）。"""
    headers = {'Authorization': f'Bearer {get_whoop_access_token()}'}

    # 睡眠を主キーにする。1睡眠＝1起床日で、cycle_id も持っているため対応付けが一意。
    sleeps = {}
    for s in _whoop_pages('/activity/sleep', headers):
        if s.get('nap'):
            continue
        d = _whoop_local_date(s.get('end', ''), s.get('timezone_offset', ''))
        if not d:
            continue
        if d < oldest:
            break
        # 同じ日に複数あれば長いほうを主睡眠とする
        prev = sleeps.get(d)
        if prev is None or (s.get('score') or {}).get('stage_summary', {}).get(
                'total_in_bed_time_milli', 0) > (prev.get('score') or {}).get(
                'stage_summary', {}).get('total_in_bed_time_milli', 0):
            sleeps[d] = s

    # recovery には日付フィールドが無いので created_at で打ち切る。数日の余裕を持たせて
    # おかないと、遅れて採点された分を取りこぼす。終了条件が無いと日次同期でも
    # 全履歴を辿ってしまう（毎朝100リクエスト超の無駄になる）。
    cutoff = (dt.date.fromisoformat(oldest) - dt.timedelta(days=3)).isoformat()
    recoveries = {}
    for r in _whoop_pages('/recovery', headers):
        if r.get('cycle_id') is not None:
            recoveries[r['cycle_id']] = r
        if (r.get('created_at') or '')[:10] < cutoff:
            break

    cycles = {}
    for c in _whoop_pages('/cycle', headers):
        cycles[c['id']] = c
        d = _whoop_local_date(c.get('start', ''), c.get('timezone_offset', ''))
        if d and d < oldest:
            break

    index = {}
    for d, s in sleeps.items():
        out = {}
        score = s.get('score') or {}
        out['sleep_perf'] = score.get('sleep_performance_percentage')
        stage = score.get('stage_summary') or {}
        total_ms = ((stage.get('total_light_sleep_time_milli') or 0) +
                    (stage.get('total_slow_wave_sleep_time_milli') or 0) +
                    (stage.get('total_rem_sleep_time_milli') or 0))
        out['sleep_hours'] = round(total_ms / 3_600_000, 1) if total_ms else None

        cid = s.get('cycle_id')
        rec = recoveries.get(cid) or {}
        rscore = rec.get('score') or {}
        out['recovery'] = rscore.get('recovery_score')
        out['hrv']      = rscore.get('hrv_rmssd_milli')
        out['rhr']      = rscore.get('resting_heart_rate')

        # strain は cycle 全体（起床〜次の就寝）の積み上げなので、cycle が閉じるまで
        # 確定しない。進行中の cycle では入れない（recovery は起床時点で確定済み）。
        cyc = cycles.get(cid) or {}
        if cyc.get('end'):
            out['strain'] = (cyc.get('score') or {}).get('strain')

        index[d] = {k: v for k, v in out.items() if v is not None}
    return index


def fetch_whoop(date_str: str) -> dict:
    # 機械固有の無効化スイッチ。.no_whoop マーカーがある機では WHOOP を一切叩かない
    # （WHOOP の refresh_token はローテート＆.env書き戻しのため、複数機で同時に走ると
    #  互いのトークンを無効化し合って破損する）。
    if (Path(__file__).parent / '.no_whoop').exists():
        print('  [WHOOP] .no_whoop マーカー検出 — この機では WHOOP 無効（二重ローテート防止）。スキップ。')
        return {}
    if not (WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET and (WHOOP_REFRESH_TOKEN or WHOOP_ACCESS_TOKEN)):
        print('  [WHOOP] 認証情報未設定。スキップ。')
        return {}

    global _whoop_index, _whoop_index_from
    # 一括同期では最古の日付で1回だけ作り、以後は使い回す（日ごとに叩かない）
    if _whoop_index is None or date_str < (_whoop_index_from or ''):
        try:
            _whoop_index_from = date_str
            _whoop_index = build_whoop_index(date_str)
        except Exception as e:
            print(f'  [WHOOP] 取得失敗: {e}')
            _whoop_index = {}
            return {}
        print(f'  [WHOOP] {len(_whoop_index)}日分を取得（{date_str} 以降）')

    return dict(_whoop_index.get(date_str, {}))


# ── Google Health ────────────────────────────────────────────────────────────

def _gh_num(v):
    """Google Health は数値を文字列で返す項目がある（beatsPerMinute: "66"）ほか、
    欠測を文字列 "NaN" で返す（baselineTemperatureCelsius）。両方を吸収する。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)   # NaN 除外
    if isinstance(v, str):
        try:
            f = float(v)
        except ValueError:
            return None
        return None if f != f else f
    return None


def _gh_civil_date(d: dict) -> str:
    """{'year':2026,'month':9,'day':20} → '2026-09-20'"""
    if not isinstance(d, dict) or not d.get('year'):
        return ''
    return f"{d['year']:04d}-{d.get('month', 1):02d}-{d.get('day', 1):02d}"


def _gh_local(ts: str, offset: str) -> 'dt.datetime | None':
    """RFC3339(UTC) + '32400s' 形式のオフセットからローカル時刻を作る。"""
    if not ts:
        return None
    try:
        base = dt.datetime.strptime(ts.replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None
    secs = 0
    if isinstance(offset, str) and offset.endswith('s'):
        try:
            secs = int(offset[:-1])
        except ValueError:
            secs = 0
    return base + dt.timedelta(seconds=secs)


def _gh_sleep_summary(sleep: dict):
    """睡眠セッションから (就寝日=起床側のローカル日付, 睡眠時間h, 効率%) を作る。

    Google Health の sleep は minutesAsleep を持たず stages の配列だけなので、
    AWAKE 以外の区間を足して実睡眠時間を出す。日付は「起床した日」に寄せる
    ―― daily-* 系が同じ夜を起床日で数えており、Oura の day / WHOOP の cycle とも揃う。"""
    iv = sleep.get('interval') or {}
    start = _gh_local(iv.get('startTime', ''), iv.get('startUtcOffset', '0s'))
    end   = _gh_local(iv.get('endTime', ''),   iv.get('endUtcOffset', '0s'))
    if not (start and end):
        return '', None, None

    asleep = 0.0
    for st in sleep.get('stages', []) or []:
        if st.get('type') == 'AWAKE':
            continue
        s = _gh_local(st.get('startTime', ''), st.get('startUtcOffset', '0s'))
        e = _gh_local(st.get('endTime', ''),   st.get('endUtcOffset', '0s'))
        if s and e and e > s:
            asleep += (e - s).total_seconds()

    in_bed = (end - start).total_seconds()
    hours = round(asleep / 3600, 1) if asleep else None
    eff   = round(asleep / in_bed * 100, 1) if (asleep and in_bed > 0) else None
    return end.date().isoformat(), hours, eff


def fetch_ghealth(date_str: str) -> dict:
    """Google Health API から1日分を取る。

    ⚠️ FITBIT 由来のみ採用する。このAPIは iPhone の Apple ヘルスケア経由で同期された
    Oura / WHOOP のデータ (platform=HEALTH_KIT) も同じストリームで返すため、
    素通しすると Oura 列と Google Health 列に同じ値が入り、ソース比較が成立しない。"""
    if (Path(__file__).parent / '.no_ghealth').exists():
        print('  [GHealth] .no_ghealth マーカー検出 — この機では無効。スキップ。')
        return {}
    if not ghealth.configured():
        print('  [GHealth] 認証情報未設定。スキップ。')
        return {}

    result = {}

    for key, (dtype, node_key) in ghealth.DATA_TYPES.items():
        if key == 'sleep':
            continue
        for point in ghealth.iter_points(dtype, page_size=100):
            if not ghealth.is_fitbit(point):
                continue
            node = point.get(node_key) or {}
            d = _gh_civil_date(node.get('date') or {})
            if not d:
                continue
            if d == date_str:
                if key == 'hrv':
                    # averageHeartRateVariabilityMilliseconds が Fitbit の dailyRmssd 相当。
                    # deepSleep... は深睡眠中のみの値なので、他ソースと揃うこちらを使う。
                    result['hrv'] = _gh_num(node.get('averageHeartRateVariabilityMilliseconds'))
                elif key == 'rhr':
                    n = _gh_num(node.get('beatsPerMinute'))
                    result['rhr'] = int(round(n)) if n is not None else None
                elif key == 'breathing_rate':
                    result['breathing_rate'] = _gh_num(node.get('breathsPerMinute'))
                elif key == 'spo2':
                    result['spo2'] = _gh_num(node.get('averagePercentage'))
                elif key == 'skin_temp':
                    # Oura の temperature_deviation と違い「絶対値」(33.7℃ など)。
                    # baseline は NaN で返ることが多いので偏差は作らず実測温度を持つ。
                    n = _gh_num(node.get('nightlyTemperatureCelsius'))
                    result['skin_temp'] = round(n, 2) if n is not None else None
                elif key == 'vo2max':
                    result['vo2max'] = _gh_num(node.get('vo2MaxMillilitersPerKilogramMinute')
                                               or node.get('vo2Max'))
                break
            if d < date_str:
                break   # 降順なので目的日を過ぎたら以降は見ない

    # 睡眠は stages から組み立てる（pageSize 上限が 25）
    for point in ghealth.iter_points('sleep', page_size=25):
        if not ghealth.is_fitbit(point):
            continue
        d, hours, eff = _gh_sleep_summary(point.get('sleep') or {})
        if not d:
            continue
        if d == date_str:
            result['sleep_hours'] = hours
            result['sleep_eff'] = eff
            break
        if d < date_str:
            break

    return {k: v for k, v in result.items() if v is not None}


# ── Diary 更新 ───────────────────────────────────────────────────────────────

def fmt(v, unit=''):
    return f'{v}{unit}' if v is not None else '—'

def build_biometrics_block(date_str: str, oura: dict, whoop: dict, gh: dict) -> str:
    rows = []

    def add(label, o_val, w_val, f_val, unit=''):
        if o_val is None and w_val is None and f_val is None:
            return
        rows.append(f'| {label} | {fmt(o_val, unit)} | {fmt(w_val, unit)} '
                    f'| {fmt(f_val, unit)} |')

    add('総合スコア',  oura.get('readiness'),   whoop.get('recovery'),    None)
    add('HRV',        oura.get('hrv'),          whoop.get('hrv'),         gh.get('hrv'),         'ms')
    add('安静時心拍',  oura.get('rhr'),          whoop.get('rhr'),         gh.get('rhr'),         'bpm')
    add('睡眠時間',    oura.get('sleep_hours'),  whoop.get('sleep_hours'), gh.get('sleep_hours'), 'h')
    add('睡眠スコア',  oura.get('sleep_score'),  whoop.get('sleep_perf'),  gh.get('sleep_eff'))
    # Oura は「基準からの偏差」、Google Health は「実測絶対値」で意味が違う。
    # 同じ行に並べると -0.1 と 33.7 が比較可能に見えてしまうので行を分ける。
    add('体温偏差',    oura.get('body_temp'),    None,                     None,                  '°C')
    add('皮膚温',      None,                     None,                     gh.get('skin_temp'),   '°C')
    add('呼吸数',      None,                     None,                     gh.get('breathing_rate'), '/分')
    add('SpO2',       None,                     None,                     gh.get('spo2'),        '%')
    add('VO2max',     None,                     None,                     gh.get('vo2max'))
    add('Strain',     None,                     whoop.get('strain'),      None)

    if not rows:
        return ''

    lines = [
        f'## 💤 バイオメトリクス',
        '',
        '| 指標 | Oura | WHOOP | Google Health |',
        '|------|:----:|:-----:|:------------:|',
    ] + rows
    return '\n'.join(lines)


def update_diary_biometrics(date_str: str, block: str):
    if not block:
        return
    diary_path = DIARY_DIR / f'{date_str}.md'

    # 日記は ~/Documents/NeoBrain 配下 = macOS TCC 保護下。launchd から実行する
    # python に Full Disk Access が無いと PermissionError になる。DB 保存は既に
    # 完了しているのでダッシュボードには影響させず、書き戻しのみ警告して握りつぶす。
    try:
        ensure_diary(date_str)
        content = diary_path.read_text(encoding='utf-8')
        header  = '## 💤 バイオメトリクス'

        if header in content:
            # 既存セクションを置換
            start = content.find(header)
            next_sec = content.find('\n## ', start + len(header))
            if next_sec < 0:
                content = content[:start].rstrip() + '\n\n' + block + '\n'
            else:
                content = content[:start] + block + content[next_sec:]
        else:
            content = content.rstrip() + '\n\n' + block + '\n'

        diary_path.write_text(content, encoding='utf-8')
        print(f'  → {diary_path} にバイオメトリクスを書き込みました')
    except PermissionError as e:
        print(f'  ⚠ 日記への書き戻しをスキップ（DB保存は成功済み）: {e}')
        print('    → 実行 python に Full Disk Access を付与すると書き戻しも有効化されます。')


# ── Main ─────────────────────────────────────────────────────────────────────

def sync_one(conn, target: str):
    print(f'\n── {target} ──')
    print('  Oura 取得中...')
    oura = fetch_oura(target)
    print(f'    → {oura}')

    print('  WHOOP 取得中...')
    whoop = fetch_whoop(target)
    print(f'    → {whoop}')

    print('  Google Health 取得中...')
    gh = fetch_ghealth(target)
    print(f'    → {gh}')

    if not oura and not whoop and not gh:
        print('  データなし。スキップ。')
        return

    # INSERT OR REPLACE は行ごと差し替えるので、取得できなかったソースの既存値を
    # NULL で潰してしまう。先に現在の行を読んで、新しい値があるものだけ上書きする。
    base_cols = [
        'oura_readiness', 'oura_sleep_score', 'oura_sleep_hours',
        'oura_hrv', 'oura_rhr', 'oura_body_temp',
        'whoop_recovery', 'whoop_sleep_perf', 'whoop_sleep_hours',
        'whoop_hrv', 'whoop_rhr', 'whoop_strain',
    ]
    cols = base_cols + [name for name, _, _ in GHEALTH_FIELDS]
    row = conn.execute(
        f'SELECT {", ".join(cols)} FROM biometrics WHERE date = ?', (target,)
    ).fetchone()
    vals = dict(zip(cols, row)) if row else {c: None for c in cols}

    fresh = {
        'oura_readiness':   oura.get('readiness'),
        'oura_sleep_score': oura.get('sleep_score'),
        'oura_sleep_hours': oura.get('sleep_hours'),
        'oura_hrv':         oura.get('hrv'),
        'oura_rhr':         oura.get('rhr'),
        'oura_body_temp':   oura.get('body_temp'),
        'whoop_recovery':   whoop.get('recovery'),
        'whoop_sleep_perf': whoop.get('sleep_perf'),
        'whoop_sleep_hours': whoop.get('sleep_hours'),
        'whoop_hrv':        whoop.get('hrv'),
        'whoop_rhr':        whoop.get('rhr'),
        'whoop_strain':     whoop.get('strain'),
    }
    fresh.update({name: gh.get(key) for name, _, key in GHEALTH_FIELDS})
    for k, v in fresh.items():
        if v is not None:
            vals[k] = v

    conn.execute(
        f'INSERT OR REPLACE INTO biometrics (date, {", ".join(cols)}) '
        f'VALUES ({", ".join(["?"] * (len(cols) + 1))})',
        [target] + [vals[c] for c in cols])
    conn.commit()
    print('  → DB 保存完了')

    block = build_biometrics_block(target, oura, whoop, gh)
    update_diary_biometrics(target, block)


WHOOP_DB_FIELDS = [
    ('whoop_recovery',    'recovery'),
    ('whoop_hrv',         'hrv'),
    ('whoop_rhr',         'rhr'),
    ('whoop_strain',      'strain'),
    ('whoop_sleep_perf',  'sleep_perf'),
    ('whoop_sleep_hours', 'sleep_hours'),
]


def rebuild_whoop(conn, oldest: str):
    """WHOOP 列だけを正しい起床日で貼り直す。

    日付ズレ修正後の過去データ補正用。全ソースを --from で回すと Oura や
    Google Health まで日数ぶん叩くことになるので、WHOOP だけを対象にする。
    索引に無い日は「その日の WHOOP データは存在しない」ということなので、
    ズレて入っていた古い値を消す（残すと直したそばから嘘が残る）。"""
    idx = build_whoop_index(oldest)
    print(f'  索引: {len(idx)}日分（{min(idx) if idx else "—"} 〜 {max(idx) if idx else "—"}）')

    cols = ', '.join(f'{c} = ?' for c, _ in WHOOP_DB_FIELDS)
    updated = cleared = 0

    for d, vals in sorted(idx.items()):
        conn.execute('INSERT OR IGNORE INTO biometrics (date) VALUES (?)', (d,))
        conn.execute(f'UPDATE biometrics SET {cols} WHERE date = ?',
                     [vals.get(k) for _, k in WHOOP_DB_FIELDS] + [d])
        updated += 1

    rows = conn.execute(
        'SELECT date FROM biometrics WHERE date >= ? AND whoop_recovery IS NOT NULL',
        (oldest,)).fetchall()
    for (d,) in rows:
        if d not in idx:
            conn.execute(f'UPDATE biometrics SET {cols} WHERE date = ?',
                         [None] * len(WHOOP_DB_FIELDS) + [d])
            cleared += 1

    conn.commit()
    print(f'  → {updated}日を更新 / {cleared}日をクリア')


def main():
    # 引数: --from YYYY-MM-DD [--to YYYY-MM-DD]
    #       --whoop-only --from YYYY-MM-DD   WHOOP 列だけ貼り直す（日付ズレ修正用）
    # 引数なし: 前日のみ
    args = sys.argv[1:]

    if '--whoop-only' in args:
        oldest = args[args.index('--from') + 1] if '--from' in args else '2023-01-01'
        print(f'WHOOP 列の再構築: {oldest} 以降')
        conn = init_db()
        rebuild_whoop(conn, oldest)
        conn.close()
        print('\n完了。')
        return

    if '--from' in args:
        idx = args.index('--from')
        start = date.fromisoformat(args[idx + 1])
        if '--to' in args:
            end = date.fromisoformat(args[args.index('--to') + 1])
        else:
            end = date.today() - timedelta(days=1)
        dates = []
        d = start
        while d <= end:
            dates.append(d.isoformat())
            d += timedelta(days=1)
        print(f'バイオメトリクス一括同期: {dates[0]} 〜 {dates[-1]} ({len(dates)}日)')
    else:
        dates = [(date.today() - timedelta(days=1)).isoformat()]
        print(f'バイオメトリクス同期: {dates[0]}')

    conn = init_db()
    for i, target in enumerate(dates):
        sync_one(conn, target)
        if len(dates) > 1:
            time.sleep(1.0)  # レート制限対策
    conn.close()
    print('\n完了。')


if __name__ == '__main__':
    main()
