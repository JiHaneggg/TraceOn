import assert from 'node:assert/strict';
import test from 'node:test';
import {acceptJudge} from '../static/judge-format.mjs';

const current = {reviewId:'review-current', snapshotId:'graph-current', judge:null};
const reply = {schemaVersion:'1.0', reviewId:current.reviewId, snapshotId:current.snapshotId,
  judgeState:'finished', judge:{decisionStatus:'not_judged', verificationScore:null}};

test('a no-model verdict remains distinguishable and tied to the original draft', () => {
  const result = acceptJudge(current, reply);
  assert.equal(result.judge.decisionStatus, 'not_judged');
  assert.equal(result.judge.verificationScore, null);
  assert.equal(current.judge, null);
});

test('a response for an old draft or graph is refused', () => {
  for (const key of ['reviewId', 'snapshotId', 'judgeState']) {
    assert.throws(() => acceptJudge(current, {...reply, [key]:'old'}));
  }
});
