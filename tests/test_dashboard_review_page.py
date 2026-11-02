"""Offline contract, text boundary, and lazy loading checks for task Changes."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


PAGE = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/dist/index.js"
SOURCE = PAGE.read_text(encoding="utf-8")
NODE = shutil.which("node")
HELPERS = SOURCE.split("// Pure diff helpers.", 1)[1].split("// End pure diff helpers.", 1)[0]
HELPERS = HELPERS[HELPERS.index("function diffCounts"):]
CONTENT = "function DiffContent(" + SOURCE.split("function DiffContent(", 1)[1].split(
    "// Shared across panel unmounts", 1)[0]
CHANGES = SOURCE.split("const taskDiffCache =", 1)[1].split("function ExpandedTask(", 1)[0]
CHANGES = "const taskDiffCache =" + CHANGES


def _node(program):
    if not NODE:
        pytest.skip("node is unavailable; static dashboard checks still run")
    result = subprocess.run(
        [NODE, "-e", 'const assert = require("node:assert/strict");\n' + HELPERS + "\n" + program],
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_diff_route_is_only_requested_in_the_open_handler():
    assert SOURCE.count('+ "/diff"') == 1
    handler = CHANGES.split("const onToggle = async () => {", 1)[1].split(
        'return h("div"', 1)[0]
    assert 'if (!next || loading || (diff && !diff.error)) return;' in handler
    assert handler.index('if (!next') < handler.index('api("/runs/"')
    assert '"/tasks/" + encodeURIComponent(taskId) + "/diff"' in handler
    assert 'encodeURIComponent(runId)' in handler
    assert 'setError(reason(err))' in handler
    assert 'useEffect' not in CHANGES
    assert 'JSON.stringify([runId, taskId, commitSha])' in handler
    assert 'taskDiffCache.has(key)' in handler
    assert 'task.commit && task.commit.sha ? h(TaskChanges, { key: task.commit.sha, runId, taskId: task.id, commitSha: task.commit.sha, showToast })' in SOURCE
    assert 'h(ExpandedTask, { task, trace, runId, showToast })' in SOURCE
    assert 'task, runId: run.run_id, expanded:' in SOURCE
    expanded = SOURCE.split('function ExpandedTask(', 1)[1].split('function TaskRow(', 1)[0]
    assert expanded.index('h(VerifyBlock,') < expanded.index('h(TaskChanges,')
    assert 'expanded ? h(ExpandedTask,' in SOURCE


def test_only_the_original_strict_mermaid_html_sink_remains():
    sinks = re.findall(
        r'[^\n]*(?:\binnerHTML\b|\bdangerouslySetInnerHTML\b|\binsertAdjacentHTML\b|'
        r'\bouterHTML\b|\bdocument\.write\b|\beval\s*\(|\bnew\s+Function\b)[^\n]*', SOURCE,
    )
    assert [line.strip() for line in sinks] == ['svgHost.current.innerHTML = drawing.svg;']


def test_page_has_valid_javascript():
    if not NODE:
        pytest.skip("node is unavailable; static dashboard checks still run")
    result = subprocess.run([NODE, "--check", str(PAGE)], text=True, capture_output=True,
                            timeout=15, check=False)
    assert result.returncode == 0, result.stderr


TREE = r'''
    const h=(type,props,...children)=>({type,props,children});
    const React={Fragment:'fragment'}, Button='button';
    const flatten=value=>Array.isArray(value)?value.flatMap(flatten)
      :value&&typeof value==='object'?[value,...flatten(value.children)]:[];
    const text=value=>Array.isArray(value)?value.map(text).join('')
      :value&&typeof value==='object'?text(value.children):String(value??'');
'''
FIXTURE = r'''
    const diff={run_id:'r',task_id:'t',branch:'task/t',commit:'abc',base:'def',error:null,
      files:[{path:'<script>alert(1)</script>',status:'modified',binary:false,
        added:1,removed:1,truncated:true,hunks:[{header:'@@ -1,2 +1,2 @@ <script>',lines:[
          {kind:'del',old:1,new:null,text:'old',masked:[]},
          {kind:'add',old:null,new:1,text:'token=«masked:key» <script>',masked:['key']},
          {kind:'ctx',old:2,new:2,text:'  unchanged\ttext',masked:[]},
          {kind:'note',old:null,new:null,text:'\\ No newline at end of file',masked:[]}
        ]}]}, {path:'image.png',status:'added',binary:true,added:0,removed:0,
          truncated:false,hunks:[]}],
      added:1,removed:1,truncated:true,secret_hits:1,
      limits:{max_files:300,max_lines_per_file:3000,max_bytes:1000000}};
'''


def test_fixture_renders_as_text_with_warnings_and_file_defaults():
    _node(TREE + FIXTURE + CONTENT + r'''
        const copied=[];
        const copy=(...args)=>copied.push(args), showToast=()=>{};
        const before=JSON.stringify(diff);
        const tree=DiffContent({diff,runId:'r',showToast});
        const nodes=flatten(tree), rendered=text(tree);
        assert.equal(diffSummary(diff),'2 files · +1/-1');
        assert.equal(diffCounts(null),'+?/-?');
        assert(rendered.includes('1 line(s) hide a possible secret (shown as «masked:…»); see git diff in a terminal for the full text'));
        assert(rendered.includes('Diff too large for the page; full diff: hermes control review-pack r --diff'));
        for (const content of ['<script>alert(1)</script>', '@@ -1,2 +1,2 @@ <script>',
            'token=«masked:key» <script>', '\\ No newline at end of file',
            '  unchanged\ttext', 'binary file', 'file cut at 4 lines']) {
          assert(rendered.includes(content),content);
        }
        assert(!nodes.some(n=>n.type==='script'));
        assert(nodes.filter(n=>n.type==='details').every(n=>n.props.open));
        const rows=nodes.filter(n=>(n.props?.className||'').startsWith('hc-diff-line '));
        assert.deepEqual(rows.map(n=>n.props.className),
          ['hc-diff-line hc-diff-del','hc-diff-line hc-diff-add','hc-diff-line hc-diff-ctx','hc-diff-line hc-diff-note']);
        assert.deepEqual(rows.map(n=>n.children.map(text)),[
          ['1','','-','old'],['','1','+','token=«masked:key» <script>'],
          ['2','2',' ','  unchanged\ttext'],['','',' ','\\ No newline at end of file']]);
        nodes.find(n=>n.type==='button').props.onClick();
        assert.deepEqual(copied, [['hermes control review-pack r --diff',showToast]]);
        assert.equal(JSON.stringify(diff),before);
        const many={...diff,files:Array(11).fill(diff.files[0]),secret_hits:0,truncated:false};
        const manyTree=DiffContent({diff:many,runId:'r',showToast});
        assert(flatten(manyTree).filter(n=>n.type==='details').every(n=>!n.props.open));
        assert(!text(manyTree).includes('hide a possible secret'));
        assert(!text(manyTree).includes('Diff too large'));
        assert.equal(diffSummary({...diff,files:[diff.files[0]]}),'1 file · +1/-1');
    ''')


def test_open_is_lazy_cached_across_remounts_and_failures_can_retry():
    _node(TREE + FIXTURE + CONTENT + CHANGES + r'''
        let state=[], cursor=0;
        const useState=initial=>{
          const i=cursor++;
          if (!(i in state)) state[i]=initial;
          return [state[i],value=>{state[i]=value;}];
        };
        const calls=[];
        let reply=()=>Promise.resolve(diff);
        const api=path=>{calls.push(path);return reply();};
        const reason=err=>'reason: '+err.message;
        const render=(runId='r /',taskId='t?',commitSha='abc')=>{
          cursor=0;return TaskChanges({runId,taskId,commitSha,showToast:()=>{}});
        };
        const button=tree=>flatten(tree).find(n=>n.type==='button');
        (async()=>{
          let tree=render();
          assert.equal(calls.length,0);
          assert.equal(button(tree).props['aria-expanded'],false);
          const opened=button(tree).props.onClick();
          assert(text(render()).includes('Loading changes…'));
          await opened;
          assert.deepEqual(calls,['/runs/r%20%2F/tasks/t%3F/diff']);
          for(let i=0;i<5;i++) render();
          assert.equal(calls.length,1);
          await button(render()).props.onClick();
          assert(!text(render()).includes('Loading changes…'));
          await button(render()).props.onClick();
          assert.equal(calls.length,1);
          state=[]; // remount after task collapse or run switch
          await button(render()).props.onClick();
          assert.equal(calls.length,1);
          state=[];
          reply=()=>Promise.reject(new Error('404: missing'));
          await button(render('other')).props.onClick();
          assert(text(render('other')).includes('reason: 404: missing'));
          await button(render('other')).props.onClick();
          reply=()=>Promise.resolve({...diff,error:'branch gone <script>'});
          await button(render('other')).props.onClick();
          assert(text(render('other')).includes('branch gone <script>'));
          assert.equal(calls.length,3);
          await button(render('other')).props.onClick(); // close error
          reply=()=>Promise.resolve(diff);
          await button(render('other')).props.onClick(); // retry same mount
          assert.equal(calls.length,4);
          assert(!text(render('other')).includes('branch gone'));
          state=[];
          reply=()=>Promise.resolve({...diff,error:'git diff failed'});
          await button(render('error-run')).props.onClick();
          state=[]; // errors also retry across remounts
          reply=()=>Promise.resolve(diff);
          await button(render('error-run')).props.onClick();
          assert.equal(calls.length,6);
          state=[];
          const newer=render('r /','t?','new-sha');
          assert.equal(calls.length,6); // new commits stay lazy
          await button(newer).props.onClick();
          assert.equal(calls.length,7);
          state=[];
          await button(render('r /','different task')).props.onClick();
          assert.equal(calls.length,8);
        })().catch(err=>{console.error(err);process.exitCode=1;});
    ''')
