#!/bin/bash
cd "/Users/neo/AI Dev/diary-web"
"/Users/neo/AI Dev/diary-web/.venv/bin/python3" pull_inbox.py
"/Users/neo/AI Dev/diary-web/.venv/bin/python3" sync_biometrics.py
# Strava・Garmin アクティビティ・日次指標を先に同期してから書き込む
"/Users/neo/AI Dev/Triathlon/.venv/bin/python3" "/Users/neo/AI Dev/Triathlon/sync.py" --daily
"/Users/neo/AI Dev/Triathlon/.venv/bin/python3" "/Users/neo/AI Dev/Triathlon/garmin_sync.py" --daily
"/Users/neo/AI Dev/diary-web/.venv/bin/python3" sync_fitness.py
"/Users/neo/AI Dev/diary-web/.venv/bin/python3" sync_training.py
