"""Render measured V2 artifacts into the team's reviewable record."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def percent(value):
    if value is None:
        return "정의 불가"
    digits = 4 if 0 < abs(value * 100) < .01 else 2
    return f"{value*100:.{digits}f}%"


def main():
    frozen_path = ROOT / "artifacts/rule-v2-frozen.json"
    report_path = ROOT / "artifacts/rule-v2-holdout.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "data/evaluation/HI-Small-v2/manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(frozen_path.read_bytes()).hexdigest() != report["frozenSha256"]:
        raise ValueError("Report belongs to another frozen configuration")
    config = frozen["selectedConfig"]
    text = ["# DB·룰 2차 평가 기록 — 2026-11-14", "",
        "## 결과", "",
        "기존 룰과 개선 후보를 동일한 신규 최종평가 250건에서 비교했다. 세탁 표본은 100건, 정상 표본은 150건이다. "
        "표본의 세탁 비율은 원본보다 높으므로 아래 주요 지표는 추출 확률의 역수로 보정한 추정치다.", "",
        "| 지표 | 기존 설정 | 개선 후보 |", "|---|---:|---:|"]
    baseline = report["results"]["baseline"]
    candidate = report["results"]["selected"]
    for label, key in [("Recall", "recall"), ("Precision", "precision"),
                       ("정상 거래 오탐률", "falsePositiveRate"), ("추정 알림 비율", "alertRate")]:
        text.append(f"| {label} | {percent(baseline['weighted'][key])} | {percent(candidate['weighted'][key])} |")
    text += ["", "| 표본 건수 | 기존 설정 | 개선 후보 |", "|---|---:|---:|"]
    for label, key in [("정탐 TP", "tp"), ("미탐 FN", "fn"), ("오탐 FP", "fp"), ("정상 TN", "tn")]:
        text.append(f"| {label} | {baseline['sample'][key]} | {candidate['sample'][key]} |")
    text += ["", "Recall은 개선됐지만 최종평가에서 목표 90%에 도달하지 못했고 정상 거래의 66%를 의심으로 분류했다. "
        "개선 후보를 최종 운영 기준으로 채택하지 않는다. ML과의 결합 및 추가 특성 검증이 필요하다. "
        "기존 설정의 Precision 100%는 이 표본에서 단 1건을 탐지한 결과로 안정적인 성능을 뜻하지 않는다.", "",
        "## 고정한 설정", "", "```json", json.dumps(config, indent=2), "```", "",
        "집금과 분산의 상대 계좌 수 기준을 분리했다. 점수 기준 75가 점수 65인 집금·분산 룰을 모두 막는 문제를 "
        "피하기 위해 V2 탐색에서는 점수 기준을 65로 고정하고 증거 조건을 조절했다. "
        "시간 창 1·3·7일, 최대 순환 길이 4·6홉, 집금·분산 상대 계좌 수 각각 2·3·5·8·12·20을 사전에 정해 216개 조합을 비교했다.", "",
        "## 데이터 분리와 재현", "",
        f"- 전처리 전체 {5078345:,}건의 디스크 인덱스로 과거 서브그래프를 준비했다. 이 인덱스는 오프라인 평가 도구이며 운영 DB는 Neo4j다.",
        f"- 검증 시작: {manifest['timeCutoffs']['validationStart']}. 최종평가 시작: {manifest['timeCutoffs']['holdoutStart']}.",
        "- 거래 수 기준 60/20/20 경계 시각을 사용하며 같은 분의 거래는 경계 한쪽에만 둔다. 앞 60%는 향후 ML 학습 영역으로 남긴다.",
        f"- 검증 250건과 최종평가 250건, 총 500건. 각 구간에서 8개 패턴과 패턴 미지정 세탁 거래를 층화 추출했다.",
        f"- 기존에 노출된 거래 {manifest['excludedPriorTransactions']:,}건 및 패턴 가족 {manifest['excludedPriorPatternFamilies']}개를 대상 추출에서 제외했다.",
        "- 최종평가 패턴은 최종평가 시작 이후에 시작된 가족만 포함했다. 검증·최종평가 간 패턴 가족 중복은 0개다. 역사적 입력 거래·계좌의 중복은 허용한다.",
        "- 검증 파일만 읽어 조건을 선택하고 설정·규칙 코드·데이터 해시를 고정했다. 그 다음 최종평가를 한 번 실행했으며 이 결과에 맞춰 조건을 다시 바꾸지 않았다.",
        f"- 최종평가 {candidate['truncatedGraphs']}/250개 그래프에서 조회 수 제한이 발생했다. 입력에는 과거 7일, 2홉, 계좌별 최근 50거래, 최대 1,000거래 제한이 있다.",
        "- 정답·패턴 유형은 평가 메타데이터에만 있고 룰 입력에는 없다. 금액은 원본 문자열을 보존한다.",
        "- 보정 지표의 대상 모집단은 위 제외·시간·패턴 가족 조건을 통과한 거래다. 전체 은행·전체 HI-Small 성능으로 일반화하지 않는다. 정상 표본 150건, 희소 유형별 표본은 작아 추정 불확실성이 크다.", "",
        "## 유형별 최종평가", "",
        "아래는 해당 유형에 속한 세탁 거래를 의심으로 잡은 비율이다. 정확한 유형을 맞힌 분류 정확도가 아니다. 현재 구현한 룰은 집금·분산·순환 3개다.", "",
        "| 정답 유형 | 기존 정탐/표본 | 후보 정탐/표본 |", "|---|---:|---:|"]
    for kind, result in candidate["byPattern"].items():
        old = baseline["byPattern"][kind]
        text.append(f"| {kind} | {old['tp']}/{old['total']} | {result['tp']}/{result['total']} |")
    text += ["", "## 검증 데이터의 오탐 제약별 후보", "",
        "다음 값은 검증 데이터에서만 계산했다. 이 대안들을 최종평가에 추가로 맞춰 보지 않았다.", "",
        "| 정상 오탐률 상한 | 검증 Recall 최대 추정치 | 실제 검증 오탐률 |", "|---|---:|---:|"]
    for budget in [.1, .2, .3, .5]:
        pool = [r for r in frozen["sweep"] if r["weighted"]["falsePositiveRate"] <= budget]
        if pool:
            best = max(pool, key=lambda r: (r["weighted"]["recall"], r["weighted"]["precision"] or 0))
            text.append(f"| {percent(budget)} | {percent(best['weighted']['recall'])} | {percent(best['weighted']['falsePositiveRate'])} |")
    text += ["", "## 구현 검증", "",
        "전처리·인덱스·표본 추출 테스트 16개와 룰·평가·HTTP 계약 테스트 24개, 총 40개가 통과했다. "
        "실행 중인 로컬 API에 새 집금·분산 설정을 전달해 로컬 계산 결과와 일치하는 것도 확인했다.", "",
        "## 산출물과 다음 통합", "",
        "- `data/evaluation/HI-Small-v2/manifest.json`: 선택 정책·구간·표본 수·데이터 해시.",
        "- `artifacts/rule-v2-frozen.json` 및 `.sha256`: 최종평가 전에 고정한 설정과 검증 결과.",
        "- `artifacts/rule-v2-holdout.json`: 동일 최종평가에서의 기존/후보 비교.",
        "- `rule-service/configs/rule-v2-research.json`: 연동 실험용 설정. API 기본값을 자동 변경하지 않는다.",
        "- 1차 기록과 `artifacts/neo4j-rule-pilot.json`은 보존했다. 이 기록의 수치와 표본을 섞지 않는다.",
        "- 다음 단계는 ML 담당에게 공통 그래프·룰 점수·근거를 연결하고, 새로 확보한 최종평가에서 하이브리드의 오탐 감소를 확인하는 것이다. 현재 최종평가는 이후 개발 판단에 사용되었으므로 다음 변경의 최종 검증에는 재사용하지 않는다.", ""]
    out = ROOT / "docs/db-rule-v2.md"
    out.write_text("\n".join(text), encoding="utf-8")
    config_out = ROOT / "rule-service/configs/rule-v2-research.json"
    config_out.parent.mkdir(parents=True, exist_ok=True)
    config_out.write_text(json.dumps({"status": "research_candidate_not_promoted", "config": config,
        "frozenSha256": report["frozenSha256"], "holdoutTargetMet": report["holdoutTargetMet"]}, indent=2), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
