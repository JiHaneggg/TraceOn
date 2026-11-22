# ml-service

ML 담당자가 구현하는 위험 점수 산출 서비스입니다.

- 상태: 미구현 (모델 종류·실행 환경 협의 전)
- 백엔드와의 연동 방식: 백엔드(Spring Boot)가 Subgraph JSON을 전달하고, 이 서비스는 점수만 반환합니다.
- 입출력 규격: `docs/api-specification.md`에서 확정 예정

## 오프라인 평가

`recipient_hybrid.py`에 수취 계좌 특성의 오프라인 결합 평가를 추가했다. 이력 확보 여부와 평소 대비 증가량의 효과를 각각 확인했다. 결과와 재현 방법은 [룰·ML 결합 평가](../docs/recipient-hybrid-evaluation.md)에 있다. 상위의 서비스 구현 상태와 별도로 실행하는 로컬 평가다.
