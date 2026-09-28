#!/usr/bin/env bash
# Docker 없이 로컬에서 전체 데모 실행: 브로커 + 서버 + 트럭 2대 + 현장 장비 시뮬레이터
set -e
cd "$(dirname "$0")/.."
# 이미 떠 있는 데모가 있으면 중복 실행하지 않는다 (같은 트럭 ID가 두 개 붙으면 서로 연결을 빼앗음)
if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 || lsof -nP -iTCP:1883 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "데모가 이미 실행 중입니다. 먼저 ./scripts/stop_demo.sh 로 종료하세요."
  exit 1
fi
mkdir -p logs
python3 scripts/broker.py            > logs/broker.log 2>&1 & echo $! >  logs/pids
sleep 2
python3 -m uvicorn server.app:app --port 8080 > logs/server.log 2>&1 & echo $! >> logs/pids
sleep 3
python3 -m agent.agent --tenant acme   --truck TRK-01 --step "${DEMO_STEP:-2}" > logs/truck1.log 2>&1 & echo $! >> logs/pids
python3 -m agent.agent --tenant hanbit --truck TRK-02 --step "${DEMO_STEP:-2}" > logs/truck2.log 2>&1 & echo $! >> logs/pids
echo "관제:  http://localhost:8080/dock"
echo "작업 발생: python3 -m sim.scenario --tenant acme --truck TRK-01 --dock 5"
echo "통신 단절/복구: kill -USR1 \$(sed -n 3p logs/pids)    현장 비상정지: kill -USR2 \$(sed -n 3p logs/pids)"
echo "종료:  ./scripts/stop_demo.sh"
