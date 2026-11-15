# RAG 문서 등록·갱신 관리 — 2026-11-15

문서를 **후보 등록 → 변경 확인 → 검색 반영** 순서로 관리한다. 검색 반영 전에는 기존 자료를 계속 사용한다. 반영할 때 현재/새 검색 색인을 모두 해시별로 보존하므로 과거 보고서는 원래 `corpusSha256 + chunkId`로 원문을 조회한다.

현재는 로컬 관리자 명령으로 제공한다. 사용자용 업로드 화면이나 공개 쓰기 API는 만들지 않았다. 표준 라이브러리만 사용하며 Neo4j·LLM·API 키가 필요 없다.

## 기존 자료 연결

저장소 루트에서 실행한다.

```powershell
python rag-service/corpus_manager.py init
python rag-service/corpus_manager.py status
python rag-service/corpus_manager.py history --document-id kofiu-str
```

- `init`: 현재 색인과 원본 스냅샷을 대조해 등록부를 만든다. 기존 5문서·26청크의 본문·ID·색인 해시는 유지한다. 다시 실행해도 버전을 추가하지 않는다.
- `status`: 문서명·발행기관·원문 주소·관할·종류·시행일·출처 버전·원본 해시·수집/등록 시각과 현재 사용 중인 버전을 표시한다.
- `history`: 특정 문서의 등록 이력과 현재 사용 버전을 확인한다.
- 빈 환경도 `init`으로 시작할 수 있다. 기존 색인이 있다면 원본 스냅샷과 해당 메타데이터가 있어야 한다.

새 PC에서 기존 공식 자료부터 수집할 때는 최초 1회 `python rag-service/corpus.py`를 실행한 다음 `init`을 실행한다. 이미 등록부가 있는 환경에서는 `corpus.py --refresh` 대신 아래 `check`와 `publish`를 사용한다. 이전 명령으로 관리 절차를 건너뛰려 하면 중단한다.

## 새 문서 등록

1. 허용된 공식 사이트의 UTF-8 HTML 원문을 확보한다.
2. 문서별 JSON 메타데이터를 작성한다. 기존 `rag-service/corpus_sources.json`의 한 객체를 형식 참고로 사용한다.
3. 아래처럼 후보로 등록한다.

```powershell
python rag-service/corpus_manager.py register --source data/rag/intake/new-source.json --snapshot data/rag/intake/new-document.html
```

예시 형식이다. 주소·제목·기관·본문 구간은 실제 원문을 보고 채운다. 예시 문자열을 공식 자료로 등록하지 않는다.

```json
{
  "documentId": "new-official-document",
  "title": "실제 원문 제목",
  "url": "https://kofiu.go.kr/실제_원문_경로",
  "publisher": "실제 발행기관",
  "jurisdiction": "KR",
  "sourceKind": "official_guidance",
  "effectiveDate": null,
  "sections": [
    {"heading": "원문 구간 시작 제목", "end": "원문 구간 다음 제목"}
  ]
}
```

`documentId`는 영문 소문자·숫자·하이픈의 고정 식별자다. 필수 필드는 문서 ID·제목·URL·기관·관할·자료 종류·`effectiveDate`·선택 구간이다. 선택 필드로 `publishedDate`, `modifiedDate`, `validUntil`, `version`, `applicability`, `requiredText`를 기록한다. 날짜는 `YYYY-MM-DD`, 모르는 시행일은 `null`로 둔다. 수집일을 시행일로 대체하지 않는다. `validUntil`은 해당 날짜 미포함의 유효 종료일이다.

`--snapshot` 대신 `--fetch`를 지정하면 메타데이터의 원문 주소를 한 번 내려받는다. 허용 호스트는 `corpus.py`의 `ALLOWED_HOSTS`로 고정하고 HTTPS와 리다이렉트를 확인한다. PDF·Word·일반 텍스트·임의 사내 포털 주소는 현재 지원하지 않는다. 파일을 가져와 등록한 경우 `capturedAt`은 시스템에 가져온 시각이며, 실제 최초 다운로드 시각이나 출처의 진위를 인증하는 값은 아니다. 원문 주소·기관·시행일은 검토자가 대조해야 한다.

결과의 `status=candidate`는 등록 성공이다. `revisionId`를 보관한다. 검색 결과는 아직 바뀌지 않는다. 동일 원문·동일 메타데이터를 반복 등록하면 기존 버전을 재사용한다.

## 기존 문서 변경 확인

```powershell
# 저장한 수정본과 대조
python rag-service/corpus_manager.py check --document-id kofiu-str --snapshot data/rag/intake/str-new.html

# 공식 페이지를 한 번 다시 받아 대조
python rag-service/corpus_manager.py check --document-id kofiu-str --fetch

# 제목·버전·본문 선택 구간 등도 바뀌었다면 새 메타데이터를 함께 전달
python rag-service/corpus_manager.py check --document-id kofiu-str --source data/rag/intake/str-source-new.json --snapshot data/rag/intake/str-new.html
```

`check`의 기본 메타데이터는 해당 문서의 마지막 등록 버전이다. `--source`의 문서 ID가 다른 경우 거부한다. 다운로드 실패·점검 화면·필수 식별 문구 누락·본문 구간 변경·2MB 초과 자료는 등록하지 않으며 현재 검색 자료를 유지한다. 일정에 따른 자동 수집은 포함하지 않는다.

변경 결과는 세 종류를 구분한다.

| 항목 | 의미 |
|---|---|
| `sourceBytesChanged` | HTML 원본 바이트가 달라짐. 메뉴·서식 변경도 포함 |
| `normalizedDocumentChanged` | HTML 전체의 정규화된 가시 본문이 달라짐 |
| `metadataOrSelectorsChanged` | 제목·발행기관·시행일·버전·선택 절 등 등록 정보가 달라짐 |

선택된 본문의 변경과 메타데이터 변경을 확인한다. 아래 식별자는 실제 등록 결과로 바꾼다.

```powershell
python rag-service/corpus_manager.py diff --document-id kofiu-str --revision-id 받은_revisionId
```

`selectedSectionDiffs`에 현재/후보 본문 차이, `metadataChanges`에 등록 정보 차이를 반환한다. 본문 미리보기는 절당 20,000자·차이 출력 합계 12,000자로 제한하고 잘리면 `previewTruncated=true`다. 전체 원문 해시와 인용 데이터는 유지한다. 본문 밖의 메뉴/서식 변경은 원본 변경으로만 표시될 수 있다. 이 기능이 법적 유효성·출처의 진위를 판정하지는 않는다.

## 검토한 버전 검색에 반영

```powershell
python rag-service/corpus_manager.py publish --document-id kofiu-str --revision-id 받은_revisionId --expected-corpus-sha256 status에서_확인한_현재_색인_해시
```

처음 문서를 반영해 현재 색인이 없을 때는 `--expected-corpus-sha256 none`을 사용한다. 다른 작업으로 색인이 바뀌었다면 중단하므로 `status`와 변경 내용을 다시 확인한다. 이 값은 사용자에게 추가 승인을 묻는 절차가 아니라 동시 작업 중 덮어쓰기를 막는 값이다.

반영 시 다른 문서의 원문·청크 ID는 보존한다. 현재/새 색인을 `versions/<corpusSha256>.json`으로 보관한 뒤 `index.json`을 한 번에 교체한다. 원본 스냅샷은 `raw/snapshots/<documentId>/<sourceSha256>.html`에 버전별로 남긴다. 새 코드로 실행 중인 RAG 서버는 다음 요청부터 새 색인을 읽으며, 진행 중인 요청은 자신이 선택한 버전을 사용한다. 기존에 실행된 옛 코드의 서버는 최초 한 번 재시작해야 한다.

과거 보고서 조회는 보고서에 기록된 원래 색인 해시를 전달한다. 버전이 없거나 손상되면 현재 자료로 대체하지 않고 오류를 반환한다. 이전 문서로 되돌리려면 `history`의 과거 `revisionId`를 현재 색인 해시와 함께 `publish`한다. 기존 보고서·색인·등록 이력은 삭제하지 않는다. 되돌림으로 만들어진 전체 색인의 해시는 과거 전체 색인 해시와 다를 수 있다.

## 저장·공유

```text
data/rag/registry.json                           문서별 등록 버전·원본 해시·메타데이터
data/rag/index.json                              현재 검색 색인
data/rag/versions/<corpusSha256>.json             과거/새 검색 색인 전체
data/rag/raw/snapshots/<documentId>/<hash>.html   원본 HTML
```

위 자료는 Git 제외 대상이다. 코드만 공유한 PC에는 원문·과거 색인이 생기지 않으므로, 과거 보고서를 조회할 팀원에게는 허용된 범위에서 `data/rag/` 묶음을 별도로 전달해야 한다. 백엔드는 보고서의 원래 `corpusSha256`·Graph·Rule·Query를 보관한다.

동시 등록/반영은 `.corpus-manager.lock`으로 막는다. 작업이 강제로 종료되어 파일이 남았다면 다른 문서 관리 작업이 실행 중인지 확인한 뒤 해당 잠금 파일만 정리한다. 자동으로 남의 잠금을 삭제하지 않는다. 색인은 교체 전까지 이전 상태를 유지하므로 새 파일 저장에 실패하면 이전 버전으로 계속 조회할 수 있다.

## 확인 결과

- 자동 테스트 **159개** 통과: 데이터 16·룰 24·RAG 119. 이 중 문서 관리 19개는 합성 자료로 등록·중복 방지·변경 미리보기·발행·되돌림·과거 보고서 검증·실행 중 색인 갱신·장애 시 보존을 검사한다.
- 기존 5문서·26청크를 등록부에 연결했으며 기존 색인 해시는 `a811aa50fffe640332a25843af96b1ba39ba77aada9a3020b20acf8693c51263`로 유지했다.
- 실제 등록부 검사에서 5문서의 동일 자료 재등록 시 등록부/색인 바이트 무변경, 26청크 원본 대조, 이전 3문서 색인의 원문 조회를 확인했다. 결과는 `artifacts/rag-document-registry-verification.json`에 있다.
- 새 코드로 재시작한 RAG에서 실제 Neo4j 12사례의 보고서 검사·원문 93건·사실 32건 조회를 확인했다. 금액/시간/건수/인용/본문 변조도 거부했다. 결과는 `artifacts/rag-document-evidence-verification.json`이며 Spring·ML·UI·실제 LLM을 모두 포함한 검사는 아니다.
- 국내 법령이나 은행 내규가 새로 수집되었다는 의미는 아니다. 자료 수집·사람의 근거 적합성 검토는 후속 작업이다.

```powershell
python -m unittest discover -s rag-service/tests -p test_corpus_manager.py -q
python scripts/verify-document-registry.py --output artifacts/new-document-registry-check.json
```

관리자 검증 스크립트는 기존 등록 자료의 재등록이 무변경인지 확인하고 이전 원문 조회를 대조한다. 기존 결과 파일을 덮어쓰지 않는다. 신규/수정 문서를 실제 공식 색인에 자동 반영하지 않는다.
