#!/usr/bin/env python3
"""
diary-web inbox puller
GitHub の inbox/{YYYY-MM-DD_HHMMSS}.txt を読み取り、
Claude で処理して NeoBrain/diary/ に追記する。
"""
import os
import re
import base64
from collections import defaultdict
from pathlib import Path
from datetime import datetime
import requests
import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')

GITHUB_TOKEN  = os.environ['GITHUB_TOKEN']
GITHUB_REPO   = os.environ['GITHUB_REPO']
DIARY_DIR     = Path.home() / 'Documents/NeoBrain/diary'
ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']

GH_API = 'https://api.github.com'
HEADERS = {
    'Authorization': f'Bearer {GITHUB_TOKEN}',
    'Accept': 'application/vnd.github+json',
}

# ファイル名パターン: YYYY-MM-DD_HHMMSS.txt
FILENAME_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_\d{6}\.txt$')
URL_RE = re.compile(r'https?://\S+')

JOURNAL_PROMPT = """\
あなたは日記アシスタントです。以下の入力を整形して追記用のMarkdownブロックだけを返してください。説明文・コードブロック記法は不要です。

## ユーザーの入力（複数エントリがある場合はまとめて処理）
{input}

## 出力フォーマット（厳守）
以下の形式で、追記するブロックのみを返す。既存ファイルの内容は含めない。

### 📝 モバイルメモ ({date})

**口頭メモ**
（出来事・感想・食事・運動など箇条書き）

**タグ候補:** (work/mercer, personal/triathlon など該当するもの)
**energy:** (1〜5)
**output_candidate:** (true/false)
"""

URL_SUMMARY_PROMPT = """\
以下のWebページの内容を日本語で3〜5行に要約してください。説明文は不要で、要約本文のみ返してください。

{content}
"""

TEMPLATE = """\
---
date: {date}
type: diary
tags: []
energy: 3
output_candidate: false
---

# {date}

## 口頭メモ

## 📋 今日のルーチン
- [ ] 起床: __時
- [ ] 睡眠: __時間
- [ ] 運動: __km / __分
- [ ] 朝食: __
- [ ] ランチ: __
- [ ] 夕食: __
- [ ] プロジェクト作業: __時間
- [ ] エネルギー（朝）:
- [ ] エネルギー（夜）:

## 😊 3つの嬉しいこと
1.
2.
3.

## 🎯 3つのやりたいこと
1.
2.
3.
"""


def list_inbox_files():
    """inbox/ 配下のファイル一覧を取得"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/inbox'
    res = requests.get(url, headers=HEADERS)
    if res.status_code == 404:
        return []
    res.raise_for_status()
    return [f for f in res.json() if FILENAME_RE.match(f['name'])]


def get_file(path: str):
    """ファイル内容と sha を取得"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}'
    res = requests.get(url, headers=HEADERS)
    res.raise_for_status()
    data = res.json()
    content = base64.b64decode(data['content']).decode('utf-8')
    return content, data['sha']


def delete_file(path: str, sha: str, label: str):
    """処理済みファイルを削除"""
    url = f'{GH_API}/repos/{GITHUB_REPO}/contents/{path}'
    payload = {
        'message': f'diary: processed {label}',
        'sha': sha,
    }
    res = requests.delete(url, headers=HEADERS, json=payload)
    res.raise_for_status()


def fetch_url_summary(url: str) -> str:
    """URLのページ内容を取得してClaudeで要約"""
    try:
        res = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        res.raise_for_status()
        # 簡易的にテキスト抽出（HTMLタグ除去）
        text = re.sub(r'<[^>]+>', ' ', res.text)
        text = re.sub(r'\s+', ' ', text).strip()
        # 長すぎる場合は先頭3000文字だけ使う
        text = text[:3000]
    except Exception as e:
        return f'（URL取得失敗: {e}）'

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=400,
        messages=[{
            'role': 'user',
            'content': URL_SUMMARY_PROMPT.format(content=text)
        }]
    )
    return msg.content[0].text.strip()


def enrich_with_url_summaries(text: str) -> str:
    """テキスト中のURLを検出してサマリーを付加"""
    urls = URL_RE.findall(text)
    if not urls:
        return text

    summaries = []
    for url in urls:
        print(f'    URLサマリー取得中: {url}')
        summary = fetch_url_summary(url)
        summaries.append(f'🔗 {url}\n> {summary}')

    return text + '\n\n' + '\n\n'.join(summaries)


def format_with_claude(date_str: str, raw_entries: str) -> str:
    """Claude で追記用ブロックに整形"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=1000,
        messages=[{
            'role': 'user',
            'content': JOURNAL_PROMPT.format(date=date_str, input=raw_entries)
        }]
    )
    return msg.content[0].text.strip()


def append_to_diary(date_str: str, block: str) -> Path:
    """既存ファイルがあれば追記、なければテンプレートで新規作成して追記"""
    diary_path = DIARY_DIR / f'{date_str}.md'
    if not diary_path.exists():
        diary_path.write_text(TEMPLATE.format(date=date_str), encoding='utf-8')
    with diary_path.open('a', encoding='utf-8') as f:
        f.write(f'\n{block}\n')
    return diary_path


def main():
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    files = list_inbox_files()

    if not files:
        print('inbox は空です。')
        return

    # 日付ごとにグループ化（YYYY-MM-DD_HHMMSS.txt → date_str）
    by_date = defaultdict(list)
    for f in files:
        m = FILENAME_RE.match(f['name'])
        if m:
            by_date[m.group(1)].append(f)

    for date_str, date_files in sorted(by_date.items()):
        print(f'処理中: {date_str} ({len(date_files)}件) ...')

        # 全エントリを結合
        all_entries = []
        file_shas = []
        for f in date_files:
            raw_content, sha = get_file(f['path'])
            all_entries.append(raw_content.strip())
            file_shas.append((f['path'], sha, f['name']))

        combined = '\n\n'.join(all_entries)

        # URLサマリーを付加
        enriched = enrich_with_url_summaries(combined)

        # Claude で整形
        block = format_with_claude(date_str, enriched)

        # 日記ファイルに追記
        diary_path = append_to_diary(date_str, block)
        print(f'  → {diary_path} に追記しました')

        # 処理済みファイルを削除
        for path, sha, name in file_shas:
            delete_file(path, sha, date_str)
            print(f'  → inbox/{name} を削除しました')

    print('完了。')


if __name__ == '__main__':
    main()
