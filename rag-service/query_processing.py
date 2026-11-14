"""Bounded lexical query normalization; no generated source facts or law text.

Curated bilingual terms cover concepts present in the existing official corpus.
Scope guards identify known missing source types, not general answerability.
"""
import re

QUESTION_WORDS = {"어떤", "어디", "어떻게", "무엇", "왜", "얼마", "몇", "경우", "하는", "되는",
                  "있으면", "있나요", "있습니까", "있을까요", "인가요", "하나요", "알려주세요", "알려줘",
                  "알려", "찾아주세요", "보여주세요", "알아보세요", "함께", "대해", "대한", "통해", "있다"}
ENGLISH_STOP = {"a", "an", "the", "is", "are", "be", "to", "of", "and", "or", "in", "on", "at",
                "with", "from", "for", "by", "as", "does", "do", "it", "that", "this"}
QUERY_TERMS = [
    (r"집금", "funnel account multiple deposits"),
    (r"분산|다수.?수취인", "multiple beneficiaries wire transfers"),
    (r"쪼개기|분할.?거래", "structuring reporting threshold"),
    (r"신속|짧은.?시간", "rapid movement funds little time elapsing"),
    (r"휴면|장기간.?거래가.?없|비활성.?계좌", "inactive dormant account activity"),
    (r"단일.?징후|징후.?하나|한.?가지.?징후", "single indicator one activity"),
    (r"사실과.?맥락|사실.?맥락", "facts context assessment"),
    (r"관계없는|관계가.?없는|무관한", "unrelated"),
    (r"수취인|받는.?사람", "recipient beneficiary"),
    (r"전자.?송금", "wire transfers"),
    (r"(?<![a-z])single(?![a-z])", "one activity"),
    (r"(?<![a-z])context(?![a-z])", "circumstances"),
]


def english_term(word):
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def clean_query(query):
    value = query.lower()
    for word in sorted(QUESTION_WORDS, key=len, reverse=True):
        value = re.sub(r"(?<![가-힣])" + re.escape(word) + r"(?![가-힣])", " ", value)
    # Strip only common request/question suffixes. Source text is never changed.
    value = re.sub(r"(인가요|하나요|되나요|알려주세요|찾아주세요|알려줘)(?=\s|[?.!]|$)", "", value)
    return value


def expand_query(query):
    value = clean_query(query)
    expansions = [terms for pattern, terms in QUERY_TERMS if re.search(pattern, query, re.I)]
    return value + (" " + " ".join(expansions) if expansions else "")


def missing_capability(query):
    # A long internal-policy request must not evade the older 16-character guard.
    if "내부" in query and re.search(r"규정|점수|스코어|룰|승인.{0,8}기준", query):
        return "bank_internal_policy"
    if re.search(r"금리|이자율|수수료율|대출.{0,16}할인", query):
        return "product_pricing"
    field = re.search(r"직업|주소|이름|실제\s*소유자|계좌주|연락처|주민등록", query)
    request = re.search(r"알려|누구|무엇|몇|어떻게.?되", query)
    identifier = re.search(r"\b(?:TX|AC)-[0-9A-Z-]+\b|\b\d{2,}:[0-9A-Z]+\b", query, re.I)
    general = {"어떤", "어느", "모든", "해당", "고위험", "개인", "일반", "신규", "기존", "법인", "비영리", "외국인", "그", "이", "각", "특정"}
    named = [name for name in re.findall(r"([가-힣]{2,4})\s*(?:고객|씨|님)", query) if name not in general]
    if field and request and (identifier or named):
        return "case_specific_customer_data"
    return None
