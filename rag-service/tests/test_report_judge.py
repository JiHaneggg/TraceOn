"""HTTP and exact-report Judge integration; all provider replies are synthetic."""
from contextlib import contextmanager
from http.client import IncompleteRead
from copy import deepcopy
from http.server import ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError, HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_workflow_demo import fixture_tools
from corpus import canonical, digest
from evidence_lookup import EvidenceError
from rag_server import handler_for
from report_judge import judge_verified_report
from test_agent_judge import KEY, model_response, provider_reply


class ReportJudgeTest(unittest.TestCase):
    def setUp(self):
        self.tools, self.request = fixture_tools(self.enterContext(tempfile.TemporaryDirectory()))
        self.prepared = self.tools.call('prepare_review_report', {k:self.request[k] for k in ('graph','ruleResult')})
        self.inputs = {k:deepcopy(self.request[k]) for k in ('graph','ruleResult')}
        self.inputs['report'] = self.prepared['report']
        self.enterContext(patch.dict(os.environ, {'OPENAI_API_KEY': KEY, 'OPENAI_MODEL': 'fixture-model'}))

    def run_judge(self, provider):
        with patch('agent_judge.urlopen', side_effect=provider) as transport:
            result = self.invoke()
        return result, transport

    def invoke(self):
        return judge_verified_report(self.tools.repository,self.inputs['graph'],self.inputs['ruleResult'],self.inputs['report'])

    @staticmethod
    def reply(request, timeout, verdict='supported'):
        payload = json.loads(json.loads(request.data)['input'][1]['content'])
        overrides = {row['itemId']: verdict for row in payload['items']}
        return provider_reply(model_response(payload, overrides))

    def test_exact_prepared_draft_is_verified_without_regeneration(self):
        original = deepcopy(self.inputs)
        def provider(request, timeout):
            self.assertEqual(request.get_header('Authorization'), 'Bearer '+KEY)
            payload = json.loads(json.loads(request.data)['input'][1]['content'])
            self.assertEqual(payload['draft'], original['report']['markdown'])
            self.assertEqual(payload['identity']['reportSha256'], digest(canonical(original['report'])))
            self.assertNotIn(KEY, request.data.decode())
            return provider_reply(model_response(payload))
        with patch('reports.generate_report', side_effect=AssertionError('Must use the original draft')):
            result, transport = self.run_judge(provider)
        transport.assert_called_once()
        judge = result['judge']
        self.assertEqual(judge['decisionStatus'], 'passed')
        self.assertTrue(judge['evaluationCompleted'])
        self.assertTrue(judge['evidenceTxIds'])
        self.assertEqual(judge['identity']['reportId'], original['report']['reportId'])
        self.assertEqual(self.inputs, original)
        self.assertNotIn(KEY, json.dumps(result))

    def test_insufficient_evidence_is_a_scored_hold(self):
        result, _ = self.run_judge(lambda request, timeout:self.reply(request, timeout, 'insufficient'))
        judge = result['judge']
        self.assertEqual((judge['decisionStatus'],judge['outcomeCategory'],judge['verificationScore']), ('held','insufficient_evidence',50))
        self.assertTrue(judge['evaluationCompleted'])

    def test_contradiction_is_rejected(self):
        result, _ = self.run_judge(lambda request, timeout:self.reply(request, timeout, 'contradicted'))
        self.assertEqual((result['judge']['decisionStatus'],result['judge']['verificationScore']),('rejected',0))

    def test_unknown_evidence_response_is_not_a_semantic_hold(self):
        def provider(request, timeout):
            payload=json.loads(json.loads(request.data)['input'][1]['content'])
            response=model_response(payload);response['items'][0]['factIds']=['foreign-fact']
            return provider_reply(response)
        result, _ = self.run_judge(provider);judge=result['judge']
        self.assertEqual((judge['decisionStatus'],judge['outcomeCategory']),('not_judged','model_response'))
        self.assertFalse(judge['evaluationCompleted']);self.assertIsNone(judge['verificationScore'])
        self.assertEqual(judge['responseErrorCode'],'unknown_evidence_id')
        self.assertEqual(judge['items'],[])

    def test_transport_error_is_unscored_and_secret_free(self):
        result, _ = self.run_judge(URLError('private request '+KEY));judge=result['judge']
        self.assertEqual((judge['decisionStatus'],judge['outcomeCategory']),('not_judged','transport'))
        self.assertEqual(judge['modelCallAttempts'],1);self.assertNotIn(KEY,canonical(result))

    def test_malformed_provider_json_is_not_judged(self):
        result,_=self.run_judge(lambda request,timeout:io.BytesIO(b'{invalid-json'))
        self.assertEqual(result['judge']['decisionStatus'],'not_judged')
        self.assertEqual(result['judge']['responseErrorCode'],'invalid_response_json')
        self.assertIsNone(result['judge']['verificationScore'])

    def test_provider_error_only_keeps_allowlisted_diagnostics(self):
        error=HTTPError('https://api.openai.com/v1/responses',429,'private '+KEY,{},
                        io.BytesIO(json.dumps({'error':{'code':'insufficient_quota','message':KEY}}).encode()))
        result,_=self.run_judge(error);judge=result['judge']
        self.assertEqual((judge['decisionStatus'],judge['outcomeCategory']),('not_judged','provider_error'))
        self.assertEqual(judge['providerErrorCode'],'insufficient_quota');self.assertEqual(judge['providerHttpStatus'],429)
        self.assertNotIn(KEY,canonical(result))

    def test_missing_local_config_does_not_call_provider(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'','OPENAI_MODEL':''}):
            result, transport=self.run_judge(AssertionError('Provider must not be used'))
        transport.assert_not_called()
        self.assertEqual(result['judge']['reasonCode'],'judge_not_configured')
        self.assertEqual(result['judge']['decisionStatus'],'not_judged')

    def test_rehashed_wrong_amount_never_reaches_judge(self):
        self.inputs['report']['draft']['facts'][0]['value']='999999.99'
        self.rehash_report()
        with patch('agent_judge.urlopen') as transport:
            with self.assertRaises(EvidenceError) as caught:self.invoke()
        self.assertEqual(caught.exception.status,409);transport.assert_not_called()

    def test_rehashed_wrong_citation_never_reaches_judge(self):
        self.inputs['report']['draft']['citations'][0]['quote']='원문에 없는 문장'
        self.rehash_report()
        with patch('agent_judge.urlopen') as transport:
            with self.assertRaises(EvidenceError):self.invoke()
        transport.assert_not_called()

    def test_another_graph_never_reaches_judge(self):
        self.inputs['graph']['edges'][0]['amountPaid']='1.00'
        with patch('agent_judge.urlopen') as transport:
            with self.assertRaises(EvidenceError):self.invoke()
        transport.assert_not_called()

    def rehash_report(self):
        report=self.inputs['report'];report['reportId']='RPT-'+digest(canonical({k:v for k,v in report.items() if k!='reportId'}))[:24]

    @contextmanager
    def service(self, judge=None, *, judge_hosts=()):
        handler=handler_for(self.tools.index,self.tools.repository.raw_root,judge=judge,judge_hosts=judge_hosts)
        class Quiet(handler):
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Quiet)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def call(body=None,raw=None,headers=None,host=None):
            connection=http.client.HTTPConnection(*server.server_address,timeout=10)
            headers = dict(headers or {})
            if host is not None:
                headers["Host"] = f"{host}:{server.server_port}"
            try:
                connection.request('POST','/v1/rag/reports/judge',raw if raw is not None else json.dumps(body or self.inputs),{'Content-Type':'application/json',**headers})
                response=connection.getresponse();return response.status,json.loads(response.read())
            finally:connection.close()
        try:yield call
        finally:server.shutdown();server.server_close();thread.join(5)

    def test_http_runs_the_existing_judge_adapter(self):
        with self.service() as call,patch('agent_judge.urlopen',side_effect=self.reply) as transport:
            status,result=call()
        self.assertEqual(status,200);self.assertEqual(result['judge']['decisionStatus'],'passed');transport.assert_called_once()

    def test_http_rejects_key_model_extra_fields_and_duplicate_keys(self):
        with self.service() as call,patch('agent_judge.urlopen') as transport:
            for key in ['apiKey','model','judgeMode','reportPath']:
                status,result=call({**self.inputs,key:KEY})
                self.assertEqual(status,400);self.assertNotIn(KEY,canonical(result))
            self.assertEqual(call(raw='{"graph":{},"graph":{},"ruleResult":{},"report":{}}')[0],400)
        transport.assert_not_called()

    def test_http_rejects_cross_origin_and_foreign_host(self):
        with self.service() as call:
            self.assertEqual(call(headers={'Origin':'http://evil.example'})[0],403)
            self.assertEqual(call(headers={'Host':'evil.example:8080'})[0],421)
            self.assertEqual(call(headers={'Host':'['})[0],421)
            self.assertEqual(call(host="rag")[0], 421)

    def test_http_accepts_only_configured_compose_host_without_weakening_origin(self):
        with self.service(judge_hosts=("rag",)) as call, patch(
                "agent_judge.urlopen", side_effect=self.reply) as transport:
            self.assertEqual(call(host="another-service")[0], 421)
            self.assertEqual(call(host="rag", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
            status, body = call(host="rag")
        self.assertEqual(status, 200)
        self.assertEqual(body["judge"]["decisionStatus"], "passed")
        transport.assert_called_once()

    def test_http_returns_not_judged_when_provider_response_is_truncated(self):
        with self.service(judge_hosts=("rag",)) as call, patch(
                "agent_judge.urlopen", side_effect=IncompleteRead(b"provider details", 10)):
            status, body = call(host="rag")
        self.assertEqual(status, 200)
        judge = body["judge"]
        self.assertEqual(judge["decisionStatus"], "not_judged")
        self.assertEqual(judge["outcomeCategory"], "transport")
        self.assertNotIn("provider details", canonical(judge))

    def test_http_serializes_expensive_judge_requests(self):
        entered,release=threading.Event(),threading.Event()
        def slow(payload):
            entered.set();release.wait(5)
            from agent_judge import decide
            return {**decide(payload,model_response(payload)), 'modelCallAttempts':1}
        result=[]
        with self.service(slow) as call:
            worker=threading.Thread(target=lambda:result.append(call()));worker.start()
            try:
                self.assertTrue(entered.wait(5));self.assertEqual(call()[0],429)
            finally:release.set();worker.join(5)
        self.assertEqual(result[0][0],200)


if __name__=='__main__':unittest.main()
