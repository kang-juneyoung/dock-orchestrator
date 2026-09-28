#!/usr/bin/env bash
# 데모 프로세스 전부 종료 (pids 파일이 꼬여도 남은 프로세스까지 정리)
# --reset: 작업·경보 기록(DB)까지 지우고 깨끗하게 시작
cd "$(dirname "$0")/.."
[ -f logs/pids ] && kill $(cat logs/pids) 2>/dev/null
pkill -f "scripts/broker.py|uvicorn server.app|agent.agent|sim.field_devices" 2>/dev/null
sleep 1
if [ "$1" = "--reset" ]; then
  rm -f -- *.db *.db-shm *.db-wal
  echo "데모 종료 + 작업·경보 기록 초기화"
else
  echo "데모 종료"
fi
