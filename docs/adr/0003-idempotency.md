# 0003. 같은 메시지가 두 번 와도 결과는 한 번만

**상황** 재전송은 불가피하다. 서버는 ACK를 못 받으면 명령을 다시 보내고, 장비는 재연결 시 이벤트를 다시 보낸다.
컨베이어가 두 번 구동되거나, 완료가 두 번 집계되어 과금이 부풀면 안 된다.

**결정**
- 명령에는 `cmd_id`. 장비는 처리한 cmd_id를 기록하고, 다시 오면 구동 없이 이전 결과만 회신한다.
- 서버의 재전송은 **새 명령이 아니라 같은 cmd_id**로 보낸다.
- 이벤트에는 `evt_id`. 서버는 처음 보는 evt_id만 처리한다.
- 진행 수량은 "몇 개째"라는 누적값으로 보내 순서가 섞여도 값이 틀어지지 않게 한다.

**검증** `test_duplicate_command_runs_once`, `test_ack_timeout_resends_same_command`, `test_duplicate_event_is_ignored`.
