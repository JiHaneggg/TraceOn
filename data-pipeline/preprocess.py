"""IBM AML 원천 데이터를 TraceOn 팀 공통 데이터로 변환한다.

사용법:
    python preprocess.py --input <원천 폴더> --dataset HI-Small --output ../data/processed

입력: {dataset}_Trans.csv, {dataset}_Patterns.txt, {dataset}_accounts.csv
출력: {output}/{dataset}/ 아래
    transactions.csv  거래 1건 = 1행. txId, 정규화 계좌키, USD 환산 금액, 라벨, 패턴 유형
    accounts.csv      거래에 등장한 계좌. 은행명·소유 개체 정보 포함
    patterns.csv      세탁 패턴 묶음 목록
    fx_rates.csv      통화별 환율 (1 USD당 해당 통화)
    manifest.json     입력 파일 해시, 행 수, 처리 규칙 버전 (재현성 확인용)

표준 라이브러리만 사용하며, Trans.csv를 두 번 순차로 읽는다 (메모리를 적게 쓰기 위함).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PIPELINE_VERSION = "1.0.0"
USD = "US Dollar"

# Trans.csv 컬럼 순서 (헤더에 'Account'가 두 번 나와 이름 대신 위치로 읽는다)
TS, FROM_BANK, FROM_ACCT, TO_BANK, TO_ACCT, AMT_RECV, CUR_RECV, AMT_PAID, CUR_PAID, PAY_FORMAT, IS_LAUNDERING = range(11)
EXPECTED_HEADER = ["Timestamp", "From Bank", "Account", "To Bank", "Account", "Amount Received",
                   "Receiving Currency", "Amount Paid", "Payment Currency", "Payment Format", "Is Laundering"]

TX_FIELDS = ["txId", "timestamp", "fromAccountKey", "toAccountKey", "amountPaid", "paymentCurrency",
             "amountReceived", "receivingCurrency", "amountUsd", "paymentFormat", "isSelfLoop",
             "isLaundering", "patternId", "patternType"]
ACCOUNT_FIELDS = ["accountKey", "bankId", "accountNumber", "bankName", "entityId", "entityName"]


# ---------------------------------------------------------------- 정규화 규칙

def normalize_bank_id(raw: str) -> int:
    """Trans.csv의 은행 ID는 0 패딩이 일정하지 않다('014' == '0014'). 정수로 정규화한다."""
    return int(raw)


def account_key(bank_raw: str, account_number: str) -> str:
    """계좌 식별자 = (정규화 은행 ID, 계좌번호). 같은 계좌번호가 다른 은행에도 존재한다."""
    return f"{normalize_bank_id(bank_raw)}:{account_number}"


def to_iso_timestamp(raw: str) -> str:
    """'2022/09/01 00:20' -> '2022-09-01T00:20:00' (시간대 없는 LocalDateTime)."""
    return datetime.strptime(raw, "%Y/%m/%d %H:%M").strftime("%Y-%m-%dT%H:%M:%S")


def tx_id(row_number: int) -> str:
    """Trans.csv의 데이터 행 번호(1부터) 기반 고정 ID. 같은 내용의 행도 서로 다른 거래로 본다."""
    return f"TX-{row_number:09d}"


# ---------------------------------------------------------------- 패턴 파일

@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    pattern_type: str
    detail: str
    tx_count: int


def parse_patterns(lines) -> tuple[list[Pattern], dict[tuple[str, ...], str]]:
    """BEGIN/END LAUNDERING ATTEMPT 블록을 읽는다.

    반환: (패턴 목록, {Trans.csv 행 내용 튜플: patternId})
    """
    patterns: list[Pattern] = []
    row_to_pattern: dict[tuple[str, ...], str] = {}
    current: dict | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("BEGIN LAUNDERING ATTEMPT"):
            body = line.split(" - ", 1)[1]
            ptype, _, detail = body.partition(":")
            current = {"id": f"P-{len(patterns) + 1:05d}", "type": ptype.strip(), "detail": detail.strip(), "count": 0}
        elif line.startswith("END LAUNDERING ATTEMPT"):
            if current is None:
                raise ValueError("BEGIN 없이 END가 나왔습니다.")
            patterns.append(Pattern(current["id"], current["type"], current["detail"], current["count"]))
            current = None
        else:
            if current is None:
                raise ValueError(f"패턴 블록 밖의 거래 행: {line}")
            key = tuple(line.split(","))
            if key in row_to_pattern:
                raise ValueError(f"한 거래가 여러 패턴에 속합니다: {line}")
            row_to_pattern[key] = current["id"]
            current["count"] += 1
    if current is not None:
        raise ValueError("마지막 패턴 블록이 END로 닫히지 않았습니다.")
    return patterns, row_to_pattern


# ---------------------------------------------------------------- 환율

def compute_fx_rates(rows) -> dict[str, tuple[float, int]]:
    """데이터 내부의 USD 환전 거래로 통화별 환율(1 USD당 통화량) 중앙값을 구한다.

    반환: {통화: (환율, 계산에 사용한 거래 수)}. USD는 (1.0, 0).
    """
    samples: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        paid, recv = float(row[AMT_PAID]), float(row[AMT_RECV])
        if paid <= 0 or recv <= 0:
            continue
        if row[CUR_PAID] == USD and row[CUR_RECV] != USD:
            samples[row[CUR_RECV]].append(recv / paid)
        elif row[CUR_RECV] == USD and row[CUR_PAID] != USD:
            samples[row[CUR_PAID]].append(paid / recv)
    rates = {cur: (statistics.median(v), len(v)) for cur, v in samples.items()}
    rates[USD] = (1.0, 0)
    return rates


def to_usd(amount: str, currency: str, rates: dict[str, tuple[float, int]]) -> str:
    if currency not in rates:
        raise ValueError(f"환율을 계산할 수 없는 통화입니다: {currency}")
    return f"{float(amount) / rates[currency][0]:.2f}"


# ---------------------------------------------------------------- 파일 처리

def read_trans_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        if header != EXPECTED_HEADER:
            raise ValueError(f"Trans.csv 헤더가 예상과 다릅니다: {header}")
        yield from reader


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_account_info(path: Path) -> dict[str, tuple[str, str, str]]:
    """accounts.csv -> {accountKey: (bankName, entityId, entityName)}"""
    info = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = account_key(row["Bank ID"], row["Account Number"])
            info[key] = (row["Bank Name"], row["Entity ID"], row["Entity Name"])
    return info


def run(input_dir: Path, dataset: str, output_root: Path) -> dict:
    started = time.time()
    trans_path = input_dir / f"{dataset}_Trans.csv"
    patterns_path = input_dir / f"{dataset}_Patterns.txt"
    accounts_path = input_dir / f"{dataset}_accounts.csv"
    for p in (trans_path, patterns_path, accounts_path):
        if not p.is_file():
            raise FileNotFoundError(p)
    out = output_root / dataset
    out.mkdir(parents=True, exist_ok=True)

    with patterns_path.open(encoding="utf-8") as f:
        patterns, row_to_pattern = parse_patterns(f)
    pattern_type = {p.pattern_id: p.pattern_type for p in patterns}

    print("[1/3] 환율 계산", file=sys.stderr)
    rates = compute_fx_rates(read_trans_rows(trans_path))

    print("[2/3] 거래 변환", file=sys.stderr)
    stats = {"transactions": 0, "laundering": 0, "selfLoops": 0, "patternMatched": 0}
    accounts_seen: set[str] = set()
    with (out / "transactions.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(TX_FIELDS)
        for n, row in enumerate(read_trans_rows(trans_path), start=1):
            src, dst = account_key(row[FROM_BANK], row[FROM_ACCT]), account_key(row[TO_BANK], row[TO_ACCT])
            pid = row_to_pattern.get(tuple(row), "")
            laundering = row[IS_LAUNDERING] == "1"
            if pid and not laundering:
                raise ValueError(f"패턴 거래인데 라벨이 0입니다: {row}")
            self_loop = src == dst
            writer.writerow([tx_id(n), to_iso_timestamp(row[TS]), src, dst,
                             row[AMT_PAID], row[CUR_PAID], row[AMT_RECV], row[CUR_RECV],
                             to_usd(row[AMT_PAID], row[CUR_PAID], rates), row[PAY_FORMAT],
                             int(self_loop), int(laundering), pid, pattern_type.get(pid, "")])
            accounts_seen.add(src)
            accounts_seen.add(dst)
            stats["transactions"] += 1
            stats["laundering"] += laundering
            stats["selfLoops"] += self_loop
            stats["patternMatched"] += bool(pid)
    if stats["patternMatched"] != len(row_to_pattern):
        # 누락(패턴 거래가 Trans.csv에 없음)과 중복(같은 내용의 행이 여러 개라 패턴이 둘 이상에 붙음)을 모두 막는다
        raise ValueError(f"패턴 거래 {len(row_to_pattern)}건과 매칭된 Trans.csv 행 {stats['patternMatched']}건이 다릅니다.")

    print("[3/3] 계좌·패턴·환율·manifest 저장", file=sys.stderr)
    account_info = load_account_info(accounts_path)
    missing_info = 0
    with (out / "accounts.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(ACCOUNT_FIELDS)
        for key in sorted(accounts_seen):
            bank, number = key.split(":", 1)
            bank_name, entity_id, entity_name = account_info.get(key, ("", "", ""))
            missing_info += key not in account_info
            writer.writerow([key, bank, number, bank_name, entity_id, entity_name])

    with (out / "patterns.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["patternId", "patternType", "detail", "txCount"])
        for p in patterns:
            writer.writerow([p.pattern_id, p.pattern_type, p.detail, p.tx_count])

    with (out / "fx_rates.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["currency", "unitsPerUsd", "sampleCount"])
        for cur, (rate, count) in sorted(rates.items()):
            writer.writerow([cur, f"{rate:.8g}", count])

    manifest = {
        "dataset": dataset,
        "pipelineVersion": PIPELINE_VERSION,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "inputs": {p.name: {"sha256": sha256_of(p), "bytes": p.stat().st_size}
                   for p in (trans_path, patterns_path, accounts_path)},
        "counts": {**stats, "accounts": len(accounts_seen), "accountsWithoutInfo": missing_info,
                   "patterns": len(patterns)},
        "fxRatesUnitsPerUsd": {cur: round(rate, 8) for cur, (rate, _) in sorted(rates.items())},
        "rules": {
            "txId": "Trans.csv 데이터 행 번호(1부터), TX-%09d",
            "accountKey": "int(은행 ID):계좌번호",
            "timestamp": "YYYY-MM-DDTHH:MM:SS, 시간대 없음",
            "amountUsd": "amountPaid / unitsPerUsd[paymentCurrency], 소수 2자리",
            "fxRate": "데이터 내 USD 환전 거래의 (통화량/USD) 중앙값",
        },
        "elapsedSeconds": round(time.time() - started, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="IBM AML 데이터를 TraceOn 공통 데이터로 변환")
    parser.add_argument("--input", required=True, type=Path, help="원천 CSV가 있는 폴더")
    parser.add_argument("--dataset", default="HI-Small", help="예: HI-Small, LI-Small, HI-Medium")
    parser.add_argument("--output", default=Path(__file__).resolve().parent.parent / "data" / "processed", type=Path)
    args = parser.parse_args(argv)
    manifest = run(args.input, args.dataset, args.output)
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))
    print(f"완료: {args.output / args.dataset} ({manifest['elapsedSeconds']}초)")


if __name__ == "__main__":
    main()
