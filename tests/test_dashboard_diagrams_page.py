"""Offline checks for the shipped, build-free Diagrams page and spec-to-Mermaid boundary."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PAGE = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/dist/index.js"
SOURCE = PAGE.read_text(encoding="utf-8")
NODE = shutil.which("node")


def _node(program, *, helpers=True):
    if not NODE:
        pytest.skip("node is unavailable; static dashboard checks still run")
    pure = SOURCE.split("// Pure diagram helpers", 1)[1].split("// End pure diagram helpers.", 1)[0]
    pure = pure[pure.index("function mermaidLabel"):]
    result = subprocess.run(
        [NODE, "-e", 'const assert = require("node:assert/strict");\n'
         + (pure if helpers else "") + "\n" + program],
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_page_contract_and_strict_renderer_are_always_checked():
    assert '/dashboard-plugins/hermes_control/dist/vendor/mermaid.min.js' in SOURCE
    assert 'securityLevel: "strict"' in SOURCE
    assert 'startOnLoad: false' in SOURCE
    assert 'htmlLabels: false' in SOURCE
    assert 'flowchart: { htmlLabels: false }' in SOURCE
    assert 'mermaid.render(id, mermaidText)' in SOURCE
    assert 'new URLSearchParams(window.location.search).get("diagram")' in SOURCE
    assert '["diagrams", "Diagrams"]' in SOURCE
    assert '["runs", "diagrams"].includes(stored)' in SOURCE
    assert 'if (initialDiagram) return "diagrams"' in SOURCE
    assert 'api("/diagrams")' in SOURCE
    assert 'api("/diagrams/" + encodeURIComponent(selected))' in SOURCE
    assert 'const DIAGRAM_POLL_MS = 5000' in SOURCE
    assert 'if (!listLive) return undefined' in SOURCE
    assert 'detailLive ? setInterval(load, DIAGRAM_POLL_MS) : null' in SOURCE
    assert 'Diagram not found. Choose a diagram from the list.' in SOURCE
    assert 'h(DiagramFallback, { view, onSelect })' in SOURCE
    assert SOURCE.count('document.createElement("script")') == 1


def test_worker_text_only_enters_html_through_the_strict_mermaid_svg():
    assert "dangerouslySetInnerHTML" not in SOURCE
    html_sinks = re.findall(r"[^\n]*(?:\.innerHTML\b|\binsertAdjacentHTML\b|\bouterHTML\b)[^\n]*", SOURCE)
    assert [line.strip() for line in html_sinks] == ['svgHost.current.innerHTML = drawing.svg;']


def test_viewport_renders_an_empty_svg_host_without_react_owned_markup():
    drawing = SOURCE.split('function DiagramDrawing(', 1)[1].split('function DiagramEdgeList(', 1)[0]
    assert 'dangerouslySetInnerHTML' not in drawing
    assert re.search(r'className: "hc-diagram-svg", ref: container\s*},\s*'
                     r'h\("div", { ref: svgHost }\)', drawing)


def test_labels_escape_mermaid_control_characters_before_interpolation():
    _node(r'''
        const input = '"[]#;<>&\n\r`|\\%{}\'';
        const output = mermaidLabel(input);
        assert.equal(output, '”［］＃；‹›＆  ˋ｜＼％（）’');
        assert.equal(mermaidLabel(null), '');
        assert.equal(mermaidLabel('x'.repeat(81) + '<script>'), 'x'.repeat(80));
        assert.equal(mermaidLabel('😀'.repeat(81)), '😀'.repeat(80));
        assert.equal(mermaidLabel('x'.repeat(79) + '"tail'), 'x'.repeat(79) + '”');
        const attack = '"; end\nclick n0 "javascript:alert(1)" <img> #quot;';
        const result = toMermaid({kind:'module', nodes:[{id:'arbitrary-worker-id', label:attack}], edges:[]});
        assert(!result.includes('arbitrary-worker-id'));
        assert(!result.includes('<img>'));
        assert(!result.includes('\nclick'));
        assert(!result.includes('"; end'));
        assert(result.includes('＃quot；'));
    ''')


def test_module_groups_and_verdict_marks_use_safe_generated_ids():
    _node(r'''
        const view = {
          kind:'module', groups:[{id:'worker-group',label:'Core ["quoted"]'}],
          nodes:[
            {id:'worker-a',label:'A',group:'worker-group',verdict:'confirmed'},
            {id:'worker-b',label:'B',verdict:'wrong',evidence_invalid:true,claude_edited:true},
            {id:'worker-c',label:'C',verdict:'unverifiable'},
            {id:'worker-d',label:'D'}],
          edges:[{id:'worker-edge',from:'worker-a',to:'worker-b',label:'calls',verdict:'wrong',
                  evidence_invalid:true,claude_edited:true},
                 {id:'e2',from:'worker-b',to:'worker-c',label:'reads',verdict:'confirmed'},
                 {id:'e3',from:'worker-c',to:'worker-d',label:'unknown'}]
        };
        const before = JSON.stringify(view);
        const text = toMermaid(view);
        assert(text.startsWith('flowchart LR\nsubgraph g0["Core ［”quoted”］"]\nn0["A"]\nend'));
        assert(text.includes('n0 --> n1'));
        assert(toMermaid(view, {showEdgeLabels:true}).includes('n0 -->|"calls"| n1'));
        assert(!text.includes('worker-'));
        assert(text.includes('class n0 confirmed'));
        assert(text.includes('class n1 wrong\nclass n1 claude_edited\nclass n1 evidence_invalid'));
        assert(!/^class n\d+ .*?,/m.test(text));
        assert(text.includes('class n2 unverifiable'));
        assert(text.includes('class n3 unreviewed'));
        assert(text.includes('linkStyle 0 stroke:#f97316,stroke-width:3px,stroke-dasharray:5 3'));
        assert(!text.includes('filter:'));
        assert(!text.includes('drop-shadow'));
        assert(text.includes('linkStyle 1 stroke:#16a34a'));
        assert(text.includes('linkStyle 2 stroke:#6b7280'));
        assert.equal(JSON.stringify(view), before);
    ''')


def test_flow_numbering_and_sequence_messages_follow_edge_order():
    _node(r'''
        const view = {kind:'flow', nodes:[{id:'a',label:'Actor'},{id:'b',label:'Store'}],
          edges:[{id:'late',from:'b',to:'a',label:'return',order:2,verdict:'wrong'},
                 {id:'early',from:'a',to:'b',label:'request',order:1,verdict:'confirmed'}]};
        const flow = toMermaid(view);
        assert(flow.startsWith('flowchart TD'));
        assert(flow.indexOf('n0 -->|"1"| n1') < flow.indexOf('n1 -->|"2"| n0'));
        assert(flow.includes('linkStyle 0 stroke:#16a34a'));
        assert(flow.includes('linkStyle 1 stroke:#dc2626,stroke-width:2px,stroke-dasharray:5 3'));
        assert.equal(toMermaid({...view,kind:'sequence'}),
          'sequenceDiagram\nparticipant n0 as Actor\nparticipant n1 as Store\nn0->>n1: request\nn1->>n0: return');
        assert.equal(view.edges[0].id, 'late');
        assert.equal(toMermaid({kind:'module',nodes:[],edges:[]}).split('\n')[0], 'flowchart LR');
    ''')


def test_legacy_flow_numbers_match_steps_even_after_missing_endpoints_are_dropped():
    _node(r'''
        const view = {kind:'flow',nodes:[{id:'a',label:'A'},{id:'b',label:'B'}],edges:[
          {id:'missing-to',from:'a',to:'gone',label:'lost'},
          {id:'valid',from:'a',to:'b',label:'call',verdict:'confirmed'},
          {id:'missing-from',from:'gone',to:'b',label:'lost too'},
          {id:'return',from:'b',to:'a',label:'return',verdict:'wrong'},
          {id:'explicit',from:'a',to:'b',label:'ordered',order:9}]};
        const before = JSON.stringify(view);
        assert.deepEqual(diagramSteps(view).map(s=>s.text),[
          '1. A → gone: lost','2. A → B: call','3. gone → B: lost too',
          '4. B → A: return','9. A → B: ordered']);
        const text = toMermaid(view);
        assert.deepEqual([...text.matchAll(/ -->\|"(\d+)"\| /g)].map(m=>m[1]),['2','4','9']);
        assert(text.includes('linkStyle 0 stroke:#16a34a'));
        assert(text.includes('linkStyle 1 stroke:#dc2626'));
        assert.equal(JSON.stringify(view),before);
    ''')


def test_unknown_omission_statuses_stay_open():
    _node(r'''
        const entries = [undefined,null,'','open','closed','future','resolved','dismissed']
          .map((status,i)=>({id:String(i),status}));
        const before = JSON.stringify(entries);
        const result = diagramMissing(entries);
        assert.deepEqual(result.open,entries.slice(0,6));
        assert.deepEqual(result.settled,entries.slice(6));
        assert.equal(result.open.length + result.settled.length,entries.length);
        assert.equal(JSON.stringify(entries),before);
    ''')


def test_finished_stage_progress_reports_transition_until_next_stage_starts():
    _node(r'''
        const stage = {stage:'review',engine:'claude',started_at:120,finished_at:240};
        const record = {status:'fix',created_at:0,stages:[stage]};
        assert.equal(diagramProgress(record,300),
          'Between stages: review finished, next starting (total 5 min)');
        assert.equal(diagramProgress(record,420),
          'Between stages: review finished, next starting (total 7 min)');
        assert.equal(diagramProgress({...record,created_at:undefined},300),
          'Between stages: review finished, next starting (total 3 min)');
        assert.equal(diagramProgress({...record,stages:[{...stage,started_at:0,finished_at:0}]},300),
          'Between stages: review finished, next starting (total 5 min)');
        assert.equal(diagramProgress({...record,stages:[stage,
          {stage:'fix',engine:'codex',started_at:300,finished_at:null}]},420),
          'Stage fix on codex running for 2 min (total 7 min)');
        for (const status of ['done','failed']) {
          assert.equal(diagramProgress({...record,status},300),'');
        }
    ''')


def test_overview_fallback_offers_group_drilldown_and_only_real_items_select():
    fallback = 'function DiagramFallback(' + SOURCE.split('function DiagramFallback(', 1)[1].split('function DiagramDrawing(', 1)[0]
    _node(r'''
        const h=(type,props,...children)=>({type,props,children}), Button='button';
        const flatten=value=>Array.isArray(value)?value.flatMap(flatten)
          :value&&typeof value==='object'?[value,...flatten(value.children)]:[];
        const text=value=>Array.isArray(value)?value.map(text).join('')
          :value&&typeof value==='object'?text(value.children):String(value??'');
        const selected=[];
        const view={kind:'module',groups:[{id:'g',label:'Core'},{id:'io',label:'IO'}],
          nodes:[{id:'a',label:'A',group:'g'},{id:'b',label:'B',group:'io'},
            {id:'real',label:'Real'}],edges:[{id:'e',from:'a',to:'b',label:'calls'}]};
    ''' + fallback + r'''
        const overview=diagramOverview(view);
        const tree=DiagramFallback({view:overview,onSelect:(...args)=>selected.push(args)});
        assert(text(tree).includes('Select a group to see its links.'));
        const buttons=flatten(tree).filter(n=>n.type==='button');
        assert.equal(buttons.length,overview.nodes.length);
        buttons.forEach(button=>button.props.onClick());
        assert.deepEqual(selected,overview.nodes.map(n=>['node',n.id]));
        assert(selected.some(([type,id])=>type==='node'&&id==='real'));
        selected.length=0;
        const realTree=DiagramFallback({view,onSelect:(...args)=>selected.push(args)});
        assert(!text(realTree).includes('Select a group to see its links.'));
        flatten(realTree).filter(n=>n.type==='button').forEach(button=>button.props.onClick());
        assert.deepEqual(selected,[['node','a'],['node','b'],['node','real'],['edge','e']]);
    ''')


def test_dashboard_mount_deep_link_overrides_storage_and_switch_remembers_choice():
    _node('''
        const vm = require("node:vm");
        const source = ''' + json.dumps(SOURCE) + ''';
        new vm.Script(source); // Parse the entire shipped file, not just the pure helpers.
        function mount(search, stored, blockedStorage=false) {
          let page;
          const writes = [], states = [];
          const storage = {getItem: () => {if(blockedStorage) throw Error('blocked'); return stored;},
            setItem: (key,value) => writes.push([key,value])};
          const window = {location:{search},localStorage:storage,
            __HERMES_PLUGIN_SDK__:{React:{createElement:(type,props,...children)=>({type,props,children})},
              hooks:{useState:(init)=>[typeof init==='function'?init():init,(value)=>states.push(value)]},components:{}},
            __HERMES_PLUGINS__:{register:(name,component)=>{page=component;}}};
          vm.runInNewContext(source,{window,URLSearchParams});
          return {tree:page(),writes,states};
        }
        let result = mount('?diagram=d-20261014-a1b2c3&profile=default','runs');
        assert.equal(result.tree.children[1].type.name,'DiagramsView');
        assert.equal(result.tree.children[1].props.initialId,'d-20261014-a1b2c3');
        result.tree.children[0].children[0][0].props.onClick();
        assert.equal(result.writes[0][0],'hermes-control.view');
        assert.equal(result.writes[0][1],'engines');
        assert.equal(mount('', 'diagrams').tree.children[1].type.name,'DiagramsView');
        assert.equal(mount('?diagram=invalid','runs').tree.children[1].type.name,'RunsView');
        assert.equal(mount('?diagram=d-20261014-a1b2c3x','engines').tree.children[1].type.name,'EnginesPage');
        assert.equal(mount('', 'unknown').tree.children[1].type.name,'EnginesPage');
        assert.equal(mount('', null,true).tree.children[1].type.name,'EnginesPage');
    ''', helpers=False)


def test_labels_cannot_inject_directives_or_entity_codes():
    _node(r'''
        const label = "%%{init:{'flowchart':{'htmlLabels':true}}}%%";
        for (const kind of ['module', 'flow', 'sequence']) {
          const text = toMermaid({kind, nodes:[{id:'a',label},{id:'b',label:'plugin_api.py <script>'}],
            groups:[{id:'g',label}], edges:[{from:'a',to:'b',label,order:1}]});
          assert(!text.includes('%%'));
          assert(!text.includes('{'));
          assert(!text.includes("'"));
          assert(text.includes('plugin_api.py ‹script›'));
          assert(!text.includes('#60;'));
        }
    ''')


def test_rendered_flowchart_id_extraction_is_anchored_and_supports_prefixes():
    _node(r'''
        assert.equal(diagramNodeId('hc-diagram-3-flowchart-n0-0'), 'n0');
        assert.equal(diagramNodeId('hc-diagram-20-flowchart-n12-345'), 'n12');
        assert.equal(diagramNodeId('flowchart-n1-2'), 'n1');
        for (const id of ['', null, 'n0', 'flowchart-worker-0', 'flowchart-n0-0-label',
                          'hc-diagram-3-flowchart-n0-x']) assert.equal(diagramNodeId(id), null);
    ''')


def test_drawing_effect_uses_generated_text_and_svg_fills_container():
    drawing = SOURCE.split('function DiagramDrawing(', 1)[1].split('function DiagramDetail(', 1)[0]
    assert 'const mermaidText = toMermaid(view, { showEdgeLabels })' in drawing
    assert '}, [mermaidText, theme])' in drawing
    assert 'querySelectorAll("g.node, rect.actor[name]")' in drawing
    assert 'diagramNodeId(element.id)' in drawing
    css = PAGE.with_name('style.css').read_text(encoding='utf-8')
    svg = re.search(r'\.hc-diagram-svg svg \{([^}]+)\}', css)[1]
    assert 'max-width: none !important' in svg
    assert 'transform-origin: 0 0' in svg
    assert 'width: 100%' not in svg
    assert re.search(r'\.hc-diagram-svg \{[^}]*overflow: hidden', css)
    assert 'cursor: pointer' in css


def test_stage_status_and_author_note_reflect_finished_work():
    detail = 'function DiagramDetail(' + SOURCE.split('function DiagramDetail(', 1)[1].split('function DiagramsView(', 1)[0]
    _node('''
        const h = (type, props, ...children) => ({type,props,children});
        const useState = (value) => [value, () => {}];
        const useCallback = (fn) => fn;
        const Card = 'card', CardHeader = 'header', CardTitle = 'title', CardContent = 'content';
        const DiagramStatus = 'status', DiagramCounts = 'counts', DiagramExplorer = 'drawing';
        const DiagramItem = 'item', DiagramEvidence = 'evidence', DiagramMissing = 'missing';
        const diagramFinal = (status) => ['done', 'failed'].includes(status);
        const lasted = () => 'duration';
    ''' + detail + '''
        const text = (value) => Array.isArray(value) ? value.map(text).join('')
          : value && typeof value === 'object' ? text(value.children) : String(value ?? '');
        function render(stage, author='codex', edited=0) {
          return text(DiagramDetail({diagram:{spec_author:author,counts:{claude_edited:edited},
            views:[],stages:[stage]},theme:'default'}));
        }
        for (const ok of [null, false, true]) {
          const result = render({stage:'review',ok,finished_at:null});
          assert(result.includes('pending'));
          assert(!result.includes('ok: false'));
          assert(!result.includes('ok: true'));
        }
        assert(render({ok:false,finished_at:123}).includes('ok: false'));
        assert(render({ok:true,finished_at:123}).includes('ok: true'));
        assert(!render({},'claude',0).includes('edited by claude after review'));
        assert(!render({},'codex',2).includes('edited by claude after review'));
        assert(render({},'claude',2).includes('edited by claude after review'));
    ''')


def test_drawing_clicks_and_keyboard_select_nodes_and_sequence_actors():
    drawing = 'function DiagramDrawing(' + SOURCE.split('function DiagramDrawing(', 1)[1].split('function DiagramDetail(', 1)[0]
    _node('''
        const effects = [], selected = [];
        let stateIndex = 0;
        const h = () => null, Button = 'button';
        const useState = () => [stateIndex++ === 0 ? {svg:'svg'} : '', () => {}];
        const useEffect = (effect) => effects.push(effect);
        function element(id, name=null, parent=null) {
          return {id,attrs:{name},listeners:{},closest:() => parent,
            getAttribute(key) {return this.attrs[key];},
            setAttribute(key,value) {this.attrs[key]=value;},
            addEventListener(key,fn) {this.listeners[key]=fn;},
            removeEventListener(key,fn) {assert.equal(this.listeners[key],fn); delete this.listeners[key];}};
        }
        const flow = element('hc-diagram-3-flowchart-n0-0');
        const unrelated = element('hc-diagram-3-flowchart-n99-0');
        const actorGroup = element('');
        const actor = element('', 'n1', actorGroup);
        let inserted = false;
        const container = {set innerHTML(value) {assert.equal(value,'svg'); inserted=true;},
          querySelectorAll(selector) {
          assert(inserted, 'SVG must be inserted before node listeners are attached');
          return selector === 'g.node, rect.actor[name]' ? [flow,unrelated,actor] : [];
        }};
        const useRef = () => ({current:container});
    ''' + drawing + '''
        DiagramDrawing({view:{kind:'sequence',nodes:[{id:'worker-a',label:'A'},
          {id:'worker-b',label:'B'}],edges:[]}, theme:'default',
          onSelect:(...args) => selected.push(args)});
        let cleanup;
        for (const effect of effects.slice(1,-1)) cleanup=effect(); // Insertion, then node wiring.
        assert.equal(flow.attrs.role,'button');
        assert.equal(flow.attrs.tabindex,'0');
        assert.equal(flow.attrs['aria-label'],'A');
        flow.listeners.click();
        let prevented = 0;
        actorGroup.listeners.keydown({key:'Enter',preventDefault:() => prevented++});
        actorGroup.listeners.keydown({key:' ',preventDefault:() => prevented++});
        actorGroup.listeners.keydown({key:'Escape',preventDefault:() => prevented++});
        assert.deepEqual(selected,[['node','worker-a'],['node','worker-b'],['node','worker-b']]);
        assert.equal(prevented,2);
        assert.deepEqual(unrelated.listeners,{});
        cleanup();
        assert.deepEqual(flow.listeners,{});
        assert.deepEqual(actorGroup.listeners,{});
    ''')


def test_overview_group_drilldown_and_summary_preserve_spec_and_marks():
    _node(r'''
        const view = {kind:'module', groups:[{id:'g1',label:'Core'},{id:'g2',label:'IO'}],
          nodes:Array.from({length:16}, (_,i)=>({id:'n'+i,label:'Node '+i,
            group:i<8?'g1':i<15?'g2':undefined,verdict:i===0?'wrong':'confirmed',
            claude_edited:i===1,evidence_invalid:i===2})),
          edges:[{id:'internal',from:'n0',to:'n1'},
            {id:'e1',from:'n0',to:'n8',verdict:'wrong'},
            {id:'e2',from:'n1',to:'n8',verdict:'confirmed'},
            {id:'reverse',from:'n8',to:'n0'},
            {id:'ungrouped',from:'n0',to:'n15'},
            {id:'far',from:'n9',to:'n10'}]};
        const before = JSON.stringify(view);
        assert.equal(diagramMode(view),'overview');
        assert.equal(diagramMode({...view,nodes:view.nodes.slice(0,15)}),'all');
        assert.equal(diagramMode({...view,groups:view.groups.slice(0,1)}),'all');
        assert.equal(diagramMode({...view,kind:'sequence'}),'all');
        const overview = diagramOverview(view);
        assert.equal(overview.nodes.length,3);
        assert.deepEqual(overview.groups,[]);
        const core = overview.nodes.find(n=>n.overviewGroup==='g1');
        assert.equal(core.label,'Core (8 nodes)');
        const longGroups = diagramOverview({...view,groups:[{...view.groups[0],label:'x'.repeat(80)},view.groups[1]]});
        assert(toMermaid(longGroups).includes('(8 nodes)'));
        assert.equal(core.verdict,'wrong');
        assert(core.claude_edited && core.evidence_invalid);
        assert.equal(overview.edges.length,3);
        assert.equal(overview.edges.find(e=>e.from===core.id && e.to!== 'n15').label,'2 links');
        assert(toMermaid(overview).includes('|"2 links"|'));
        assert(toMermaid(overview).startsWith('flowchart TD\n'));
        assert.equal(overview.edges.find(e=>e.to==='n15').label,'1 link');
        const collision = diagramOverview({...view,nodes:[...view.nodes,{id:core.id,label:'Collision'}]});
        assert.equal(new Set(collision.nodes.map(n=>n.id)).size,collision.nodes.length);
        const group = diagramGroup(view,'g1');
        assert.deepEqual(group.groups,view.groups.slice(0,1));
        assert.equal(group.nodes.length,10);
        assert.equal(group.nodes.filter(n=>n.neighbour).length,2);
        assert(group.nodes.find(n=>n.id==='n8').neighbour);
        assert(!group.edges.some(e=>e.id==='far'));
        assert(toMermaid(group).includes('class n8 neighbour'));
        assert.equal(JSON.stringify(view),before);
        assert.deepEqual(diagramSummary([]),{verdict:'unreviewed',claude_edited:false,evidence_invalid:false});
        assert.equal(diagramSummary([{verdict:'confirmed'},{verdict:'unverifiable'}]).verdict,'unverifiable');
        assert.equal(diagramSummary([{verdict:'confirmed'},{}]).verdict,'unreviewed');
        assert.equal(diagramSummary([{verdict:'confirmed'}]).verdict,'confirmed');
        assert.deepEqual(diagramGroup(view,'absent').nodes,[]);
    ''')


def test_step_list_edge_filters_missing_and_progress_are_backward_compatible():
    _node(r'''
        const view = {kind:'flow',nodes:[{id:'a',label:'A'},{id:'b',label:'B'}],edges:[
          {id:'e2',from:'b',to:'a',order:2,label:'return',verdict:'wrong',claude_edited:true},
          {id:'e1',from:'a',to:'b',order:1,label:'call',verdict:'unverifiable'},
          {id:'e3',from:'a',to:'b',order:3,label:'again'}]};
        assert.deepEqual(diagramSteps(view).map(s=>s.text),[
          '1. A → B: call','2. B → A: return','3. A → B: again']);
        assert.deepEqual(diagramSteps({...view,edges:[{from:'a',to:'b',label:'old'}]}).map(s=>s.text),['1. A → B: old']);
        assert.equal(diagramEdgeGroups(view,'All').length,2);
        for (const filter of ['wrong','unverifiable','unreviewed','claude-edited']) {
          const groups = diagramEdgeGroups(view,filter);
          assert.equal(groups.length,1);
          assert.equal(groups[0].edges.length,1);
          assert(['A','B'].includes(groups[0].label));
        }
        assert.deepEqual(diagramEdgeGroups({nodes:[],edges:[]},'All'),[]);
        const missing = [{label:'legacy'}, {id:'o',status:'open'},
          {id:'r',status:'resolved',by:['a']},{id:'d',status:'dismissed',reason:'out of scope'}];
        assert.deepEqual(diagramMissing(missing),{open:missing.slice(0,2),settled:missing.slice(2)});
        assert.deepEqual(diagramMissing(),{open:[],settled:[]});
        for (const stage of ['spec','review','review-rest','fix','re-review','re-review-rest','claude-fix']) {
          assert.equal(diagramProgress({status:stage,created_at:0,stages:[
            {stage:'spec',engine:'codex',started_at:0},{stage,engine:'claude',started_at:120}]},300),
            'Stage '+stage+' on claude running for 3 min (total 5 min)');
        }
        assert.equal(diagramProgress({status:'done'},300),'');
        assert.equal(diagramProgress({status:'failed'},300),'');
        assert.equal(diagramProgress({status:'spec'},300),'In progress: spec');
        assert(!diagramProgress({status:'spec',stages:[{stage:'spec',engine:'codex',started_at:400}]},300).includes('-'));
    ''')


def test_viewport_math_preserves_zoom_anchor_and_fits_large_legacy_svg():
    _node(r'''
        const fit = diagramFit(4854,3450,728,520);
        assert(fit.scale>0 && fit.scale<0.16);
        assert(Math.abs(fit.x*2+4854*fit.scale-728)<1e-8);
        assert(Math.abs(fit.y*2+3450*fit.scale-520)<1e-8);
        const zoom = diagramZoom(fit,1,364,260);
        assert.equal(zoom.scale,1);
        assert(Math.abs((364-fit.x)/fit.scale-(364-zoom.x)/zoom.scale)<1e-8);
        assert(Math.abs((260-fit.y)/fit.scale-(260-zoom.y)/zoom.scale)<1e-8);
        assert.equal(diagramZoom(zoom,100,0,0).scale,8);
        assert.equal(diagramZoom(zoom,0,0,0).scale,0.01);
        assert.equal(diagramFit(0,0,0,0).scale,1);
        assert.equal(diagramFit(2000,100,760,600).scale,0.38);
        assert.equal(diagramFit(100,2000,760,600).scale,0.3);
        assert.equal(diagramFit(100,100,760,600).scale,1);
    ''')


def test_theme_uses_neutral_nodes_transparent_clusters_and_border_verdicts():
    _node(r'''
        for (const theme of ['dark','light','default']) {
          const vars=diagramTheme(theme);
          assert.equal(vars.clusterBkg,'transparent');
          assert.notEqual(vars.primaryColor,vars.primaryTextColor);
          assert.equal(vars.darkMode,theme==='dark');
          const text=toMermaid({kind:'module',nodes:[{id:'a',label:'x'.repeat(80)}]}, {theme});
          assert(text.includes('n0["'+'x'.repeat(80)+'"]'));
          assert(text.includes('classDef confirmed stroke:#16a34a'));
          assert(!text.includes('fill:#dcfce7'));
          assert(!text.includes('fill:#fee2e2'));
        }
        assert.notEqual(diagramTheme('dark').primaryColor,diagramTheme('light').primaryColor);
    ''')


def test_new_controls_and_settled_omission_selection_are_present():
    assert 'theme: "base", themeVariables: diagramTheme(theme)' in SOURCE
    for label in ['Fit', '100%', 'Full screen', 'Show edge labels', 'Overview', 'All', 'Steps', 'Settled omissions']:
        assert '"' + label + '"' in SOURCE
    assert 'requestFullscreen' in SOURCE
    assert 'setPointerCapture' in SOURCE
    assert 'event.ctrlKey || event.metaKey' in SOURCE
    assert 'passive: false' in SOURCE
    assert 'ResizeObserver' in SOURCE
    assert 'diagramProgress(diagram)' in SOURCE


def test_viewport_controls_drag_wheel_resize_and_cleanup():
    drawing = 'function DiagramDrawing(' + SOURCE.split('function DiagramDrawing(', 1)[1].split('function DiagramEdgeList(', 1)[0]
    _node(r'''
        const effects=[], states=[], refs=[], cleanups=[], windowListeners={}, listeners={}, captures=new Set();
        let stateIndex=0, refIndex=0, effectIndex=0, resize, disconnected=false, fullscreen=0, insertions=0;
        const h=(type,props,...children)=>({type,props,children}), Button='button';
        const useState=init=>{const i=stateIndex++;
          if(!(i in states))states[i]=i===0?{svg:'svg'}:typeof init==='function'?init():init;
          return [states[i],value=>states[i]=value];};
        const useEffect=(fn,deps)=>{const i=effectIndex++, prior=effects[i];
          effects[i]={fn,deps,pending:!prior||deps.some((dep,j)=>!Object.is(dep,prior.deps[j]))};};
        const freshSvg=(width=4854,height=3450)=>({attrs:{width:'100%'},style:{maxWidth:'771px'},
          viewBox:{baseVal:{width,height}}});
        let svg=freshSvg();
        const svgHost={set innerHTML(value){assert.equal(value,states[0].svg);insertions++;svg=freshSvg();}};
        const viewport={style:{setProperty:(key,value)=>{viewport.style[key]=value;}},clientWidth:728,
          get clientHeight(){return Math.max(256,Math.min(560,parseFloat(this.style['--hc-diagram-height'])||520));},querySelector:()=>svg,
          querySelectorAll:()=>[], getBoundingClientRect:()=>({left:10,top:20}),
          setPointerCapture:id=>captures.add(id), hasPointerCapture:id=>captures.has(id),
          releasePointerCapture:id=>captures.delete(id),
          addEventListener:(name,fn,options)=>{listeners[name]=fn;if(name==='wheel')assert.equal(options.passive,false);},
          removeEventListener:(name,fn)=>{assert.equal(listeners[name],fn);delete listeners[name];}};
        const useRef=init=>{const i=refIndex++;return refs[i]||(refs[i]={current:init});};
        const frame={requestFullscreen:()=>{fullscreen++;return Promise.resolve();}};
        const window={innerHeight:800,addEventListener:(name,fn)=>windowListeners[name]=fn,
          removeEventListener:(name,fn)=>{assert.equal(windowListeners[name],fn);delete windowListeners[name];}};
        const document={fullscreenElement:null};
        class ResizeObserver {constructor(fn){resize=fn;}observe(target){assert.equal(target,viewport);}disconnect(){disconnected=true;}}
    ''' + drawing + r'''
        const props={view:{kind:'module',nodes:[],edges:[]},theme:'dark',onSelect:()=>{}};
        const mountRefs=tree=>{if(!tree||typeof tree!=='object')return;
          if(tree.props?.ref)tree.props.ref.current=tree.props.className==='hc-diagram-svg'?viewport:
            tree.props.className==='hc-diagram-drawing'?frame:svgHost;
          (tree.children||[]).forEach(mountRefs);};
        const render=()=>{stateIndex=refIndex=effectIndex=0;const tree=DiagramDrawing(props);mountRefs(tree);return tree;};
        // The Mermaid request is outside this DOM/control harness; drawing is already resolved.
        const flushEffects=()=>effects.forEach((effect,i)=>{if(i>0&&effect.pending){
          cleanups[i]?.();cleanups[i]=effect.fn();effect.pending=false;}});
        const cleanup=()=>cleanups.forEach((fn,i)=>{fn?.();cleanups[i]=undefined;});
        let tree=render();flushEffects();
        assert.equal(svg.style.width,'4854px'); assert.equal(svg.style.height,'3450px');
        assert(svg.style.transform.includes('scale(0.'));
        let buttons=tree.children[0].children;
        buttons.find(b=>b.children?.[0]==='100%').props.onClick();
        assert(svg.style.transform.endsWith('scale(1)'));
        const at100=svg.style.transform;
        buttons.find(b=>b.children?.[0]==='100%').props.onClick();
        assert.equal(svg.style.transform,at100,'Applying the same scale must be idempotent');
        const beforeRender=svg, beforeTransform=svg.style.transform, beforeInsertions=insertions;
        tree=render();flushEffects();buttons=tree.children[0].children;
        assert.equal(svg,beforeRender);assert.equal(svg.style.transform,beforeTransform);
        assert.equal(insertions,beforeInsertions,'Scale updates must not reinsert SVG markup');
        assert.equal(tree.children[0].children.find(b=>b.type==='span').children[0],'100%');
        // Simulate the browser regression: a replacement has only Mermaid's own attributes.
        svg=freshSvg();assert.equal(svg.attrs.width,'100%');assert.equal(svg.style.transform,undefined);
        const translated=beforeTransform.match(/translate\(([^p]+)px,([^p]+)px\)/);
        const expectedPlus=diagramZoom({scale:1,x:Number(translated[1]),y:Number(translated[2])},
          1.25,viewport.clientWidth/2,viewport.clientHeight/2);
        buttons.find(b=>b.children?.[0]==='+').props.onClick();
        assert.equal(svg.style.width,'4854px');assert.equal(svg.style.height,'3450px');
        assert(svg.style.transform.endsWith('scale(1.25)'));
        assert.equal(svg.style.transform,'translate('+expectedPlus.x+'px,'+expectedPlus.y+'px) scale(1.25)');
        const prior=svg.style.transform;
        let prevented=0,stopped=0;
        listeners.wheel({deltaY:100,preventDefault:()=>prevented++});
        assert.equal(svg.style.transform,prior); assert.equal(prevented,0);
        listeners.wheel({ctrlKey:true,deltaY:-100,clientX:100,clientY:120,preventDefault:()=>prevented++});
        assert.notEqual(svg.style.transform,prior); assert.equal(prevented,1);
        listeners.pointerdown({button:0,pointerId:1,clientX:10,clientY:10});
        svg=freshSvg();
        listeners.pointermove({pointerId:1,clientX:40,clientY:50});
        assert.equal(svg.style.width,'4854px');assert.equal(svg.style.height,'3450px');
        assert(captures.has(1));
        listeners.pointerup({pointerId:1});
        assert(!captures.has(1));
        const afterDrag=svg.style.transform;
        listeners.pointermove({pointerId:1,clientX:90,clientY:90});
        assert.equal(svg.style.transform,afterDrag); // Releasing must stop panning.
        listeners.click({preventDefault:()=>prevented++,stopPropagation:()=>stopped++});
        assert.equal(stopped,1);
        viewport.clientWidth=900;resize();assert.equal(svg.style.transform,afterDrag);
        svg=freshSvg();buttons.find(b=>b.children?.[0]==='Fit').props.onClick();
        assert.equal(svg.style.width,'4854px');assert.equal(svg.style.height,'3450px');
        const fitted=svg.style.transform;viewport.clientWidth=600;resize();assert.notEqual(svg.style.transform,fitted);
        buttons.find(b=>b.children?.[0]==='Full screen').props.onClick();assert.equal(fullscreen,1);
        cleanup();assert(disconnected);assert.deepEqual(listeners,{});assert.deepEqual(windowListeners,{});
        svg.viewBox.baseVal={width:2000,height:100}; viewport.clientWidth=728;
        const shortCleanup=effects.at(-1).fn();
        assert.equal(viewport.style['--hc-diagram-height'],'36.4px');
        assert.equal(viewport.clientHeight,256);
        assert(svg.style.transform.endsWith('scale(0.364)'));
        viewport.clientWidth=1000;resize();
        assert.equal(viewport.style['--hc-diagram-height'],'50px');
        assert(svg.style.transform.endsWith('scale(0.5)'));
        shortCleanup();
        states[0]={svg:'new drawing'};
        render();flushEffects();assert.equal(insertions,beforeInsertions+1);
        assert.equal(svg.style.width,'4854px');assert.equal(svg.style.height,'3450px');
        cleanup();
    ''')


def test_overview_clicks_breadcrumb_all_labels_and_missing_by_links():
    components = 'function DiagramEdgeList(' + SOURCE.split('function DiagramEdgeList(', 1)[1].split('function DiagramDetail(', 1)[0]
    counts = 'function DiagramCounts(' + SOURCE.split('function DiagramCounts(', 1)[1].split('function DiagramEvidence(', 1)[0]
    _node(r'''
        const h=(type,props,...children)=>({type,props,children});
        const Button='button', Card='card', CardHeader='header', CardTitle='title', CardContent='content', Badge='badge';
        const DiagramDrawing='drawing', DiagramEvidence='evidence';
        const states=[], selected=[];let index=0;
        const useState=init=>{const i=index++;if(!(i in states))states[i]=typeof init==='function'?init():init;
          return [states[i],value=>states[i]=value];};
        const flatten=value=>Array.isArray(value)?value.flatMap(flatten):value&&typeof value==='object'?[value,...flatten(value.children)]:[];
        const text=value=>Array.isArray(value)?value.map(text).join(''):value&&typeof value==='object'?text(value.children):String(value??'');
    ''' + components + counts + r'''
        const view={kind:'module',groups:[{id:'g',label:'Core'},{id:'io',label:'IO'}],
          nodes:Array.from({length:40},(_,i)=>({id:'n'+i,label:'x'.repeat(80),group:i<20?'g':'io'})),
          edges:[{id:'edge',from:'n0',to:'n21',label:'calls'}]};
        const render=()=>{index=0;return DiagramExplorer({view,theme:'dark',onSelect:(...args)=>selected.push(args)});};
        let tree=render();let drawing=flatten(tree).find(n=>n.type==='drawing');
        assert.equal(drawing.props.view.nodes.length,2);
        drawing.props.onSelect('node',drawing.props.view.nodes[0].id);
        tree=render();assert(text(tree).includes('Overview > Core'));
        drawing=flatten(tree).find(n=>n.type==='drawing');
        assert.equal(drawing.props.view.nodes.length,21);
        drawing.props.onSelect('node','n0');assert.deepEqual(selected,[['node','n0']]);
        flatten(tree).find(n=>n.type==='button'&&text(n)==='All').props.onClick();
        tree=render();assert.equal(flatten(tree).find(n=>n.type==='drawing').props.view.nodes.length,40);
        flatten(tree).find(n=>n.type==='input').props.onChange({target:{checked:true}});
        assert.equal(flatten(render()).find(n=>n.type==='drawing').props.showEdgeLabels,true);
        const missing=DiagramMissing({entries:[{label:'old'}, {id:'resolved',label:'Found',status:'resolved',by:['edge']},
          {id:'dismissed',label:'No need',status:'dismissed',reason:'out of scope'}],onSelect:(...args)=>selected.push(args)});
        const settled=flatten(missing).find(n=>n.type==='details');assert(!settled.props?.open);
        assert(text(missing).includes('old'));assert(text(settled).includes('Dismissed: out of scope'));
        flatten(settled).find(n=>n.type==='button').props.onClick();assert.deepEqual(selected.at(-1),['item','edge']);
        assert(text(DiagramCounts({counts:{missing_settled:2}})).includes('missing settled 2'));
        assert(!text(DiagramCounts({counts:{missing:1}})).includes('missing settled'));
        index=0;states.length=0;
        const edges=DiagramEdgeList({view,onSelect:()=>{}});
        for(const details of flatten(edges).filter(n=>n.type==='details'))assert(!details.props?.open);
        assert(text(edges).includes('Edges (1)'));
        assert(text(edges).includes('1. '+view.nodes[21].label+': calls'));
        index=0;states.length=0;
        const flow={kind:'flow',nodes:[],edges:[
          {id:'explicit',from:'b',to:'a',order:7,label:'explicit'},
          {id:'old1',from:'a',to:'b',label:'first',verdict:'wrong'},
          {id:'old2',from:'b',to:'a',label:'second'}]};
        const flowEdges=DiagramEdgeList({view:flow,onSelect:()=>{}});
        for(const {edge,number} of diagramNumberedEdges(flow))
          assert(text(flowEdges).includes(number+'. '+edge.to+': '+edge.label));
        flatten(flowEdges).find(n=>n.type==='button'&&text(n)==='wrong').props.onClick();
        index=0;
        assert(text(DiagramEdgeList({view:flow,onSelect:()=>{}})).includes('1. b: first'));
    ''')


def test_flow_overview_preserves_step_ranges_and_uses_per_view_cap():
    _node(r'''
        const view={kind:'flow',groups:[{id:'a',label:'A'},{id:'b',label:'B'}],
          nodes:Array.from({length:41},(_,i)=>({id:'n'+i,group:i<20?'a':'b'})),
          edges:[{id:'internal',from:'n0',to:'n1',order:1},
            {id:'reverse',from:'n20',to:'n0',order:2},
            ...[7,3,6,5].map(order=>({id:'e'+order,from:'n0',to:'n20',order}))]};
        assert.equal(diagramMode(view),'overview');
        // A full flow of 16-30 nodes drew at 21% in a live check; open on the
        // step-range overview like module views, never on a group-less overview.
        assert.equal(diagramMode({...view,nodes:view.nodes.slice(0,15)}),'all');
        for(const count of [16,30,31])
          assert.equal(diagramMode({...view,nodes:view.nodes.slice(0,count)}),'overview');
        assert.equal(diagramMode({...view,groups:[] }),'all');
        assert.equal(diagramMode({...view,groups:view.groups.slice(0,1)}),'all');
        const before=JSON.stringify(view);
        const overview=diagramOverview(view);
        assert.deepEqual(overview.edges.map(e=>e.label),['step 2','steps 3, 5-7']);
        assert(toMermaid(overview).includes('|"steps 3, 5-7"|'));
        assert.equal(JSON.stringify(view),before);
        const legacy={...view,edges:[{from:'absent',to:'n20'},
          {from:'n0',to:'n1'}, {from:'n0',to:'n20'},
          {from:'n0',to:'n20',order:5}]};
        assert.equal(diagramOverview(legacy).edges[0].label,'steps 3, 5');
        const long=diagramOverview({...view,edges:Array.from({length:20},(_,i)=>
          ({from:'n0',to:'n20',order:i*2+1}))}).edges[0].label;
        assert.equal(long,'steps 1, 3, 5, 7, 9, ...');
        assert.equal(long.length,24);
    ''')


def test_drawing_height_tracks_scaled_svg_with_css_limits():
    css=PAGE.with_name('style.css').read_text(encoding='utf-8')
    assert 'height: clamp(16rem, var(--hc-diagram-height, 16rem), 70vh)' in css
    assert 'clamp(20rem, 60vh, 42rem)' not in css
    assert 'setProperty("--hc-diagram-height",' in SOURCE
