"""Проверка порядка сохранения изменений общего турнира."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js is required')
def test_tournament_saves_are_serialized_and_use_latest_revision():
    text = (Path(__file__).parents[1] / 'app/templates/base.html').read_text()
    source = text[text.index('    async function saveTrainingTournament('):text.index('    async function pollTrainingTournament(')]
    script = r'''
const assert = require('assert');
const calls = [], scheduled = [];
let resolveFirst;
const tabsCode = {textContent:''};
let tabsTraining = {id: 4, revision: 0, canManage: true, busy: false, conflict: false,
                    pending:{scores: {'0-0': {a: 1}}}};
function api(path, method, body) {
  calls.push(body);
  if (calls.length === 1) return new Promise(resolve => {resolveFirst = resolve;});
  return Promise.resolve({revision: 2});
}
function toast() { throw Error('unexpected error'); }
function setTimeout(fn) { scheduled.push(fn); }
''' + source + r'''
(async () => {
  const saving = saveTrainingTournament(tabsTraining);
  tabsTraining.pending = {scores: {'0-0': {a: 2}}};
  await saveTrainingTournament(tabsTraining);
  assert.strictEqual(calls.length, 1);
  resolveFirst({revision: 1});
  await saving;
  assert.strictEqual(scheduled.length, 1);
  await scheduled[0]();
  await new Promise(resolve => process.nextTick(resolve));
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[1].revision, 1);
  assert.strictEqual(calls[1].state.scores['0-0'].a, 2);
  assert.strictEqual(tabsTraining.revision, 2);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
