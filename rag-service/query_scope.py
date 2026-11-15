"""Versioned, bounded request-scope checks before lexical retrieval.

No claim of general intent recognition. Metadata can establish a requested source
scope; mentions in another document's body cannot substitute for that source.
"""
import re

VERSION = '1.0'
GENERAL_PERSON_WORDS = {'어떤','어느','모든','해당','고위험','개인','개인인','일반','일반적인','신규','기존','법인','영리법인','비영리법인','법인인','비영리','외국인','그','이','각','특정','우리'}


def assess_scope(query, rows):
    allowed = {row['chunkId'] for row in rows}
    requirements = []
    override_kind, override = None, False

    def require(kind, target, matches, code, message):
        nonlocal allowed
        ids = {row['chunkId'] for row in matches}
        requirements.append({'kind':kind,'requested':target,'available':bool(ids),
                             'reasonCode':None if ids else code,'message':None if ids else message})
        allowed &= ids

    def unavailable(kind, code, message):
        require(kind, kind, [], code, message)

    bank = re.search(r'KB\s*국민은행|국민은행|\bKB\b|신한은행|하나은행|우리은행|농협은행', query, re.I)
    internal = re.search(r'내규|(?:내부|사내).{0,18}(?:지침|매뉴얼|규정|정책|편람|승인|결재)|비공개.{0,12}(?:매뉴얼|문서|기준)', query, re.I)
    public_obligation = (re.search(r'작성|수립|마련|운영|작성해야|만들어야',query)
                         and re.search(r'법률|법령|일반.{0,8}의무',query)
                         and not re.search(r'비공개|실제|기한|완료.{0,8}시간|담당자.{0,8}이름|승인자|결재자|가중치',query))
    if internal and not public_obligation:
        private_docs = [r for r in rows if r['sourceKind']=='bank_internal_policy' and r['jurisdiction']=='KR']
        if bank:
            name=bank.group(0)
            aliases=['국민은행','kb'] if re.search(r'KB|국민은행',name,re.I) else [name]
            private_docs=[r for r in private_docs if any(a.casefold() in (r.get('publisher','')+' '+r['title']).casefold() for a in aliases)]
        require('bank_internal_policy', bank.group(0) if bank else '내부 지침/내규', private_docs,
                'internal_policy_unavailable','요청한 은행의 내부 지침·승인자·처리 기한 자료를 확보하지 못했습니다.')
        override_kind, override = 'bank_internal_policy', True
    elif internal and public_obligation:
        # An obligation to maintain procedures is not a request for a bank manual.
        require('public_obligation','law',[r for r in rows if r['sourceKind']=='law'],
                'requested_source_kind_unavailable','내부 절차 마련 의무에 대한 법령 원문이 요청 범위에 없습니다.')
        override_kind, override = 'law', True

    source_terms = re.search(r'원문|원본|그대로|조문|조항|권고.{0,8}(?:제\s*)?\d+|recommendation\s*\d+',query,re.I)
    secondary = re.search(r'국내.{0,15}(?:소개|언급|안내)|(?:소개|언급).{0,15}국내|FinCEN.{0,15}(?:소개|언급|안내)',query,re.I)
    original = re.search(r'원문|원본|그대로|직접\s*인용',query)
    if re.search(r'\bFATF\b|국제자금세탁방지기구',query,re.I) and source_terms and (not secondary or original):
        fatf=[r for r in rows if re.search(r'\bFATF\b|Financial Action Task Force',r.get('publisher','')+' '+r['title'],re.I)]
        require('publisher','FATF',fatf,'requested_publisher_unavailable',
                'FATF의 요청 원문을 확보하지 못했습니다. 국내 안내나 다른 해외 기관 자료로 대신 인용하지 않습니다.')
        number=re.search(r'(?:권고|recommendation[s]?)[\s]*(?:제\s*)?(\d+)',query,re.I)
        if number and fatf:
            n=number.group(1)
            selected=[r for r in fatf if re.search(r'(?:권고|recommendation[s]?|rec\.?|r\.)\s*(?:제\s*)?'+re.escape(n)+r'(?!\d)',r['section']+' '+r['title'],re.I)]
            require('document_section','FATF Recommendation '+n,selected,'requested_section_unavailable',
                    '요청한 FATF 권고 번호의 원문 구간이 검색 대상에 없습니다.')
        override_kind, override = None, True

    if (re.search(r'\bEU\b|유럽연합|European Union',query,re.I)
            and re.search(r'규정|법률|법령|조문|조항|지침|regulation|directive',query,re.I)
            and (not secondary or original)):
        eu=[r for r in rows if r['jurisdiction']=='EU' and r['sourceKind']=='law']
        require('jurisdiction','EU statutory text',eu,'requested_jurisdiction_unavailable',
                'EU 규정 원문을 확보하지 못했습니다. 한국·미국·캐나다 자료로 적용 조항을 대신하지 않습니다.')
        override_kind, override = 'law', True

    field = re.search(r'직업|주소|이름|성명|실[제질]\s*소유자|실소유자|계좌주|수익자|연락처|주민등록|자금\s*원천|어떤\s*사업',query)
    request = re.search(r'알려|알아(?:봐|보)|찾아|파악|누구|무엇|몇|어떻게|밝혀|설명|추정|추론|확인해',query)
    identifier = re.search(r'(?<![A-Za-z0-9])(?:TX|AC)-[0-9A-Z-]+(?![A-Za-z0-9-])|(?<![A-Za-z0-9])\d{2,}:[0-9A-Z]+(?![A-Za-z0-9])',query,re.I)
    named=[name for name in re.findall(r'([가-힣]{2,4})\s*(?:고객|씨|님)',query) if name not in GENERAL_PERSON_WORDS]
    selected_case = re.search(r'(?:화면|선택|고른|이번).{0,18}(?:사례|계좌|고객|거래)|이\s*(?:계좌|사례|거래|고객)',query)
    generic_process = re.search(r'(?:확인|요구|수집|기재).{0,10}(?:항목|절차|서류)|필요한.{0,8}(?:정보|자료|서류)|일반적인.{0,12}(?:항목|절차)',query)
    actual_value = re.search(r'누구(?!인지\s*확인)|(?:이름|성명|직업|연락처|주소)(?:이|가|을|를)?\s*(?:알려|알아봐|추정|무엇)|추론|추정',query)
    if field and request and (identifier or named or selected_case) and not (generic_process and not actual_value):
        unavailable('case_specific_customer_data','customer_facts_unavailable',
                    '특정 사례의 고객 원장·실소유자·직업·자금 원천 자료가 없습니다. 일반 고객확인 항목으로 실제 값을 답하지 않습니다.')

    score = re.search(r'점수|스코어|(?:룰|모델|ML|risk).{0,14}(?:확률|score|probability)',query,re.I)
    if score and re.search(r'의무|무조건|자동|신고|보고|확정|기준|임계',query):
        unavailable('score_reporting_policy','score_decision_policy_unavailable',
                    '룰/ML 점수를 자동 신고·보고 의무로 연결하는 승인된 정책이 없습니다.')
    crime = re.search(r'자금세탁|범죄|보고\s*의무|신고\s*의무',query)
    assertion = re.search(r'확정|단정|판정|결론',query)
    instruction = re.search(r'만들어|작성해|확정해|단정해|결론\s*내|판정해|처리해|답해',query)
    caution = re.search(r'(?:확정|단정|판정).{0,10}(?:하면\s*안|하지\s*말|할\s*수\s*없는)|(?:왜|이유|한계|위험성).{0,20}(?:설명|알려)',query)
    if crime and assertion and instruction and not caution:
        unavailable('automated_decision','automatic_decision_not_supported',
                    '사람의 검토 없이 범죄·보고 의무를 확정하는 문장을 생성하지 않습니다. 추가 사실과 검토가 필요합니다.')

    missing=[r for r in requirements if not r['available']]
    decision='abstain' if missing else 'scope_supported'
    assessment={'policyVersion':VERSION,'decision':decision,'requirements':requirements,
                'reasonCodes':[r['reasonCode'] for r in missing],
                'message':' '.join(dict.fromkeys(r['message'] for r in missing)) if missing else None,
                'wholeRequestAbstained':bool(missing),'generalAnswerabilityVerified':False,
                'classificationBasis':'bounded_request_patterns_and_registered_source_metadata'}
    return {'assessment':assessment,'allowedChunkIds':allowed,'overrideRequiredKind':override,
            'requiredKind':override_kind,
            'genericCustomerProcess':bool(field and request and generic_process and not actual_value)}
