"""Regression checks for the real client-side reminder handler."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required for the JavaScript check")
def test_reminder_validation_and_save_feedback():
    root = Path(__file__).resolve().parents[1]
    script = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync('app/templates/coach.html', 'utf8');
const code = html.slice(html.indexOf('// ===== Напоминание ====='), html.indexOf('// ===== Соавторы ====='));
const handlers = {};
function field(value) {
  return {value, checked: false, hidden: true, textContent: '', attrs: {},
    setAttribute(k,v) { this.attrs[k]=v; }, removeAttribute(k) { delete this.attrs[k]; }};
}
const elements = {oh:field('48'), om:field('30'), remind:field(''), error:field(''), retry:field('')};
elements.remind.checked = true;
const calls = [], toasts = [];
let fail = false;
const ctx = {
  document: {
    querySelector(selector) { const m=selector.match(/data-(reminder-error|reminder-retry|remind|oh|om)/); return m ? elements[{'reminder-error':'error','reminder-retry':'retry'}[m[1]] || m[1]] : null; },
    getElementById() { return {addEventListener(type, fn) { handlers[type] = fn; }}; }
  },
  TRAININGS:[{id:1,reminder_enabled:true,reminder_offset_min:270}],
  api: async (url, method, payload) => { calls.push(payload); if (fail) throw new Error('offline'); },
  haptic(){}, hapticNotify(){}, cacheClear(){}, loadView(){},
  toast(message) { toasts.push(message); }, fmtOffset(n){ return n + ' мин'; },
};
vm.createContext(ctx); vm.runInContext(code,ctx);
(async () => {
  await handlers.change({target:{dataset:{oh:'1'}}});
  assert.equal(calls.length,0, '48h 30m must not be silently saved as 48h');
  assert.match(elements.error.textContent, /48/);
  elements.remind.checked=false;
  await handlers.change({target:{dataset:{remind:'1'}}});
  assert.equal(calls.at(-1).enabled,false);
  assert.equal(calls.at(-1).offset_min,270);
  assert.equal(elements.oh.value,'4'); assert.equal(elements.om.value,'30');
  assert.match(toasts.at(-1), /выключено/i);
  fail=true; elements.remind.checked=true;
  await handlers.change({target:{dataset:{remind:'1'}}});
  assert.match(elements.error.textContent, /не удалось сохранить/i);
  assert.equal(elements.retry.hidden,false, 'save failure must remain visible and retryable');
  fail=false;
  await handlers.click({target:{closest:() => ({getAttribute:()=>'1'})}});
  assert.equal(calls.at(-1).enabled,true);
  assert.equal(elements.error.textContent,'');

  // Two quick changes must reach the server in order, not race.
  let release; const ordered=[];
  ctx.api = (url, method, payload) => {
    ordered.push(payload.offset_min);
    return ordered.length === 1 ? new Promise(resolve => {release=resolve;}) : Promise.resolve();
  };
  elements.oh.value='1'; elements.om.value='0';
  const first = handlers.change({target:{dataset:{oh:'1'}}});
  await new Promise(setImmediate);
  elements.oh.value='2';
  const second = handlers.change({target:{dataset:{oh:'1'}}});
  await new Promise(setImmediate);
  assert.deepStrictEqual(ordered,[60]);
  release();
  await Promise.all([first,second]);
  assert.deepStrictEqual(ordered,[60,120]);
  assert.equal(ctx.TRAININGS[0].reminder_offset_min,120);
})().catch(e=>{console.error(e); process.exitCode=1;});
'''
    result = subprocess.run(
        [shutil.which("node"), "-e", script], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is required for the JavaScript check")
def test_edit_dialog_contains_focus_and_restores_background():
    root = Path(__file__).resolve().parents[1]
    script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const html=fs.readFileSync('app/templates/coach.html','utf8');
const code=html.slice(html.indexOf('// ===== Изменение тренировки ====='),html.indexOf('// ===== Напоминание ====='));
const handlers={};
const doc={activeElement:null, addEventListener(type,fn){handlers[type]=fn;}};
function node(id) {return {id, inert:false, children:[], parentElement:null, isConnected:true,
  classList:{items:new Set(), add(v){this.items.add(v)}, remove(v){this.items.delete(v)}, contains(v){return this.items.has(v)}},
  focus(){doc.activeElement=this}, addEventListener(){}, contains(other){return this === other || this.children.some(x=>x.contains(other))}};}
function attach(parent,...children){parent.children=children;children.forEach(x=>x.parentElement=parent);}
const body=node('body'), app=node('app'), header=node('header'), main=node('main'), content=node('content'), sheet=node('sheet'), tab=node('tab'), toast=node('toast'), error=node('error');
attach(body,app,tab,toast);attach(app,header,main);attach(main,content,sheet);
doc.body=body;
const first=node('first'), date=node('date'), time=node('time'), submit=node('submit'), cancel=node('cancel');
const controls=[first,date,time,submit,cancel];attach(sheet,...controls);
const form={elements:{training_type:first,date,time}, addEventListener(){}, querySelector(){return submit}};
sheet.querySelectorAll=selector => selector.includes('input:not') ? controls : [];
doc.getElementById=id => ({'edit-training-sheet':sheet,'edit-training-form':form,'edit-training-error':error})[id];
const trigger=node('trigger');
const ctx={document:doc,TRAININGS:[{id:1,can_manage:true,type:'Тренировка',date:'2026-11-03',time:'19:00'}],trigger};
vm.createContext(ctx);vm.runInContext(code,ctx);
vm.runInContext("openEditTraining('1',trigger)",ctx);
assert.equal(doc.activeElement,first);
assert.equal(content.inert,true);assert.equal(header.inert,true);assert.equal(tab.inert,true);
let prevented=false;
handlers.keydown({key:'Tab',shiftKey:true,preventDefault(){prevented=true}});
assert.equal(prevented,true);assert.equal(doc.activeElement,cancel);
prevented=false;
handlers.keydown({key:'Tab',shiftKey:false,preventDefault(){prevented=true}});
assert.equal(prevented,true);assert.equal(doc.activeElement,first);
handlers.keydown({key:'Escape'});
assert.equal(content.inert,false);assert.equal(header.inert,false);assert.equal(tab.inert,false);
assert.equal(doc.activeElement,trigger);
'''
    result = subprocess.run(
        [shutil.which("node"), "-e", script], cwd=root, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
