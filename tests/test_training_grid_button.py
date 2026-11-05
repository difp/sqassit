"""Участник видит кнопку сетки только после её создания."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js is required')
def test_grid_button_requires_confirmed_booking_and_created_grid():
    template = (Path(__file__).parents[1] / 'app/templates/index.html').read_text()
    source = template[template.index('function trainingCard(t)'):template.index('function renderSchedule(')]
    script = r'''
const assert = require('assert');
const esc = x => String(x);
const fmtDuration = () => '';
const trainingIsLive = () => false;
const trainingLiveIndicator = () => '';
const participantsLine = () => '';
const coachHtml = () => '';
const levelPill = () => '';
''' + source + r'''
const training = {id:42, time:'19:00', type:'Тренировка', duration_min:60,
  capacity:6, booked_count:1, is_booked:true, is_past:false,
  can_self_cancel:true, tournament_started:true, participants:[]};
const html = trainingCard(training);
assert(html.includes('data-tournament="42"'));
assert(html.includes('>Сетка</span>'));
assert(!trainingCard({...training, tournament_started:false}).includes('data-tournament='));
assert(!trainingCard({...training, is_booked:false, is_pending:true}).includes('data-tournament='));
assert(!trainingCard({...training, is_booked:false, is_waitlisted:true}).includes('data-tournament='));
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
