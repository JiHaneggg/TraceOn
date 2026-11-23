"""Bounded evidence checks with synthetic sources; no legal accuracy claims."""
import copy
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent/'data-pipeline')]
import test_domestic_sources as fixtures
from corpus import canonical, digest, atomic_json
from corpus_manager import CorpusManager
from evidence import build_context
from evidence_fit import MIN_CONDITIONS, policy_digest, validate_policy, assessment_response
from evidence_lookup import EvidenceRepository
from retrieval import Index
from reports import generate_report, default_plan, render_report, validate_plan
from report_verification import verify_report
from agent_tools import AgentTools
from rag_server import handler_for
from make_demo import generate


class EvidenceFitTest(unittest.TestCase):
    def setUp(self):
        fixtures.DomesticSourcesTest.setUp(self)
        self.old = self.index
        self.manager = CorpusManager(self.path)
        self.manager.init(self.sources)
        refs = {}
        for key,doc,section,quote in [('str','act','제4조(', '합당한 근거'),
                                      ('cdd','act','제5조의2(', '실제 소유자'),
                                      ('funnel','foreign','Funnel indicators','funnel account multiple deposits'),
                                      ('wire','foreign','Funnel indicators','unrelated accounts')]:
            row = next(r for r in self.index.chunks if r['documentId']==doc and r['section'].startswith(section) and quote in r['text'])
            refs[key] = {k:row[k] for k in ('chunkId','documentId','section','sourceSha256','textSha256','jurisdiction','sourceKind')}
            refs[key].update(quote=quote,quoteSha256=digest(quote))
        self.policy = {'schemaVersion':'1.0','policyId':'traceon-evidence-fit-v1',
                       'reviewStatus':'developer_curated_pending_domain_review','references':refs,
                       'rules':{kind:{'roles':{'reporting_context':['str'],'due_diligence_context':['cdd'],
                           'pattern_reference':{'CYCLE':[],'FAN_IN':['funnel'],'FAN_OUT':['wire']}[kind]},
                           'conditions':sorted(conditions)} for kind,conditions in MIN_CONDITIONS.items()}}
        self.policy['policySha256'] = policy_digest(self.policy)
        self.manager.apply_evidence_policy(self.policy,self.index.corpus_hash)
        self.index = Index(self.path)
        self.repo = EvidenceRepository(self.index)

    def graph(self, kind='fan_in'):
        return next(c['graph'] for c in generate()[1] if c['scenario']==kind)

    def context(self,kind='fan_in',**kwargs):
        return build_context(self.index,self.graph(kind),**kwargs)

    def report(self,kind='fan_in'):
        return generate_report(self.context(kind))

    @staticmethod
    def rehash(report):
        report['reportId']='RPT-'+digest(canonical({k:v for k,v in report.items() if k!='reportId'}))[:24]
        return report

    def test_policy_publication_preserves_chunks_and_old_report(self):
        self.assertEqual(self.index.chunks,self.old.chunks)
        self.assertNotEqual(self.index.corpus_hash,self.old.corpus_hash)
        report=generate_report(build_context(self.old,self.graph()))
        self.assertNotIn('evidenceFitness',report)
        self.assertTrue(verify_report(self.repo,self.graph(),None,report)['valid'])
        old_context=build_context(self.old,self.graph())
        self.assertEqual(old_context['contextVersion'],'evidence-kr-v2')
        self.assertEqual(assessment_response(old_context)['status'],'not_configured')

    def test_reapplying_policy_is_idempotent(self):
        before=self.path.read_bytes()
        result=self.manager.apply_evidence_policy(self.policy,self.index.corpus_hash)
        self.assertEqual(result['status'],'no_change')
        self.assertEqual(self.path.read_bytes(),before)

    def test_new_source_revision_keeps_old_fitness_report_and_fact_links(self):
        context=self.context();report=generate_report(context)
        source=copy.deepcopy(next(s for s in self.sources if s['documentId']=='foreign'))
        source['version']='updated-synthetic-source'
        registered=self.manager.register(source,(self.raw/'foreign.html').read_bytes())
        self.manager.publish('foreign',registered['revisionId'],self.index.corpus_hash)
        current=Index(self.path)
        changed=build_context(current,self.graph())['evidenceFitness']
        finding=next(f for f in changed['findings'] if f['ruleId']=='FAN_IN')
        self.assertEqual(next(r for r in finding['referenceChecks'] if r['role']=='pattern_reference')['status'],'reference_missing')
        self.assertTrue(verify_report(EvidenceRepository(current),self.graph(),None,report)['valid'])
        old_fact=AgentTools(current).call('resolve_report_fact',{'graph':self.graph(),'factId':'target.amountPaid',
                         'contextId':context['contextId'],'corpusSha256':self.index.corpus_hash})
        self.assertEqual(old_fact['contextId'],context['contextId'])

    def test_general_str_reference_does_not_fill_cycle_pattern_gap(self):
        fit=self.context('cycle')['evidenceFitness']
        finding=next(f for f in fit['findings'] if f['ruleId']=='CYCLE')
        roles={r['role']:r['status'] for r in finding['referenceChecks']}
        self.assertEqual(roles['reporting_context'],'reference_available')
        self.assertEqual(roles['pattern_reference'],'reference_missing')
        self.assertEqual(finding['status'],'insufficient_evidence')
        self.assertEqual(fit['status'],'needs_more_evidence')

    def test_fan_in_reference_retains_cash_geography_and_identity_gaps(self):
        fit=self.context()['evidenceFitness']
        finding=next(f for f in fit['findings'] if f['ruleId']=='FAN_IN')
        conditions={c['conditionId']:c['status'] for c in finding['conditionChecks']}
        self.assertEqual(conditions['multiple_sending_accounts'],'observed')
        self.assertEqual(conditions['cash_format_recorded'],'not_observed')
        self.assertEqual(conditions['geographic_withdrawal_context'],'not_verified')
        self.assertEqual(conditions['client_identity'],'not_verified')
        self.assertFalse(fit['sufficientForAutomatedDecision'])
        self.assertEqual(finding['status'],'partial_reference')

    def test_wire_format_does_not_prove_distinct_customers_or_purpose(self):
        graph=self.graph('fan_out')
        for row in graph['edges']:row['paymentFormat']='Wire'
        fit=build_context(self.index,graph)['evidenceFitness']
        finding=next(f for f in fit['findings'] if f['ruleId']=='FAN_OUT')
        conditions={c['conditionId']:c['status'] for c in finding['conditionChecks']}
        self.assertEqual(conditions['wire_format_recorded'],'observed')
        self.assertEqual(conditions['client_identity'],'not_verified')
        self.assertEqual(conditions['economic_purpose'],'not_verified')
        self.assertFalse(finding['boundedRequirementsMet'])

    def test_unmet_payment_conditions_render_as_gaps_and_legacy_reports_verify(self):
        for kind, payment_format in (("fan_in", "Cash"), ("fan_out", "Wire")):
            with self.subTest(kind=kind):
                graph = self.graph(kind)
                for row in graph["edges"]:
                    row["paymentFormat"] = "ACH"
                context = build_context(self.index, graph)
                report = generate_report(context)
                legacy_label = f"관련 거래의 원천 지급 방식이 모두 {payment_format}로 기록됨"
                self.assertIn(f"{payment_format} 조건 미충족", report["markdown"])
                self.assertNotIn(legacy_label, report["markdown"])
                self.assertTrue(verify_report(self.repo, graph, None, report)["valid"])

                legacy = render_report(context, report["plan"], report["generation"], legacy_condition_labels=True)
                self.assertIn(legacy_label, legacy["markdown"])
                self.assertTrue(verify_report(self.repo, graph, None, legacy)["valid"])

                report["markdown"] = legacy["markdown"]
                self.assertFalse(verify_report(self.repo, graph, None, self.rehash(report))["valid"])

    def test_insufficient_question_is_not_answered_by_other_references(self):
        context=self.context(query='KB국민은행 내부 규정 STR 승인 기준')
        self.assertTrue(context['citations'])
        self.assertFalse(context['evidenceFitness']['queryCheck']['requestedScopeFound'])
        self.assertEqual(context['evidenceFitness']['status'],'needs_more_evidence')
        self.assertIn('다른 참고 문서로 해당 질문의 답을 대신하지 않습니다',generate_report(context)['markdown'])

    def test_no_findings_does_not_become_a_normal_transaction_decision(self):
        report=self.report('normal')
        self.assertEqual(report['evidenceFitness']['status'],'no_rule_findings')
        self.assertFalse(report['reviewReadiness']['readyForSubmission'])
        self.assertIn('정상 거래를 확정하지 않습니다',report['markdown'])

    def test_source_revision_change_fails_closed(self):
        payload=json.loads(self.path.read_text(encoding='utf-8'))
        chunk_id=self.policy['references']['funnel']['chunkId']
        for row in payload['chunks']:
            if row['chunkId']==chunk_id:row['sourceSha256']='0'*64
        payload.pop('corpusSha256');payload['corpusSha256']=digest(canonical(payload))
        atomic_json(self.path,payload)
        index=Index(self.path)
        fit=build_context(index,self.graph())['evidenceFitness']
        finding=next(f for f in fit['findings'] if f['ruleId']=='FAN_IN')
        self.assertEqual(next(r for r in finding['referenceChecks'] if r['role']=='pattern_reference')['status'],'reference_missing')

    def test_damaged_policy_unknown_fields_and_missing_conditions_are_rejected(self):
        for mutation in ['digest','condition','field']:
            policy=copy.deepcopy(self.policy)
            if mutation=='digest':policy['policySha256']='0'*64
            elif mutation=='condition':
                policy['rules']['FAN_IN']['conditions'].remove('client_identity')
                policy['policySha256']=policy_digest(policy)
            else:policy['runPython']='untrusted instruction'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_policy(policy)

    def test_foreign_reference_cannot_replace_domestic_reporting_context(self):
        policy=copy.deepcopy(self.policy)
        policy['rules']['FAN_IN']['roles']['reporting_context']=['funnel']
        policy['policySha256']=policy_digest(policy)
        with self.assertRaisesRegex(ValueError,'Domestic legal context'):validate_policy(policy)

    def test_general_law_cannot_replace_pattern_reference(self):
        policy=copy.deepcopy(self.policy)
        policy['rules']['CYCLE']['roles']['pattern_reference']=['str']
        policy['policySha256']=policy_digest(policy)
        with self.assertRaisesRegex(ValueError,'pattern reference'):validate_policy(policy)

    def test_absent_source_and_stale_corpus_cannot_activate_policy(self):
        for kind in ['binding','corpus']:
            policy=copy.deepcopy(self.policy)
            if kind=='binding':
                policy['references']['str']['chunkId']='unknown:0000'
                policy['policySha256']=policy_digest(policy)
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                self.manager.apply_evidence_policy(policy,'0'*64 if kind=='corpus' else self.index.corpus_hash)

    def test_policy_citations_and_gap_observation_cannot_be_omitted(self):
        context=self.context()
        for field,value in [('citationIds',context['evidenceFitness']['requiredCitationIds'][0]),('observationIds','evidence_fitness')]:
            plan=default_plan(context);plan[field].remove(value)
            with self.subTest(field=field),self.assertRaises(ValueError):validate_plan(context,plan)

    def test_original_anchors_are_quoted_and_snapshot_verification_remains_separate(self):
        report=self.report()
        quotes={r['chunkId']:r['quote'] for r in report['draft']['citations']}
        for row in report['evidenceFitness']['findings']:
            for role in row['referenceChecks']:
                for ref in role['references']:
                    if ref['matched']:self.assertIn(ref['quote'],quotes[ref['chunkId']])
        result=verify_report(self.repo,self.graph(),None,report)
        self.assertTrue(result['valid'],result['errors'])
        self.assertTrue(result['checks']['sourceSnapshots']['valid'])
        self.assertTrue(result['checks']['evidenceFitness']['valid'])
        self.assertEqual(result['evidenceAssessmentStatus'],'needs_more_evidence')
        self.assertFalse(report['evidenceFitness']['rawSnapshotChecked'])
        self.assertFalse(result['semanticEntailmentVerified'])

    def test_self_rehashed_sufficiency_or_readiness_tampering_is_rejected(self):
        for field in ['evidenceFitness','reviewReadiness']:
            report=self.report()
            if field=='evidenceFitness':report[field]['sufficientForAutomatedDecision']=True
            else:report[field]['readyForSubmission']=True
            result=verify_report(self.repo,self.graph(),None,self.rehash(report))
            self.assertFalse(result['valid'])

    def test_omitting_checked_anchor_even_with_a_verbatim_quote_is_rejected(self):
        report=self.report()
        ref=self.policy['references']['str']
        quote=next(r for r in report['draft']['citations'] if r['chunkId']==ref['chunkId'])
        quote['quote']=self.index.by_id[ref['chunkId']]['text'][:4]
        result=verify_report(self.repo,self.graph(),None,self.rehash(report))
        self.assertFalse(result['checks']['evidenceFitness']['valid'])

    def test_snapshot_corruption_does_not_pass_full_verification(self):
        ref=self.policy['references']['str']
        path=self.raw/'snapshots'/ref['documentId']/(ref['sourceSha256']+'.html')
        path.write_bytes(b'<p>damaged</p>')
        result=verify_report(self.repo,self.graph(),None,self.report())
        self.assertFalse(result['valid'])
        self.assertFalse(result['checks']['sourceSnapshots']['valid'])

    def test_agent_and_http_assess_require_canonical_graph_inputs(self):
        tools=AgentTools(self.index)
        result=tools.call('assess_evidence_fit',{'graph':self.graph()})
        self.assertEqual(result['evidenceFitness'],self.context()['evidenceFitness'])
        with self.assertRaises(ValueError):tools.call('assess_evidence_fit',{'graph':self.graph(),'evidenceFitness':{}})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.index))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            def request(body):
                client=http.client.HTTPConnection(*server.server_address,timeout=10)
                try:
                    client.request('POST','/v1/rag/evidence/assess',json.dumps(body),{'Content-Type':'application/json'})
                    response=client.getresponse();return response.status,json.loads(response.read())
                finally:client.close()
            code,body=request({'graph':self.graph()})
            self.assertEqual(code,200)
            self.assertEqual(body['evidenceFitness'],result['evidenceFitness'])
            code,body=request({'graph':self.graph(),'corpusSha256':self.old.corpus_hash})
            self.assertEqual(code,200);self.assertEqual(body['status'],'not_configured')
            self.assertEqual(request({'graph':self.graph(),'assessment':{}})[0],400)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
