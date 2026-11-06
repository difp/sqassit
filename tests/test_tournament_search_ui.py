"""Поиск фильтрует сетку, не меняя состав и результаты турнира."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js is required')
def test_tournament_grid_search_filters_matches_and_restores_them():
    template = (Path(__file__).parents[1] / 'app/static/squashtabs/index.html').read_text()
    source = template[template.index('function normalizeGridSearch('):template.index('function renderGrid(){')]
    script = r'''
const assert = require('assert');
function court(a, b) { return {dataset:{searchA:a,searchB:b}, hidden:false}; }
const a = court('Анна','Елена'), b = court('Мария','Ёгор'), c = court('АННА','Ёгор');
function round(courts) {return {hidden:false, querySelectorAll: () => courts};}
const rounds = [round([a,b]), round([c])];
const nodes = {
  'grid-search-input': {value:''},
  'grid-search-clear': {hidden:true},
  'grid-search-status': {textContent:''},
  'panel-grid': {querySelectorAll: () => rounds},
};
const $ = id => nodes[id];
''' + source + r'''
const input = nodes['grid-search-input'];
input.value = '  анНа  '; applyGridSearch();
assert.deepStrictEqual([a.hidden,b.hidden,c.hidden], [false,true,false]);
assert.deepStrictEqual(rounds.map(r => r.hidden), [false,false]);
assert.strictEqual(nodes['grid-search-status'].textContent, 'Найдено матчей: 2');
assert.strictEqual(nodes['grid-search-clear'].hidden, false);
input.value = 'егор'; applyGridSearch();
assert.deepStrictEqual([a.hidden,b.hidden,c.hidden], [true,false,false]);
input.value = 'елена'; applyGridSearch();
assert.deepStrictEqual([a.hidden,b.hidden,c.hidden], [false,true,true]);
assert.strictEqual(rounds[1].hidden, true);
input.value = 'нет такого'; applyGridSearch();
assert.strictEqual(nodes['grid-search-status'].textContent, 'Матчи не найдены');
assert.deepStrictEqual(rounds.map(r => r.hidden), [true,true]);
input.value = ''; applyGridSearch();
assert.deepStrictEqual([a.hidden,b.hidden,c.hidden], [false,false,false]);
assert.deepStrictEqual(rounds.map(r => r.hidden), [false,false]);
assert.strictEqual(nodes['grid-search-clear'].hidden, true);
assert.strictEqual(nodes['grid-search-status'].textContent, '');
'''
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
