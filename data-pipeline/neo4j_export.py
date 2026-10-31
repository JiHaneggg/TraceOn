"""팀 공통 데이터(data/processed)를 neo4j-admin database import용 CSV로 변환한다.

사용법:
    python neo4j_export.py --dataset HI-Small

입력: data/processed/{dataset}/accounts.csv, transactions.csv
출력: data/import/{dataset}/accounts.csv, transfers.csv  (헤더에 Neo4j 타입 표기 포함)

그래프 모델 (ADR-006):
    (:Account {accountKey})-[:TRANSFER {txId, timestamp, ...}]->(:Account)
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# neo4j-admin import 헤더: 이름:타입. ID 공간 이름은 Account.
ACCOUNT_HEADER = ["accountKey:ID(Account)", "bankId:int", "accountNumber", "bankName",
                  "entityId", "entityName", ":LABEL"]
TRANSFER_HEADER = ["txId", "timestamp:localdatetime", ":START_ID(Account)", ":END_ID(Account)",
                   "amountPaid:double", "paymentCurrency", "amountReceived:double", "receivingCurrency",
                   "amountUsd:double", "paymentFormat", "isSelfLoop:boolean", "isLaundering:boolean",
                   "patternId", "patternType", ":TYPE"]


def to_bool(flag: str) -> str:
    # neo4j-admin import는 'true'만 참으로 읽는다 ('1'은 거짓이 됨)
    if flag not in ("0", "1"):
        raise ValueError(f"0/1이 아닌 값: {flag!r}")
    return "true" if flag == "1" else "false"


def export_accounts(src: Path, dst: Path) -> int:
    n = 0
    with src.open(newline="", encoding="utf-8") as fin, dst.open("w", newline="", encoding="utf-8") as fout:
        writer = csv.writer(fout)
        writer.writerow(ACCOUNT_HEADER)
        for row in csv.DictReader(fin):
            writer.writerow([row["accountKey"], row["bankId"], row["accountNumber"], row["bankName"],
                             row["entityId"], row["entityName"], "Account"])
            n += 1
    return n


def export_transfers(src: Path, dst: Path) -> int:
    n = 0
    with src.open(newline="", encoding="utf-8") as fin, dst.open("w", newline="", encoding="utf-8") as fout:
        writer = csv.writer(fout)
        writer.writerow(TRANSFER_HEADER)
        for row in csv.DictReader(fin):
            # 패턴이 없는 거래는 빈 칸 -> import 시 속성이 생성되지 않는다 (null)
            writer.writerow([row["txId"], row["timestamp"], row["fromAccountKey"], row["toAccountKey"],
                             row["amountPaid"], row["paymentCurrency"], row["amountReceived"],
                             row["receivingCurrency"], row["amountUsd"], row["paymentFormat"],
                             to_bool(row["isSelfLoop"]), to_bool(row["isLaundering"]),
                             row["patternId"], row["patternType"], "TRANSFER"])
            n += 1
    return n


def run(processed_dir: Path, import_dir: Path) -> tuple[int, int]:
    for name in ("accounts.csv", "transactions.csv"):
        if not (processed_dir / name).is_file():
            raise FileNotFoundError(f"{processed_dir / name} 없음. preprocess.py를 먼저 실행하세요.")
    import_dir.mkdir(parents=True, exist_ok=True)
    accounts = export_accounts(processed_dir / "accounts.csv", import_dir / "accounts.csv")
    transfers = export_transfers(processed_dir / "transactions.csv", import_dir / "transfers.csv")
    return accounts, transfers


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="공통 데이터를 Neo4j import용 CSV로 변환")
    parser.add_argument("--dataset", default="HI-Small")
    parser.add_argument("--processed", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "import")
    args = parser.parse_args(argv)
    accounts, transfers = run(args.processed / args.dataset, args.output / args.dataset)
    print(f"Account {accounts:,}개, TRANSFER {transfers:,}개 -> {args.output / args.dataset}", file=sys.stderr)


if __name__ == "__main__":
    main()
