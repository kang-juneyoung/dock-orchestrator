#!/usr/bin/env bash
# Docker 없이 로컬에서 전체 데모 실행: 브로커 + 서버 + 트럭 2대 + 현장 장비 시뮬레이터
set -e
cd "$(dirname "$0")/.."
mkdir -p logs
python3 scripts/broker.py            > logs/broker.log 2>&1 & echo $! >  logs/pids
sleep 2
python3 -m uvicorn server.app:app --port 8000 > logs/server.log 2>&1 & echo $! >> logs/pids
sleep 3
python3 -m agent.agent --tenant acme   --truck TRK-01 > logs/truck1.log 2>&1 & echo $! >> logs/pids
python3 -m agent.agent --tenant hanbit --truck TRK-02 > logs/truck2.log 2>&1 & echo $! >> logs/pids
python3 -m sim.field_devices --noise 0.03 > logs/field.log 2>&1 & echo $! >> logs/pids
echo "관제:  http://localhost:8000/dock"
echo "지도:  http://localhost:8000/field"
echo "작업 발생: python3 -m sim.scenario --tenant acme --truck TRK-01 --dock 5"
echo "통신 단절/복구: kill -USR1 \$(sed -n 3p logs/pids)    현장 비상정지: kill -USR2 \$(sed -n 3p logs/pids)"
echo "종료:  kill \$(cat logs/pids)"
