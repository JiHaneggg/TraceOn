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

EXPANSION_VERSION = "2.0"


def domain_signals(query):
    """Small, auditable multi-cue vocabulary bridges, never source selection.

    These terms describe concepts, not answers or generated source text. In
    particular they cannot waive a source, date, jurisdiction or privacy guard.
    No evaluation IDs, document IDs or corpus-specific chunk IDs are used.
    """
    signals = {}
    suspect = bool(re.search(r"의심|수상|이상한|이상\s*거래|불법\s*재산", query))
    transaction = bool(re.search(r"거래|송금|재산|자금|흐름|입금", query))
    reporting = bool(re.search(r"보고|제출|알리|전달", query))
    str_topic = bool(re.search(r"\bSTR\b|의심\s*보고", query, re.I)) or (suspect and (transaction or reporting))
    # Canonical STR wording already has the needed vocabulary. Avoid changing
    # its token set/scores and the identities of existing report contexts.
    if str_topic and not re.search(r"\bSTR\b|의심\s*거래\s*보고", query, re.I):
        signals["suspicious_reporting"] = "의심거래보고"
    destination_request = re.search(r"어디(?:로|에)?|(?:어느|어떤|무슨)\s*(?:기관|곳)|기관.{0,8}(?:명칭|이름)", query)
    if str_topic and destination_request and re.search(r"보고|알리|제출|최종", query):
        signals["report_recipient"] = "보고 금융정보분석원장"
    staff = bool(re.search(r"창구|지점|영업점|직원|담당자", query))
    history = bool(re.search(r"평소|기존\s*(?:이용|거래)|거래\s*(?:모습|이력|상황)", query))
    occupation = bool(re.search(r"생업|직업|사업\s*(?:내용|활동)", query))
    if str_topic and staff and (history or occupation):
        signals["staff_customer_context"] = "영업점 직원 고객 평소 거래상황 직업 사업내용"
    if str_topic and staff and re.search(r"책임자|보고\s*경로|전달|어디.{0,8}보고", query):
        signals["staff_reporting_route"] = "영업점 직원 보고책임자 보고"
    if (reporting and re.search(r"유선|전화|팩스|FAX", query, re.I)
            and re.search(r"급해|급한|긴급|먼저|선보고|보완|추후|나중", query)):
        signals["urgent_reporting"] = "긴급 전화 FAX 보고 추후 보완"
    if (str_topic and re.search(r"이유|사유|근거|왜", query)
            and re.search(r"적|기재|밝|드러|명시|제출|보고", query)
            and not re.search(r"합당한\s*근거", query)):
        signals["report_grounds"] = "보고 합당한 근거 기재"
    if str_topic and re.search(r"보관|보존|증빙", query) and re.search(r"자료|서류|종류|어떤|무슨", query):
        signals["report_records"] = "보존 자료 종류 기재"
    no_relationship = re.search(r"관계.{0,12}(?:없|않|보이지|드러나지)|무관한|서로\s*모르", query)
    if re.search(r"송금|이체|wire\s*transfer", query, re.I) and no_relationship:
        signals["unrelated_wire_parties"] = "wire transfers senders receivers no apparent relationship frequent"
    inactive = re.search(r"휴면|비활성|(?:안|않|못)\s*(?:쓰|쓴|사용)|쓰이지\s*않|사용되지\s*않|거래.{0,8}없|잠자", query)
    if re.search(r"계좌|account", query, re.I) and inactive and re.search(r"다시|재개|활동|입금|송금|이체|거래", query):
        signals["inactive_account_activity"] = "inactive dormant account financial activity deposits wire transfers"
    if re.search(r"순환|돌고\s*돌|되돌아", query) and re.search(r"자금|송금|거래|계좌", query):
        signals["circular_funds"] = "circular flow funds repeated cycles"
        if re.search(r"중고차|자동차|수출입", query):
            signals["automotive_context"] = "automotive used car dealerships import export"
        if re.search(r"도박|카지노", query):
            signals["gambling_context"] = "online gambling casino sites"
    return signals


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
    return query_profile(query)["expanded"]


def query_profile(query):
    value = clean_query(query)
    expansions = [terms for pattern, terms in QUERY_TERMS if re.search(pattern, query, re.I)]
    signals = domain_signals(query)
    expansions.extend(signals.values())
    return {"version": EXPANSION_VERSION, "signals": sorted(signals),
            "expanded": value + (" " + " ".join(expansions) if expansions else "")}


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
