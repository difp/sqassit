"""Проверка порядка применения ответов расписания."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required")
def test_schedule_ignores_old_response_and_refreshes_in_background():
    template = (Path(__file__).parents[1] / "app/templates/index.html").read_text()
    source = template[template.index("let scheduleRequest = 0;"):template.index("async function loadProfile()")]
    script = r"""
const assert = require('assert');
let pending = [], painted = [], saved = [];
function cacheRead() { return null; }
function cacheWrite(_, data) { saved.push(data); }
function renderSchedule(data) { painted.push(data); }
function api() { return new Promise((resolve, reject) => pending.push({resolve, reject})); }
function toast() { throw Error('Unexpected error'); }
const document = {getElementById: () => ({hidden: false})};
""" + source + r"""
(async () => {
  const first = loadSchedule();
  const second = loadSchedule({background: true});
  pending[1].resolve({trainings: ['new']});
  await second;
  pending[0].resolve({trainings: ['old']});
  await first;
  assert.deepStrictEqual(painted, [['new']]);
  assert.deepStrictEqual(saved, [['new']]);
  const third = loadSchedule({background: true});
  pending[2].reject(Error('offline'));
  await third;
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
