#!/usr/bin/env python3
"""
週次AI要約を生成して diary/weekly/YYYY-WXX.md に保存するスクリプト

データソース:
  - diary/YYYY-MM-DD.md  (## メモ / ## 記録 / ## ルーチン / frontmatter)
  - plaud/YYYY-MM-DD.md  (生トランスクリプト)
  - biometrics.db        (Oura/WHOOP 週次集計)
  - triathlon.db         (Garmin/Strava 週次集計)

引数:
  --date YYYY-MM-DD   週末日（日曜）を指定（省略時は直近日曜日）
"""

import os
import re
import sys
import sqlite3
from pathlib import Path
from datetime import date, timedelta
from statistics import mean

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from dotenv import load_dotenv
from diary_utils import DIARY_DIR

sys.path.insert(0, str(Path(__file__).parent.parent))
from slack_notify import send_slack_report

load_dotenv(Path(__file__).parent / '.env')
# Gmail 認証は ai-news-mailer の .env を参照
load_dotenv(Path(__file__).parent.parent / 'ai-news-mailer/.env', override=False)

PLAUD_DIR     = Path.home() / 'Documents/NeoBrain/plaud'
WEEKLY_DIR    = DIARY_DIR / 'weekly'
BIOMETRICS_DB = Path(__file__).parent / 'data/biometrics.db'
TRIATHLON_DB  = Path.home() / 'AI Dev/Triathlon/data/triathlon.db'
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# 1日あたりの読み込み上限（長大なplaudファイル対策）
MAX_DIARY_CHARS = 1500
MAX_PLAUD_CHARS = 800


# ── 日付ユーティリティ ────────────────────────────────────────────────────────

def last_sunday(ref: date) -> date:
    """直近の日曜日（ref が日曜なら ref 自身）"""
    return ref - timedelta(days=(ref.weekday() + 1) % 7) if ref.weekday() != 6 else ref


def week_dates(sunday: date) -> list[date]:
    """月曜〜日曜の7日間リスト"""
    monday = sunday - timedelta(days=6)
    return [monday + timedelta(days=i) for i in range(7)]


def iso_week_str(d: date) -> str:
    """例: 2026-W14"""
    y, w, _ = d.isocalendar()
    return f'{y}-W{w:02d}'


# ── 日記・plaudテキスト読み込み ───────────────────────────────────────────────

def extract_section(content: str, header: str) -> str:
    """指定セクションのテキストを抽出"""
    idx = content.find(header)
    if idx < 0:
        return ''
    start = idx + len(header)
    end = content.find('\n## ', start)
    text = content[start:end].strip() if end > 0 else content[start:].strip()
    return text


def read_diary_day(d: date) -> dict:
    """1日分の日記から必要な情報を取得"""
    path = DIARY_DIR / f'{d.isoformat()}.md'
    if not path.exists():
        return {}

    content = path.read_text(encoding='utf-8')

    # frontmatter
    fm_match = re.search(r'^---\n(.*?)\n---', content, re.DOTALL)
    fm = fm_match.group(1) if fm_match else ''
    tags = re.findall(r'^\s{2}-\s+(.+)$', fm, re.MULTILINE)
    energy_m = re.search(r'^energy:\s*(\d+)', fm, re.MULTILINE)
    energy = int(energy_m.group(1)) if energy_m else None
    oc_m = re.search(r'^output_candidate:\s*(true|false)', fm, re.MULTILINE)
    output_candidate = oc_m and oc_m.group(1) == 'true'

    # セクション
    memo    = extract_section(content, '## メモ') or extract_section(content, '## 口頭メモ')
    record  = extract_section(content, '## 記録')
    routine = extract_section(content, '## ルーチン')

    return {
        'date': d.isoformat(),
        'tags': tags,
        'energy': energy,
        'output_candidate': output_candidate,
        'memo': memo[:MAX_DIARY_CHARS],
        'record': record[:MAX_DIARY_CHARS],
        'routine': routine,
    }


def read_plaud_day(d: date) -> str:
    """1日分のplaudトランスクリプト（セクションヘッダー + 先頭テキスト）"""
    path = PLAUD_DIR / f'{d.isoformat()}.md'
    if not path.exists():
        return ''

    content = path.read_text(encoding='utf-8')
    # ## HH:MM セクションのヘッダーだけ列挙 + 各セクション先頭100文字
    sections = re.split(r'\n## ', content)
    parts = []
    for sec in sections[1:]:  # 最初の「# YYYY-MM-DD Plaud」はスキップ
        lines = sec.strip().splitlines()
        header = lines[0] if lines else ''
        # タイムスタンプ行と転写テキストを少量抜粋
        body_lines = [l for l in lines[1:] if l.strip() and not re.match(r'^\d{2}:\d{2}:\d{2}$', l.strip())]
        body = ' '.join(body_lines)[:200]
        if body:
            parts.append(f'[{header}] {body}')
    return '\n'.join(parts)[:MAX_PLAUD_CHARS]


# ── バイオメトリクス週次集計 ──────────────────────────────────────────────────

def get_weekly_biometrics(dates: list[date]) -> dict:
    if not BIOMETRICS_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(BIOMETRICS_DB)
        ds = [d.isoformat() for d in dates]
        placeholders = ','.join(['?'] * len(ds))
        rows = conn.execute(f"""
            SELECT date,
                   oura_readiness, oura_hrv, oura_sleep_hours,
                   whoop_recovery, whoop_hrv, whoop_sleep_hours, whoop_strain
            FROM biometrics WHERE date IN ({placeholders})
            ORDER BY date
        """, ds).fetchall()
        conn.close()
    except Exception:
        return {}

    days = []
    for r in rows:
        days.append({
            'date': r[0],
            'oura_readiness': r[1], 'oura_hrv': r[2], 'oura_sleep_h': r[3],
            'whoop_recovery': r[4], 'whoop_hrv': r[5], 'whoop_sleep_h': r[6],
            'whoop_strain': r[7],
        })

    def avg(key):
        vals = [d[key] for d in days if d.get(key) is not None]
        return round(mean(vals), 1) if vals else None

    return {
        'days': days,
        'avg_oura_readiness': avg('oura_readiness'),
        'avg_hrv': avg('oura_hrv') or avg('whoop_hrv'),
        'avg_sleep_h': avg('oura_sleep_h') or avg('whoop_sleep_h'),
        'avg_whoop_recovery': avg('whoop_recovery'),
        'total_strain': round(sum(d['whoop_strain'] for d in days if d.get('whoop_strain')), 1),
    }


# ── トレーニング週次集計 ──────────────────────────────────────────────────────

def get_weekly_training(dates: list[date]) -> dict:
    if not TRIATHLON_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(TRIATHLON_DB)
        start, end = dates[0].isoformat(), dates[-1].isoformat()

        acts = conn.execute("""
            SELECT name, sport_type, distance, moving_time,
                   average_heartrate, activity_training_load, calories
            FROM garmin_activities
            WHERE date(start_date) BETWEEN ? AND ?
            ORDER BY start_date
        """, (start, end)).fetchall()

        garmin = conn.execute("""
            SELECT date, acute_training_load, chronic_training_load, training_readiness_score
            FROM garmin_daily
            WHERE date BETWEEN ? AND ?
            ORDER BY date
        """, (start, end)).fetchall()

        conn.close()
    except Exception:
        return {}

    sessions = []
    for a in acts:
        name, sport, dist, t, hr, load, cals = a
        from sync_training import SPORT_JA, fmt_time, fmt_pace_run
        ja = SPORT_JA.get(sport, sport)
        dist_km = (dist or 0) / 1000
        line = f'{ja} {dist_km:.1f}km {fmt_time(t or 0)}'
        if hr:
            line += f' HR{hr:.0f}'
        if load:
            line += f' 負荷{load:.0f}'
        sessions.append(line)

    total_load = sum(a[5] or 0 for a in acts)
    total_time = sum(a[3] or 0 for a in acts)

    end_garmin = garmin[-1] if garmin else None

    return {
        'sessions': sessions,
        'session_count': len(sessions),
        'total_load': round(total_load),
        'total_time_h': round(total_time / 3600, 1),
        'end_ctl': end_garmin[2] if end_garmin else None,
        'end_atl': end_garmin[1] if end_garmin else None,
        'end_readiness': end_garmin[3] if end_garmin else None,
    }


# ── Claude プロンプト生成 ─────────────────────────────────────────────────────

def build_prompt(week_str: str, dates: list[date],
                 diary_days: list[dict], plaud_days: list[str],
                 bio: dict, training: dict) -> str:

    # 日次テキストブロック
    day_blocks = []
    for i, d in enumerate(dates):
        dd = diary_days[i]
        pl = plaud_days[i]
        if not dd and not pl:
            continue

        block = [f'### {d.isoformat()} ({["月","火","水","木","金","土","日"][d.weekday()]})']
        if dd.get('energy'):
            block.append(f'エネルギー: {dd["energy"]}/5')
        if dd.get('tags'):
            block.append(f'タグ: {", ".join(dd["tags"])}')
        if dd.get('routine'):
            block.append(f'ルーチン: {dd["routine"]}')
        if dd.get('memo'):
            block.append(f'メモ:\n{dd["memo"]}')
        if dd.get('record'):
            block.append(f'記録:\n{dd["record"]}')
        if pl:
            block.append(f'音声(plaud):\n{pl}')
        if dd.get('output_candidate'):
            block.append('⭐ output_candidate: true')
        day_blocks.append('\n'.join(block))

    days_text = '\n\n'.join(day_blocks) if day_blocks else '（記録なし）'

    # バイオメトリクスサマリー
    bio_text = '（データなし）'
    if bio:
        lines = []
        if bio.get('avg_oura_readiness'):
            lines.append(f'平均Oura Readiness: {bio["avg_oura_readiness"]}')
        if bio.get('avg_whoop_recovery'):
            lines.append(f'平均WHOOP Recovery: {bio["avg_whoop_recovery"]}')
        if bio.get('avg_hrv'):
            lines.append(f'平均HRV: {bio["avg_hrv"]}ms')
        if bio.get('avg_sleep_h'):
            lines.append(f'平均睡眠: {bio["avg_sleep_h"]}h')
        if bio.get('total_strain'):
            lines.append(f'週間Strain合計: {bio["total_strain"]}')
        bio_text = '\n'.join(lines) if lines else '（データなし）'

    # トレーニングサマリー
    train_text = '（記録なし）'
    if training and training.get('sessions'):
        lines = [
            f'セッション数: {training["session_count"]}',
            f'総負荷: {training["total_load"]}',
            f'総時間: {training["total_time_h"]}h',
        ]
        if training.get('end_ctl'):
            lines.append(f'CTL(週末): {training["end_ctl"]:.0f}')
        if training.get('end_atl'):
            lines.append(f'ATL(週末): {training["end_atl"]:.0f}')
        if training.get('end_readiness'):
            lines.append(f'Readiness(週末): {training["end_readiness"]:.0f}')
        lines.append('\nセッション詳細:')
        lines += [f'  - {s}' for s in training['sessions']]
        train_text = '\n'.join(lines)

    week_label = f'{dates[0].strftime("%m/%d")}〜{dates[-1].strftime("%m/%d")}'

    return f"""\
あなたは週次振り返りアシスタントです。以下のデータをもとに、{week_str}（{week_label}）の週次サマリーを生成してください。

## 日次記録
{days_text}

## 健康データ（週次平均）
{bio_text}

## トレーニングデータ
{train_text}

---

以下の4セクション構成でMarkdownを出力してください。セクション見出し以外の説明文は不要です。

## 📖 今週のダイジェスト
今週起きた出来事・気づき・感情の動きを3〜5段落で文章化。output_candidate がある場合はハイライト。

## 💪 健康・トレーニングレビュー
睡眠・HRV・回復スコアのトレンド評価。トレーニング量と質の振り返り。来週への推奨（強度・休養）。

## 💼 仕事テーマ別まとめ
work/* タグが付いた記録をプロジェクト・テーマ別に箇条書きで整理。気づきや懸念点も含める。

## 🎯 来週のフォーカス
今週のデータと傾向から「来週やること・調整すること」を3〜5点で提案。仕事・健康・生活習慣を混ぜてよい。
"""


# ── Claude 呼び出し ───────────────────────────────────────────────────────────

def call_claude(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=2000,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return msg.content[0].text.strip()


# ── 保存 ──────────────────────────────────────────────────────────────────────

DIGEST_DIR = Path.home() / 'Documents/NeoBrain/digest'


def save_weekly(week_str: str, dates: list[date], summary_md: str) -> Path:
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = WEEKLY_DIR / f'{week_str}.md'

    week_label = f'{dates[0].strftime("%Y/%m/%d")}〜{dates[-1].strftime("%m/%d")}'
    header = (
        f'---\n'
        f'date: {week_str}\n'
        f'type: weekly\n'
        f'week_start: {dates[0].isoformat()}\n'
        f'week_end: {dates[-1].isoformat()}\n'
        f'---\n\n'
        f'# {week_str} 週次サマリー（{week_label}）\n\n'
    )
    content = header + summary_md + '\n'

    path.write_text(content, encoding='utf-8')

    # digest フォルダにも保存
    digest_path = DIGEST_DIR / f'{week_str}.md'
    digest_path.write_text(content, encoding='utf-8')
    print(f'  → {digest_path} にも保存しました')

    return path


# ── メール送信 ────────────────────────────────────────────────────────────────

def markdown_to_html(md: str) -> str:
    """最小限の Markdown → HTML 変換"""
    lines = []
    for line in md.splitlines():
        if line.startswith('## '):
            line = f'<h2 style="margin:24px 0 8px;font-size:17px;color:#1a1a1a;border-bottom:2px solid #e8e8e8;padding-bottom:6px;">{line[3:]}</h2>'
        elif line.startswith('### '):
            line = f'<h3 style="margin:16px 0 6px;font-size:14px;color:#333;">{line[4:]}</h3>'
        elif line.startswith('- ') or line.startswith('* '):
            line = f'<li style="margin:4px 0;color:#444;font-size:13px;line-height:1.8;">{line[2:]}</li>'
        elif line.startswith('**') and line.endswith('**'):
            line = f'<strong>{line[2:-2]}</strong>'
        elif line.strip() == '---':
            line = '<hr style="border:none;border-top:1px solid #e8e8e8;margin:16px 0;">'
        elif line.strip() == '':
            line = '<br>'
        else:
            # インライン **bold**
            line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
            line = f'<p style="margin:6px 0;color:#444;font-size:13px;line-height:1.8;">{line}</p>'
        lines.append(line)
    return '\n'.join(lines)


def build_email_html(week_str: str, dates: list[date], summary_md: str) -> str:
    week_label = f'{dates[0].strftime("%Y/%m/%d")}〜{dates[-1].strftime("%m/%d")}'
    body_html = markdown_to_html(summary_md)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:680px;margin:0 auto;padding:28px 20px;color:#333;background:#fff;">
  <h1 style="margin:0 0 4px;font-size:22px;color:#1a1a1a;">週次サマリー {week_str}</h1>
  <p style="margin:0 0 28px;color:#888;font-size:13px;">{week_label}</p>
  {body_html}
  <hr style="border:none;border-top:1px solid #e8e8e8;margin:28px 0 16px;">
  <p style="color:#bbb;font-size:11px;margin:0;">このメールは NeoBrain 週次サマリーにより自動送信されました。</p>
</body>
</html>"""


def send_email(recipients: list[str], week_str: str, dates: list[date], summary_md: str) -> None:
    gmail_user = os.environ.get('GMAIL_USER', '')
    gmail_pass = os.environ.get('GMAIL_APP_PASSWORD', '')
    if not gmail_user or not gmail_pass:
        print('  ⚠️  GMAIL_USER / GMAIL_APP_PASSWORD が未設定のためメール送信をスキップ')
        return

    week_label = f'{dates[0].strftime("%m/%d")}〜{dates[-1].strftime("%m/%d")}'
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'週次サマリー {week_str}（{week_label}）'
    msg['From']    = gmail_user
    msg['To']      = ', '.join(recipients)
    msg.attach(MIMEText(build_email_html(week_str, dates, summary_md), 'html', 'utf-8'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, recipients, msg.as_string())

    print(f'  → メール送信完了: {", ".join(recipients)}')


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if '--date' in args:
        ref = date.fromisoformat(args[args.index('--date') + 1])
    else:
        ref = date.today()

    # --send recipient1@example.com,recipient2@example.com
    recipients = []
    if '--send' in args:
        idx = args.index('--send')
        if idx + 1 < len(args):
            recipients = [r.strip() for r in args[idx + 1].split(',')]

    sunday = last_sunday(ref)
    dates = week_dates(sunday)
    week_str = iso_week_str(sunday)
    week_label = f'{dates[0].strftime("%m/%d")}〜{sunday.strftime("%m/%d")}'

    print(f'週次サマリー生成中: {week_str} ({week_label})')

    # データ収集
    print('  日記・plaud 読み込み中...')
    diary_days = [read_diary_day(d) for d in dates]
    plaud_days = [read_plaud_day(d) for d in dates]

    print('  バイオメトリクス集計中...')
    bio = get_weekly_biometrics(dates)

    print('  トレーニングデータ集計中...')
    training = get_weekly_training(dates)

    # Claude 呼び出し
    print('  Claude にサマリー生成依頼中...')
    prompt = build_prompt(week_str, dates, diary_days, plaud_days, bio, training)
    summary = call_claude(prompt)
    # Claude が出力した冒頭の # 見出しを除去（save_weekly がヘッダーを付与するため）
    summary = re.sub(r'^#[^\n]*\n+', '', summary).lstrip()

    # 保存
    path = save_weekly(week_str, dates, summary)
    print(f'  → {path} に保存しました')

    # メール送信
    if recipients:
        print(f'  メール送信中: {", ".join(recipients)} ...')
        send_email(recipients, week_str, dates, summary)

    # Slack 送信
    week_label = f'{dates[0].strftime("%m/%d")}〜{dates[-1].strftime("%m/%d")}'
    print('  Slack 送信中...')
    ok = send_slack_report(
        title=f'週次サマリー {week_str}（{week_label}）',
        html_body=build_email_html(week_str, dates, summary),
        username='NeoBrain',
        emoji=':bar_chart:',
    )
    print(f'  → Slack {"送信完了" if ok else "送信失敗"}')

    print('\n--- 生成されたサマリー ---')
    print(summary)


if __name__ == '__main__':
    main()
