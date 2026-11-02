"""Node-rendered review controls keep comments as text and copy server notes."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/dist/index.js"
SOURCE = PAGE.read_text(encoding="utf-8")
HELPERS = SOURCE.split("// Pure diff helpers.", 1)[1].split("// End pure diff helpers.", 1)[0]
HELPERS = HELPERS[HELPERS.index("function diffCounts"):]
CONTENT = "function DiffContent(" + SOURCE.split("function DiffContent(", 1)[1].split(
    "// Shared across panel unmounts", 1)[0]
CHANGES = "const taskDiffCache =" + SOURCE.split("const taskDiffCache =", 1)[1].split(
    "function ExpandedTask(", 1)[0]
NODE = shutil.which("node")

HARNESS = r'''
const assert = require('node:assert/strict');
const Button = 'button', React = {Fragment:'fragment'};
const h = (type, props, ...children) => typeof type === 'function'
  ? type({...props, children}) : {type, props:props || {}, children};
const flatten = v => Array.isArray(v) ? v.flatMap(flatten)
  : v && typeof v === 'object' ? [v, ...flatten(v.children)] : [];
const text = v => Array.isArray(v) ? v.map(text).join('')
  : v && typeof v === 'object' ? text(v.children) : String(v ?? '');
let state=[], cursor=0, effects=[], deps=[];
const useState = initial => {
  const i=cursor++;
  if (!(i in state)) state[i]=initial;
  return [state[i], value=>{state[i]=value;}];
};
const useEffect = (fn, next) => {
  const i=cursor++;
  if (!deps[i] || JSON.stringify(deps[i])!==JSON.stringify(next)) {
    effects.push(fn); deps[i]=next;
  }
};
const copied=[], showToast=()=>{};
const copy=(...args)=>copied.push(args);
const reason=err=>err.message;
const calls=[];
const payload='<script>alert("comment")</script>';
let server={version:1,commit:'abc',comments:[],note:'SERVER REDACTED NOTE'};
let fail=false;
const api=async(path,init)=>{
  calls.push({path,init});
  if (init) {
    if (fail) throw new Error('409: conflict');
    const body=JSON.parse(init.body);
    assert.equal(body.version,server.version);
    assert.deepEqual(Object.keys(body).sort(),['comments','version']);
    assert(body.comments.every(c=>!('created_at' in c)));
    server={...server,version:server.version+1,comments:body.comments.map((c,i)=>({
      ...c,id:c.id||('00000000000'+i).slice(-12),created_at:123}))};
  }
  return structuredClone(server);
};
const line={kind:'ctx',old:7,new:8,text:'code',masked:[]};
let diff={files:[{path:'app.py',status:'modified',added:1,removed:1,hunks:[{
  header:'@@ -7 +8 @@',lines:[line,
    {kind:'add',old:null,new:9,text:'new'},
    {kind:'del',old:9,new:null,text:'old'},
    {kind:'note',old:null,new:null,text:'note'}]}]}],added:1,removed:1};
const render=()=>{cursor=0;const [editor,setEditor]=useState(null);
  return TaskReview({runId:'r /',taskId:'t?',diff,showToast,editor,setEditor});};
const flush=async()=>{for(const fn of effects.splice(0)) fn(); await Promise.resolve(); await Promise.resolve();};
const button=(tree,label)=>flatten(tree).find(n=>n.type==='button'&&text(n)===label);
const editor=tree=>flatten(tree).find(n=>n.type==='textarea');
const shown={id:'abcdef012345',path:'app.py',side:'new',line:8,text:payload,created_at:123};
'''


def node(program):
    if not NODE:
        pytest.skip("node is unavailable; static sink check still runs")
    result = subprocess.run([NODE, "-e", HARNESS + HELPERS + CONTENT + CHANGES +
                             "\n(async()=>{\n" + program +
                             "\n})().catch(err=>{console.error(err);process.exitCode=1;});"],
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


def test_comments_and_editor_payload_stay_text_and_note_is_server_text():
    node(r'''
      server.comments=[shown];
      render(); await flush();
      let tree=render();
      assert(text(tree).includes(payload));
      assert(!flatten(tree).some(n=>n.type==='script'));
      assert(!flatten(tree).some(n=>Object.keys(n.props).some(k=>/HTML/.test(k))));
      await button(tree,'Edit').props.onClick();
      tree=render();
      assert.equal(editor(tree).props.value,payload);
      assert(!flatten(tree).some(n=>n.type==='script'));
      button(tree,'Copy review note').props.onClick();
      assert.deepEqual(copied,[['SERVER REDACTED NOTE',showToast]]);
      assert(text(tree).includes("Pasted into Hermes chat, this note becomes the next task's goal and its commit message."));
    ''')


def test_copy_button_only_appears_with_comments():
    node(r'''
      let tree=render();
      assert(!button(tree,'Copy review note'));
      await flush(); tree=render();
      assert(!button(tree,'Copy review note'));
      const adds=flatten(tree).filter(n=>n.props.className==='hc-review-add');
      assert.equal(adds.length,3); // one per code line; note offers neither
      assert.deepEqual(adds.map(n=>n.props['aria-label']),[
        'Comment on app.py new line 8',
        'Comment on app.py new line 9','Comment on app.py old line 9']);
      adds[0].props.onClick();
      tree=render();
      editor(tree).props.onChange({target:{value:payload}});
      await button(render(),'Save').props.onClick();
      tree=render();
      assert(button(tree,'Copy review note'));
      assert.equal(server.comments[0].side,'new');
      assert.equal(server.comments[0].line,8);
      assert.equal(server.comments[0].text,payload);
      assert.equal(calls[1].init.method,'PUT');
      assert.equal(calls[1].init.headers['Content-Type'],'application/json');
      await button(tree,'Delete').props.onClick();
      assert(!button(render(),'Copy review note'));
    ''')


def test_review_loads_when_diff_fails_and_unshown_comments_remain_editable():
    node(r'''
      server.comments=[shown,{...shown,id:'bbbbbbbbbbbb',path:'cut.py',line:999}];
      diff={error:'branch gone',files:[]};
      render(); await flush();
      let tree=render();
      assert.equal(calls.length,1);
      assert.equal(calls[0].path,'/runs/r%20%2F/tasks/t%3F/review');
      assert(text(tree).includes('Comments not on a shown line'));
      assert(text(tree).includes('cut.py:999'));
      assert(button(tree,'Copy review note'));
      button(tree,'Edit').props.onClick();
      tree=render();
      assert(editor(tree));
      editor(tree).props.onChange({target:{value:'Edited after prune'}});
      await button(render(),'Save').props.onClick();
      assert.equal(server.comments[0].text,'Edited after prune');
      assert.equal(server.comments[0].id,shown.id);
      diff={error:'git diff failed',files:[]};
      assert(text(render()).includes('cut.py:999'));
      diff=null;
      assert(text(render()).includes('Comments not on a shown line'));
    ''')


def test_truncated_lines_are_listed_below_and_shown_comments_are_under_line():
    node(r'''
      server.comments=[shown,{...shown,id:'bbbbbbbbbbbb',line:999}];
      render(); await flush();
      const tree=render();
      const lines=flatten(tree).filter(n=>n.props.className==='hc-review-line');
      assert(text(lines[0]).includes('app.py:8'));
      assert(!text(lines[0]).includes('app.py:999'));
      const hidden=flatten(tree).find(n=>n.props.className==='hc-review-hidden');
      assert(text(hidden).includes('app.py:999'));
      assert(!text(hidden).includes('app.py:8'));
    ''')


def test_conflict_preserves_local_data_and_requires_explicit_reload():
    node(r'''
      server.comments=[shown];
      render(); await flush();
      button(render(),'Edit').props.onClick();
      editor(render()).props.onChange({target:{value:'Unsaved local edit'}});
      server={...server,version:2,comments:[{...shown,text:'Other tab edit'}]};
      fail=true;
      await button(render(),'Save').props.onClick();
      let tree=render();
      assert(text(tree).includes('Changed in another tab; reload the review'));
      assert(text(tree).includes(payload));
      assert.equal(editor(tree).props.value,'Unsaved local edit');
      assert(editor(tree).props.disabled);
      assert(button(tree,'Delete').props.disabled);
      const n=calls.length;
      await button(tree,'Save').props.onClick();
      assert.equal(calls.length,n);
      await button(tree,'Reload review').props.onClick();
      tree=render();
      assert(text(tree).includes('Other tab edit'));
      assert(!editor(tree));
      assert(!text(tree).includes('Changed in another tab'));
      assert(!button(tree,'Delete').props.disabled);
    ''')


def test_no_new_html_sink():
    sinks = re.findall(r'[^\n]*(?:\binnerHTML\b|\bdangerouslySetInnerHTML\b|'
                       r'\binsertAdjacentHTML\b|\bouterHTML\b|\bdocument\.write\b|'
                       r'\beval\s*\(|\bnew\s+Function\b)[^\n]*', SOURCE)
    assert [line.strip() for line in sinks] == ['svgHost.current.innerHTML = drawing.svg;']


def test_add_controls_are_in_diff_rows_and_empty_comment_rows_are_absent():
    node(r'''
      render(); await flush();
      const tree=render();
      const rows=flatten(tree).filter(n=>/^hc-diff-line /.test(n.props.className));
      assert.equal(rows.length,4);
      assert.deepEqual(rows.map(row=>flatten(row).filter(n=>n.props.className==='hc-review-add').length),[1,1,1,0]);
      assert(!flatten(tree).some(n=>n.props.className==='hc-review-line'));
      for (const row of rows.slice(0,3)) {
        const gutter=flatten(row).find(n=>n.props.className==='hc-review-gutter');
        assert.equal(flatten(gutter).filter(n=>n.type==='button').length,1);
      }
      const add=flatten(rows[0]).find(n=>n.props.className==='hc-review-add');
      add.props.onClick();
      assert.equal(flatten(render()).filter(n=>n.props.className==='hc-review-line').length,1);
      button(render(),'Cancel').props.onClick();
      assert(!flatten(render()).some(n=>n.props.className==='hc-review-line'));
    ''')
    css = PAGE.with_name("style.css").read_text()
    assert ".hc-diff-line:hover .hc-review-add" in css
    assert ".hc-diff-line:focus-within .hc-review-add" in css


def test_closing_changes_keeps_the_unsaved_editor_above_the_panel():
    node(r'''
      TaskReview = props => h('review-panel',props);
      const renderChanges=()=>{cursor=0;return TaskChanges({runId:'r',taskId:'t',commitSha:'abc',showToast});};
      const panel=tree=>flatten(tree).find(n=>n.type==='review-panel');
      taskDiffCache.set(JSON.stringify(['r','t','abc']),Promise.resolve(diff));
      let tree=renderChanges();
      assert(!panel(tree));
      await button(tree,'Changes (+?/-?)').props.onClick();
      tree=renderChanges();
      panel(tree).props.setEditor({...shown,text:'Unsaved draft'});
      tree=renderChanges();
      await button(tree,'Changes (+1/-1)').props.onClick();
      assert(!panel(renderChanges()));
      await button(renderChanges(),'Changes (+1/-1)').props.onClick();
      assert.equal(panel(renderChanges()).props.editor.text,'Unsaved draft');
      assert.equal(panel(renderChanges()).props.editor.id,shown.id);
    ''')
