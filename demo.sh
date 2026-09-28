#!/usr/bin/env bash
# 면접 시연용 단축 명령. 사용법: ./demo.sh help
cd "$(dirname "$0")"
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
BASE="http://127.0.0.1:8080"

pid_of() {   # logs/pids 순서: 1 브로커, 2 서버, 3 TRK-01, 4 TRK-02, 5 현장 장비
  [ -f logs/pids ] || { echo "데모가 실행 중이 아닙니다. 먼저 ./demo.sh start"; exit 1; }
  sed -n "$1p" logs/pids
}
truck_line() { [ "$1" = "TRK-02" ] && echo 4 || echo 3; }
truck_log()  { [ "$1" = "TRK-02" ] && echo logs/truck2.log || echo logs/truck1.log; }
is_cut() {   # 시험용 통신 단절 상태인지 (트럭 로그의 마지막 단절/복구 표시로 판단)
  grep -E "통신 (단절|복구) \(시험\)" "$(truck_log "$1")" 2>/dev/null | tail -1 | grep -q "단절"
}

case "$1" in
  start)
    ./scripts/stop_demo.sh --reset >/dev/null
    PATH="$(pwd)/.venv/bin:$PATH" ./scripts/run_demo.sh | head -2
    ;;
  stop)
    ./scripts/stop_demo.sh
    ;;
  job)        # ./demo.sh job [acme|hanbit]
    T=${2:-acme}
    if [ "$T" = "hanbit" ]; then TR=TRK-02; D=2; else TR=TRK-01; D=5; fi
    if is_cut "$TR"; then
      echo "※ $TR 통신이 끊긴 상태입니다. 명령은 복구 후에 전달됩니다. (복구: ./demo.sh cut)"
    fi
    $PY -m sim.scenario --tenant "$T" --truck "$TR" --dock "$D"
    ;;
  cut)        # ./demo.sh cut [TRK-01|TRK-02]  — 한 번 끊기, 한 번 더 복구
    TR=${2:-TRK-01}
    kill -USR1 "$(pid_of "$(truck_line "$TR")")" || exit 1
    sleep 1
    if is_cut "$TR"; then echo "$TR 통신 끊음 → 복구하려면 ./demo.sh cut 한 번 더"
    else echo "$TR 통신 복구 → 쌓인 이벤트 재전송"; fi
    ;;
  estop)      # ./demo.sh estop [TRK-01|TRK-02]  — 현장 비상정지 버튼
    TR=${2:-TRK-01}
    kill -USR2 "$(pid_of "$(truck_line "$TR")")" && echo "$TR 비상정지 버튼 누름"
    ;;
  resume)     # ./demo.sh resume [acme|hanbit]  — 비상정지 해제 + 남은 수량 재개 (화면 [해제]→[재개]와 같음)
    T=${2:-acme}
    $PY - "$BASE" "$T" <<'PYEOF'
import json, sys, time, urllib.request
base, tenant = sys.argv[1], sys.argv[2]
def call(method, path):
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read() or b"null")
snap = call("GET", f"/api/{tenant}/snapshot")
halted = [j for j in snap["jobs"] if j["status"] == "HALTED"]
estop = [t["truck"] for t in snap["trucks"] if t["state"] == "ESTOP"]
for truck in {*estop, *(j["truck"] for j in halted)}:
    call("POST", f"/api/{tenant}/trucks/{truck}/reset"); print(f"{truck} 비상정지 해제")
if not estop and not halted:
    print("비상정지된 트럭이 없습니다."); sys.exit(0)
time.sleep(1)
for job in halted:
    call("POST", f"/api/{tenant}/jobs/{job['job_id']}/resume")
    print(f"작업 {job['job_id']} 재개 ({job['done']}/{job['pallets']}부터)")
PYEOF
    ;;
  drop)       # 현장 장비 UNIT-02가 20초 뒤 송신 중단 → 지도에 통신 두절
    kill "$(pid_of 5)" 2>/dev/null
    $PY -m sim.field_devices --drop UNIT-02 --drop-after 20 --noise 0.05 > logs/field.log 2>&1 &
    NEW=$!; sed -i.bak "5s/.*/$NEW/" logs/pids && rm -f logs/pids.bak
    echo "현장 장비 재시작: 20초 뒤 UNIT-02 송신 중단, 체크섬 오류 프레임 5% 섞음"
    ;;
  *)
    cat <<'HELP'
./demo.sh start            데모 초기화 후 시작 (브라우저: localhost:8080/dock, /field)
./demo.sh job              A사(acme) 작업 1건 발생
./demo.sh job hanbit       B사(hanbit) 작업 1건 발생
./demo.sh cut              TRK-01 통신 끊기 (한 번 더 입력하면 복구)
                           ※ 팔레트 1개 2초 = 적재 10초. 빠르게: DEMO_STEP=1 ./demo.sh start
./demo.sh estop            TRK-01 비상정지 버튼
./demo.sh resume           비상정지 해제 + 남은 수량 재개
./demo.sh drop             현장 장비 1대 송신 중단 → 지도에 통신 두절
./demo.sh stop             데모 종료
HELP
    ;;
esac
