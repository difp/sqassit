"""Проверка границ индикации и обновления без перерисовки карточек."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js is required')
def test_live_badge_follows_training_start_and_end():
    base = (Path(__file__).parents[1] / 'app/templates/base.html').read_text()
    source = base[base.index('    function trainingIsLive('):base.index('    function tournamentRow(')]
    script = r'''
const assert = require('assert');
const indicator = {dataset: {liveStart:'1000', liveDuration:'30'}, hidden:true};
const document = {hidden:false, querySelectorAll: () => [indicator], addEventListener: () => {}};
function setInterval() {}
''' + source + r'''
assert.strictEqual(trainingIsLive(1000, 30, 999), false);
assert.strictEqual(trainingIsLive(1000, 30, 1000), true);
assert.strictEqual(trainingIsLive(1000, 30, 2799), true);
assert.strictEqual(trainingIsLive(1000, 30, 2800), false);
assert.strictEqual(trainingIsLive(1000, 0, 1000), false);
assert.strictEqual(trainingLiveIndicator({start_at: 'bad', duration_min: 30}), '');
Date.now = () => 999000;
assert(trainingLiveIndicator({start_at:1000, duration_min:30}).includes(' hidden'));
Date.now = () => 1000000;
assert(!trainingLiveIndicator({start_at:1000, duration_min:30}).includes(' hidden'));
updateTrainingLiveIndicators();
assert.strictEqual(indicator.hidden, false);
Date.now = () => 2800000;
updateTrainingLiveIndicators();
assert.strictEqual(indicator.hidden, true);
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
