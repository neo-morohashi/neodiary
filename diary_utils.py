"""
日記ファイルの共通ユーティリティ

- DIARY_DIR : 日記ディレクトリのパス
- TEMPLATE  : 日記テンプレート文字列（{date} プレースホルダー）
- ensure_diary(date_str) : 日記ファイルが存在しなければテンプレートで新規作成
"""

from pathlib import Path

DIARY_DIR = Path.home() / 'Documents/NeoBrain/diary'

TEMPLATE = """\
---
date: {date}
type: diary
tags: []
energy: 3
output_candidate: false
---

# {date}

## メモ

## 記録

## ルーチン
起床 __時 | 睡眠 __時間 | 運動 __
朝食 — | ランチ — | 夕食 —
エネルギー 朝__ → 夜__
"""


def ensure_diary(date_str: str) -> Path:
    """日記ファイルが存在しなければ全テンプレートで新規作成してPathを返す"""
    diary_path = DIARY_DIR / f'{date_str}.md'
    if not diary_path.exists():
        DIARY_DIR.mkdir(parents=True, exist_ok=True)
        diary_path.write_text(TEMPLATE.format(date=date_str), encoding='utf-8')
        print(f'  [diary] {diary_path} を新規作成')
    return diary_path
