"""Source metadata presentation, not a decision about legal applicability."""
from collections import OrderedDict
import re

PRESENTATION_VERSION = "1.0"
LABELS = OrderedDict([
    ("domestic_law", "국내 법령 원문"),
    ("domestic_guidance", "국내 공식 안내"),
    ("domestic_internal", "국내 내부 지침"),
    ("foreign_reference", "해외 참고 사례"),
    ("other_reference", "기타 참고 자료"),
    ("synthetic_fixture", "합성 테스트 자료"),
])


def source_display(row):
    kind, jurisdiction = row.get("sourceKind"), row.get("jurisdiction")
    if kind == "synthetic_fixture":
        category = kind
    elif jurisdiction != "KR":
        category = "foreign_reference"
    else:
        category = {"law": "domestic_law", "official_guidance": "domestic_guidance",
                    "bank_internal_policy": "domestic_internal"}.get(kind, "other_reference")
    notice = {
        "domestic_law": "선별한 법령 조문 원문입니다. 개별 거래 적용과 유효 시점은 별도 검토가 필요합니다.",
        "domestic_guidance": "국내 기관의 공식 안내입니다. 법령 조문 자체나 은행 내규로 대체하지 않습니다.",
        "domestic_internal": "제공된 내부 지침입니다. 허용된 조직·버전·적용 범위에서 검토합니다.",
        "foreign_reference": "해외 징후·유형 참고 자료입니다. 한국의 보고 의무·금액 기준으로 적용하지 않습니다.",
        "other_reference": "참고 자료입니다. 발행기관·관할·적용 범위를 확인합니다.",
        "synthetic_fixture": "TraceOn 검증용으로 만든 문서입니다. 실제 법령이나 기관 안내의 근거로 사용하지 않습니다.",
    }[category]
    return {"category": category, "label": LABELS[category], "jurisdiction": jurisdiction,
            "sourceKind": kind, "effectiveDate": row.get("effectiveDate"),
            "version": row.get("version"), "classificationBasis": "registered_source_metadata",
            "legalApplicabilityVerified": False, "notice": notice}


def source_groups(rows):
    result = []
    for category, label in LABELS.items():
        ids = [r["chunkId"] for r in rows if source_display(r)["category"] == category]
        if ids:
            result.append({"category": category, "label": label, "chunkIds": ids})
    return result


def law_selector(query):
    """Only explicit article references restrict the selected law sections."""
    articles = {"제" + number + "조" + ("의" + branch if branch else "")
                for number, branch in re.findall(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?", query)}
    level = "decree" if re.search(r"시행령|대통령령", query) else ("act" if re.search(r"(?<!시행)법률", query) else None)
    return articles, level


def named_statutes(query):
    names = re.findall(r"(?<![가-힣])([가-힣]{2,40}법)(?=의|에서|은|을|\s|제|$|[?.,])", query)
    return [name for name in names if not name.endswith(("방법", "수법", "기법", "문법", "해법"))]


def matches_law_selector(row, articles, level, names=()):
    found = re.match(r"(제\d+조(?:의\d+)?)\(", row["section"])
    if articles and (not found or found.group(1) not in articles):
        return False
    version = row.get("version", "")
    if level == "decree" and not version.startswith("대통령령"):
        return False
    if level == "act" and not version.startswith("법률"):
        return False
    title = re.sub(r"\s+", "", row["title"])
    aliases = {"특정금융정보법": "특정금융거래정보의보고및이용등에관한법률", "특금법": "특정금융거래정보의보고및이용등에관한법률"}
    if names and not all(aliases.get(name, name) in title for name in names):
        return False
    return True
