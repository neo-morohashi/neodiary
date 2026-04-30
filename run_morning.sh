#!/bin/bash
# 朝6時専用: データ同期 + 日次ダイジェストメール送信
cd "/Users/neo/AI Dev/diary-web"
bash run_pull.sh
"/Users/neo/AI Dev/diary-web/.venv/bin/python3" daily_digest.py --send neo@mineo.com,neo.morohashi@mercer.com
