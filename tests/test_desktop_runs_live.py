"""Offline checks for live task activity on the Runs page."""

from test_desktop_plugin import _harness, _pure, source  # noqa: F401


def test_step_text(source):
    _pure(source, r'''
        const tool = detail => stepText({type:'tool',name:'shell',detail});
        assert.equal(tool("/bin/zsh -lc 'echo   hello'"), 'shell echo hello');
        assert.equal(tool('/bin/bash -lc "echo hello"'), 'shell echo hello');
        assert.equal(tool("/bin/sh -lc 'echo hello'"), 'shell echo hello');
        const cut = tool('/bin/zsh -lc "pwd; rg x\u2026');
        assert(cut.startsWith('shell pwd; rg x')); assert(cut.endsWith('\u2026'));
        assert.equal(tool("/bin/bash -lc 'ls -la'"), 'shell ls -la');
        assert.equal(stepText({type:'text',text:'"quoted"'}), '"quoted"');
        assert.equal(stepText({type:'text',text:"'quoted'"}), "'quoted'");
        assert.equal(tool('"quoted"'), 'shell "quoted"');
        assert.equal(tool({command:'pwd'}), 'shell [object Object]');
        assert.equal(stepText({type:'text',text:42}), '42');
        assert.equal(stepText({type:'text',text:0}), '0');
        const home = '/' + 'Users' + '/demo/x';
        const other = '/' + 'home' + '/other/y';
        const cleaned = tool(`cat ${home} ${other} ${home}`);
        assert.equal(cleaned, 'shell cat ~/x ~/y ~/x');
        assert(!cleaned.includes('demo')); assert(!cleaned.includes('other'));
        assert.equal(tool(''), 'shell');
        assert.equal(tool(null), 'shell');
        const long = tool('x'.repeat(300));
        assert.equal(long.length,120); assert(long.endsWith('\u2026'));
        assert.equal(stepText({type:'thinking',text:'private'}), '');
        assert.equal(stepText({type:'unknown',text:'private'}), '');
        assert.equal(stepText({type:'text',text:'hello\n world'}), 'hello world');
        assert.equal(stepText({type:'error',text:'bad\n command'}), 'bad command');
    ''')


def test_live_phase_and_tasks(source):
    _pure(source, r'''
        const task = {id:'active',status:'running',policy:'write',attempts:[
            {engine:'old',model:'old'},
            {engine:'codex',model:'model',started_at:100,result:null,steps:[
                {type:'text',text:'discarded'}, {type:'tool',name:'read',detail:''},
                {type:'thinking',text:'hidden'}, {type:'text',text:''},
                {type:'tool',name:'shell',detail:'false',is_error:true},
                {type:'error',text:'failure'},
            ]},
        ]};
        assert.equal(livePhase(task),'write');
        assert.equal(livePhase({...task,attempts:[{result:{}}]}),'check');
        assert.equal(livePhase({...task,policy:'read-only'}),'review');
        const detail = {state:'running',tasks:[task,{id:'done',status:'done'}]};
        assert.deepEqual(liveTasks(detail,175), [{id:'active',engine:'codex',model:'model',
            elapsed:'1m 15s',phase:'write',steps:[{text:'read',error:false},
                {text:'shell false',error:true},{text:'failure',error:true}]}]);
        assert.deepEqual(liveTasks({...detail,state:'finished'},175), []);
        assert.deepEqual(liveTasks(undefined,175), []);
        assert.deepEqual(liveTasks({state:'running',tasks:[{status:'waiting'}]},175), []);
        assert.equal(liveTasks(detail,50)[0].elapsed,'0s');
        assert.equal(liveTasks({state:'running',tasks:[{...task,attempts:[]}]},175)[0].elapsed,'');
        assert.deepEqual(liveTasks({state:'running',tasks:[task,{...task,id:'second'}]},175)
            .map(t => t.id), ['active','second']);
    ''')


LIVE_FIXTURE = r'''
    let state = 'running', disposed = 0, pending = null, detailFailure = false;
    const home = '/' + 'Users' + '/demo/x';
    const c = context(async path => {
        if (path === '/status') return baseStatus;
        if (path === '/runs?limit=30') {
            if (pending) return pending.promise;
            return {runs:[{run_id:'active-run',state}]};
        }
        if (detailFailure) throw new Error('Live detail unavailable');
        return {state,tasks:[{id:'live-task',status:state === 'running' ? 'running' : 'done',
            policy:'write',attempts:[{engine:'codex',model:'sample-model',started_at:100,
                result:null,steps:[{type:'tool',name:'shell',detail:`cat ${home}`},
                    {type:'text',text:'working'}, {type:'error',text:'retry'}]}]}]};
    });
    c.setInterval = (fn,ms) => {c.intervals.push({fn,ms}); return () => {disposed++;};};
    plugin.register(c);
    const page = mount(contribution(c,'runs-page').render());
    page.render(); await settle();
    const fast = c.intervals.find(i => i.ms === 3000); assert(fast);
    const count = () => c.calls.filter(p => p === '/runs?limit=30').length;
'''


def test_live_render_poll_and_finish(source):
    _harness(source, LIVE_FIXTURE + r'''
        let tree = page.render();
        const panel = nodes(tree).find(n => n.type === 'section' && n.props.className === 'hc-live');
        assert(panel); assert(text(panel).includes('live-task'));
        assert(text(panel).includes('codex \u00b7 sample-model'));
        assert(nodes(panel).some(n => n.props.className === 'hc-dot hc-run-active'));
        const liveState = nodes(panel).find(n => n.props.className === 'hc-run-state');
        assert(liveState);
        assert.deepEqual(liveState.props.children.map(n => n.props.className),
            ['hc-dot hc-run-active', 'hc-run-id']);
        const taskState = nodes(tree).find(n => n.props.className === 'hc-run-task-row')
            .props.children.find(n => n?.props.className === 'hc-run-state');
        assert.equal(liveState.props.className, taskState.props.className);
        assert(styles.some(s => /\.hc-run-state\s*\{[^}]*gap:\s*7px\s*;/.test(s.textContent)));
        const phase = nodes(panel).find(n => n.props.className === 'hc-live-current');
        assert.equal(text(phase),'write');
        assert.equal(text(nodes(panel).find(n => n.props.children?.includes?.(phase))),
            'write  \u2192  check  \u2192  done');
        const steps = nodes(panel).filter(n => n.props.className?.split(' ').includes('hc-live-step'));
        assert.deepEqual(steps.map(text), ['shell cat ~/x','working','retry']);
        assert(steps[2].props.className.includes('hc-live-error'));
        assert(!text(tree).includes(home)); assert(!text(tree).includes('demo'));
        assert(nodes(tree).indexOf(panel) < nodes(tree).findIndex(n => n.props.className === 'hc-run'));
        const before = count(); fast.fn(); await settle(); assert.equal(count(),before+1);
        state = 'finished'; fast.fn(); await settle(); tree = page.render();
        assert(!nodes(tree).some(n => n.props.className === 'hc-live'));
        const finished = count(); fast.fn(); await settle(); assert.equal(count(),finished);
        page.unmount(); assert.equal(disposed,2);
    ''')


def test_running_task_hides_worker_duration_until_finished(source):
    _harness(source, r'''
        Date.now = () => 175000;
        let state = 'running';
        const task = {id:'checking-task',status:'running',policy:'write',attempts:[{
            engine:'codex',started_at:100,ended_at:161,result:{},steps:[],
        }]};
        const c = context(async path => path === '/status' ? baseStatus :
            path === '/runs?limit=30' ? {runs:[{run_id:'active-run',state}]} : {state,tasks:[task]});
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        let tree = page.render();
        const panel = nodes(tree).find(n => n.props.className === 'hc-live');
        assert(text(panel).includes('1m 15s'));
        assert.equal(text(nodes(panel).find(n => n.props.className === 'hc-live-current')), 'check');
        const row = () => nodes(tree).find(n => n.props.className === 'hc-run-task-row');
        assert(!text(row()).includes('1m 1s'));
        state = 'finished'; task.status = 'done';
        c.intervals.find(i => i.ms === 3000).fn(); await settle(); tree = page.render();
        assert(!nodes(tree).some(n => n.props.className === 'hc-live'));
        assert(text(row()).includes('1m 1s'));
        page.unmount();
    ''')


def test_fast_poll_idle_and_in_flight(source):
    _harness(source, r'''
        const c = context(async path => path === '/status' ? baseStatus : {runs:[]});
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        const before = c.calls.length;
        c.intervals.find(i => i.ms === 3000).fn(); await settle();
        assert.equal(c.calls.length,before);
        assert(!nodes(page.render()).some(n => n.props.className === 'hc-live'));
        page.unmount();
    ''')
    _harness(source, LIVE_FIXTURE + r'''
        pending = deferred();
        fast.fn(); const before = count(); fast.fn(); await settle();
        assert.equal(count(),before);
        pending.resolve({runs:[{run_id:'active-run',state:'running'}]});
        await settle(); pending = null;
        fast.fn(); await settle(); assert.equal(count(),before+1);
        page.unmount(); assert.equal(disposed,2);
    ''')


def test_live_detail_failure_and_recovery(source):
    _harness(source, LIVE_FIXTURE + r'''
        const hasLive = tree => nodes(tree).some(n => n.props.className === 'hc-live');
        assert(hasLive(page.render()));
        detailFailure = true;
        fast.fn(); await settle();
        let tree = page.render();
        assert(!hasLive(tree));
        assert(text(tree).includes('Live detail unavailable'));
        detailFailure = false;
        fast.fn(); await settle();
        tree = page.render();
        assert(hasLive(tree));
        assert(!text(tree).includes('Live detail unavailable'));
        page.unmount();
    ''')


def test_fast_poll_resumes_after_list_failure(source):
    _harness(source, LIVE_FIXTURE + r'''
        pending = deferred();
        const before = count();
        fast.fn(); assert.equal(count(),before+1);
        pending.reject(new Error('Runs unavailable'));
        await settle(); pending = null;
        assert(text(page.render()).includes('Runs unavailable'));
        fast.fn(); await settle(); assert.equal(count(),before+2);
        assert(!text(page.render()).includes('Runs unavailable'));
        page.unmount();
    ''')


def test_fast_poll_resumes_after_superseded_load(source):
    _harness(source, LIVE_FIXTURE + r'''
        const first = deferred(), second = deferred();
        const before = count();
        pending = first;
        fast.fn(); assert.equal(count(),before+1);
        pending = second;
        c.intervals.find(i => i.ms === 10000).fn();
        assert.equal(count(),before+2);
        first.resolve({runs:[]}); await settle();
        fast.fn(); await settle(); assert.equal(count(),before+2);
        second.resolve({runs:[{run_id:'active-run',state:'running'}]});
        await settle(); pending = null;
        fast.fn(); await settle(); assert.equal(count(),before+3);
        assert(nodes(page.render()).some(n => n.props.className === 'hc-live'));
        page.unmount();
    ''')


def test_live_review_and_check_render_order(source):
    _harness(source, r'''
        const c = context(async path => path === '/status' ? baseStatus :
            path === '/runs?limit=30' ? {runs:[{run_id:'first',state:'running'},
                {run_id:'second',state:'running'}]} : {state:'running',tasks:[{
                    id:path, status:'running',policy:path.endsWith('first') ? 'read-only' : 'write',
                    attempts:[{result:{},steps:[]}],
                }]});
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        const panels = nodes(page.render()).filter(n => n.props.className === 'hc-live');
        assert.equal(panels.length,2);
        assert(text(panels[0]).includes('/runs/first'));
        assert(text(panels[1]).includes('/runs/second'));
        assert.equal(text(nodes(panels[0]).find(n => n.props.className === 'hc-live-current')),'review');
        assert(!text(panels[0]).includes('write')); assert(text(panels[0]).includes('done'));
        assert.equal(text(nodes(panels[1]).find(n => n.props.className === 'hc-live-current')),'check');
        page.unmount();
    ''')
