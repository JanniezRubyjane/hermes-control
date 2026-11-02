"""Offline checks for the read-only recent run history."""

from test_desktop_plugin import _harness, _pure, source  # noqa: F401


def test_run_helpers(source):
    _pure(source, r'''
        const cases = [
            [{status:'running'}, 'running', 'hc-run-active', []],
            ...['waiting','paused','stopped'].map(status => [
                {status,status_reason:'status reason',summary:{verdict:'tests-passed'}}, status, 'hc-run-other', ['status reason']]),
            [{status:'failed',status_reason:'status reason',summary:{unproven_reason:'guard'}}, 'failed', 'hc-run-failed', ['status reason']],
            [{status:'failed',summary:{unproven_reason:'guard'}}, 'failed', 'hc-run-failed', ['guard']],
            [{policy:'read-only',status:'running'}, 'running', 'hc-run-active', []],
            ...['waiting','paused','stopped'].map(status => [
                {policy:'read-only',status,status_reason:'status reason'}, status, 'hc-run-other', ['status reason']]),
            ...['done','unproven'].map(status => [
                {policy:'read-only',status,summary:{verdict:'verify-failed'}}, 'review only', 'hc-run-neutral', []]),
            [{policy:'read-only',status:'failed',status_reason:'failed review'}, 'failed', 'hc-run-failed', ['failed review']],
            [{status:'done',policy:'write',summary:{verdict:'not-committed',unproven_reason:'no change'}}, 'no commit', 'hc-run-failed', ['no change']],
            ...[
                ['red-green-proven','proven (red-green)','hc-run-passed',[]],
                ['tests-passed','tests passed','hc-run-passed',[]],
                ...['red-passed','red-skipped','red-inconclusive'].map(v => [v,'tests passed, not red-green','hc-run-other',['proof reason']]),
                ['no-verify','no check','hc-run-other',[]],
                ['unproven','not proven','hc-run-failed',['guard']],
                ['verify-failed','check failed','hc-run-failed',['guard','exit 7','tail']],
                ['verify-blocked','check blocked','hc-run-failed',['guard','tail']],
            ].map(([verdict,word,dot,lines]) => [
                {status:'done',summary:{verdict,unproven_reason:'guard',proof:{reason:'proof reason'},verify:{exit_code:7,output_tail:'tail'}}},word,dot,lines]),
            [{summary:{verdict:'red-inconclusive',proof:{reason:null}}}, 'tests passed, not red-green','hc-run-other',[]],
            [{summary:{verdict:'verify-blocked',unproven_reason:'blocked'}}, 'check blocked','hc-run-failed',['blocked']],
            [{status:'done'},'done','hc-run-neutral',[]],
            [{status:'future'},'future','hc-run-neutral',[]],
            [{summary:{verdict:'future-verdict'}},'future-verdict','hc-run-neutral',[]],
            ...['constructor','__proto__'].map(verdict => [{summary:{verdict}},verdict,'hc-run-neutral',[]]),
            [null,'unknown','hc-run-neutral',[]],
        ];
        for (const [task, word, dot, lines] of cases) {
            assert.equal(verdictText(task), word, JSON.stringify(task));
            assert.equal(taskDot(task), dot, word);
            assert.deepEqual(taskExtraLines(task), lines, word);
        }
        assert.equal(verdictText({status:'running',summary:{verdict:'tests-passed'}}), 'running');
        for (const path of ['/tmp/x/demo-repo','/tmp/x/demo-repo/','demo-repo']) {
            assert.equal(repoName(path), 'demo-repo');
            assert(!repoName(path).includes('/'));
        }
        assert.equal(repoName(null), ''); assert.equal(repoName('/'), '');
        for (const [epoch, expected] of [[700,'5m ago'],[940,'1m ago'],[995,'5s ago'],
            [1000,'0s ago'],[1100,'0s ago'],[null,'unknown'],[NaN,'unknown']])
            assert.equal(runAge(epoch,1000), expected);
        assert.equal(runAge(1000-7200,1000),'2h ago');
        assert.equal(runAge(1000-172800,1000),'2d ago');
        const lines = Array.from({length:12},(_,i) => 'line-'+i);
        assert.equal(tailLines(lines.join('\n')), lines.slice(-8).join('\n'));
        assert.equal(tailLines(lines.join('\n')+'\n'), lines.slice(-8).join('\n'));
        assert.equal(tailLines('short\ntext'), 'short\ntext');
        assert.equal(tailLines(null), '');
    ''')


FIXTURE = r'''
    const opened = [], calls = [], intervals = [];
    let disposed = 0, failList = false;
    const runs = [
        {run_id:'finished/id',state:'finished',updated_at:Date.now()/1000-300,
         repo:'/tmp/x/demo-repo/',tasks:[{id:'passed-task',status:'done',engine:'fallback'}]},
        {run_id:'running-id',state:'running',repo:'/tmp/x/other',tasks:[]},
    ];
    const details = {
        'finished/id':{tasks:[
            {id:'passed-task',policy:'write',status:'done',summary:{verdict:'red-green-proven',diff_stat:'+12/-3',
                verify:{output_tail:'PASSED-TAIL-SECRET'}},
             attempts:[{engine:'old',model:'old-model'}, {engine:'actual',model:'actual-model',started_at:100,ended_at:161}]},
            {id:'failed-task',status:'failed',status_reason:'fallback reason',
             summary:{verdict:'verify-failed',unproven_reason:'<reason> & failure',verify:{exit_code:7,
                output_tail:Array.from({length:12},(_,i) => 'tail-'+i).join('\n')}},
             verify:{exit_code:99,output_tail:'WRONG-VERIFY'}},
            {id:'review-task',status:'done',policy:'read-only',verify:{output_tail:'REVIEW-TAIL-SECRET'}},
            {id:'old-task',status:'waiting',summary:null,attempts:null,verify:null},
            {id:'fallback-failure',status:'unproven',summary:{verdict:'red-inconclusive',proof:{reason:'red unavailable'},verify:{exit_code:0,output_tail:'PASSING-TAIL-SECRET'}}},
        ]},
        'running-id':{tasks:[{id:'active-task',status:'running'}]},
    };
    const c = context(async path => {
        if (path === '/status') return baseStatus;
        if (path === '/runs?limit=30') {
            if (failList) throw new Error('history unavailable');
            return {runs};
        }
        return details[decodeURIComponent(path.slice('/runs/'.length))];
    }, {openExternal:async url => {opened.push(url); return true;}});
    c.setInterval = (fn,ms) => {intervals.push({fn,ms}); return () => {disposed++;};};
    plugin.register(c);
    const page = mount(contribution(c,'runs-page').render());
'''


def test_runs_render_poll_cache_and_dashboard(source):
    _harness(source, FIXTURE + r'''
        let tree = page.render();
        assert(nodes(tree).some(n => n.props.className === 'hc-skeleton'));
        await settle(); tree = page.render();
        const rendered = text(tree);
        for (const word of ['finished', 'finished', 'running-id', 'passed-task','failed-task',
            'proven (red-green)','failed','review only','waiting','running','demo-repo',
            'actual · actual-model','+12/-3','1m 1s','fallback reason','exit 7','red unavailable','tests passed, not red-green'])
            assert(rendered.includes(word), word);
        assert(rendered.indexOf('demo-repo') < rendered.indexOf('other'));
        assert(!rendered.includes('/tmp/'));
        for (const secret of ['PASSED-TAIL-SECRET','REVIEW-TAIL-SECRET','WRONG-VERIFY','old-model','PASSING-TAIL-SECRET','exit 0'])
            assert(!rendered.includes(secret), secret);
        const taskState = nodes(tree).find(n => n.props.className === 'hc-run-state' && text(n).includes('passed-task'));
        assert(taskState.props.children.some(n => n?.type === 'span' && n.props.children === 'passed-task'));
        assert(taskState.props.children.some(n => n?.type === 'span' && n.props.children === 'proven (red-green)'));
        const tail = nodes(tree).find(n => n.type === 'pre' && text(n).includes('tail-11'));
        assert.equal(tail.props.children, Array.from({length:8},(_,i) => 'tail-'+(i+4)).join('\n'));
        assert(c.calls.includes('/runs?limit=30'));
        assert(c.calls.includes('/runs/finished%2Fid'));
        assert(c.requests.filter(r => r.path.startsWith('/runs')).every(r => r.options.timeoutMs === 10000));
        const poll = intervals.find(i => i.ms === 10000); assert(poll);
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/finished%2Fid').length,1);
        assert.equal(c.calls.filter(p => p === '/runs/running-id').length,2);
        failList = true; poll.fn(); await settle(); tree = page.render();
        assert(text(tree).includes('history unavailable')); assert(text(tree).includes('passed-task'));
        failList = false; button(tree,'Refresh').props.onClick(); await settle(); tree = page.render();
        assert(!text(tree).includes('history unavailable'));
        await button(tree,'Open in dashboard').props.onClick();
        assert.deepEqual(opened,['http://127.0.0.1:9119/engines']);
        page.unmount(); assert.equal(disposed,2);
    ''')


def test_runs_pending_details_and_late_answers(source):
    _harness(source, r'''
        const list = deferred(), detail = deferred();
        const c = context(path => path === '/status' ? Promise.resolve(baseStatus) :
            path === '/runs?limit=30' ? list.promise : detail.promise);
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render();
        list.resolve({runs:[{run_id:'pending-id',state:'finished',tasks:[{id:'pending-task',status:'done'}]}]});
        await settle();
        assert(text(page.render()).includes('pending-task')); assert(text(page.render()).includes('done'));
        const pendingTask = nodes(page.render()).find(n => n.props.className === 'hc-run-task');
        assert(nodes(pendingTask).some(n => n.props.className === 'hc-dot hc-run-neutral'));
        page.unmount(); const count = page.setterCalls;
        detail.resolve({tasks:[{id:'late',status:'failed'}]}); await settle();
        assert.equal(page.setterCalls,count);
    ''')
    _harness(source, r'''
        const pending = deferred();
        const c = context(path => path === '/status' ? Promise.resolve(baseStatus) : pending.promise);
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); page.unmount();
        const count = page.setterCalls;
        pending.resolve({runs:[{run_id:'late',state:'finished'}]}); await settle();
        assert.equal(page.setterCalls,count); assert(!c.calls.includes('/runs/late'));
    ''')


def test_runs_cache_changes_pending_and_identity(source):
    instrumented = source.replace(
        'const [details, setDetails] = useState({})',
        '''const [details, originalSetDetails] = useState({})
        const setDetails = next => originalSetDetails(old => {
            const result = next(old)
            globalThis.detailUpdates.push([old, result])
            return result
        })''',
    )
    _harness(instrumented, r'''
        const pending = deferred();
        const row = {run_id:'changing',state:'paused',updated_at:1,tasks:[]};
        const detail = {state:'paused',tasks:[]};
        let response = pending.promise;
        const updates = globalThis.detailUpdates = [];
        const c = context(path => path === '/status' ? Promise.resolve(baseStatus) :
            path === '/runs?limit=30' ? Promise.resolve({runs:[{...row}]}) : response);
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        const poll = c.intervals.find(i => i.ms === 10000);
        poll.fn(); await settle(); button(page.render(),'Refresh').props.onClick(); await settle();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,1,'reuse pending detail');
        pending.resolve(detail); await settle(); page.render();
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,1,'unchanged paused cache');
        assert(updates.length >= 2);
        assert(updates.slice(1).every(([old,next]) => old === next),'cached detail preserves state identity');
        response = Promise.resolve({...detail,state:'running'});
        row.state = 'running'; row.updated_at = 2;
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,2,'resumed run');
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,3,'running always refreshes');
        row.state = 'paused'; row.updated_at = 3;
        response = Promise.resolve(detail);
        poll.fn(); await settle(); page.render();
        row.updated_at = 4;
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,5,'updated paused run');
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,5,'unchanged paused run');
        row.state = 'stopped';
        response = Promise.resolve({...detail,state:'stopped'});
        poll.fn(); await settle(); page.render();
        assert.equal(c.calls.filter(p => p === '/runs/changing').length,6,'state change refetches detail');
        page.unmount();
    ''')


def test_runs_detail_errors_are_local_and_retried(source):
    _harness(source, r'''
        let fail = true;
        const c = context(async path => {
            if (path === '/status') return baseStatus;
            if (path === '/runs?limit=30') return {runs:['bad','good'].map(run_id => ({run_id,state:'finished'}))};
            if (path === '/runs/bad' && fail) throw new Error('detail unavailable');
            return {tasks:[]};
        });
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        let tree = page.render();
        const sections = nodes(tree).filter(n => n.type === 'section');
        assert(text(sections[0]).includes('detail unavailable'));
        assert(!text(sections[1]).includes('detail unavailable'));
        const message = nodes(sections[0]).find(n => n.props.children === 'detail unavailable');
        assert(message.props.className.includes('hc-run-secondary'));
        assert(!nodes(tree).some(n => n.props.className === 'hc-problem'));
        assert(sections[0].props.children.indexOf(message) > 0,'error under heading');
        fail = false;
        c.intervals.find(i => i.ms === 10000).fn(); await settle(); tree = page.render();
        assert(!text(tree).includes('detail unavailable'));
        assert.equal(c.calls.filter(p => p === '/runs/bad').length,2);
        assert.equal(c.calls.filter(p => p === '/runs/good').length,1);
        page.unmount();
    ''')


def test_runs_dashboard_error_survives_poll_and_respects_unmount(source):
    _harness(source, r'''
        const opening = deferred();
        const c = context(async path => path === '/status' ? baseStatus : {runs:[]},
            {openExternal:() => opening.promise});
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        const action = button(page.render(),'Open in dashboard').props.onClick();
        c.intervals.find(i => i.ms === 10000).fn(); await settle();
        opening.reject(new Error('dashboard unavailable')); await action; await settle();
        assert(text(page.render()).includes('dashboard unavailable'));
        assert.equal(nodes(page.render()).filter(n => n.props.className === 'hc-problem').length,0);
        const action2 = button(page.render(),'Open in dashboard').props.onClick();
        page.unmount(); const count = page.setterCalls;
        await action2; await settle(); assert.equal(page.setterCalls,count);
    ''')


def test_runs_running_pending_request_is_shared(source):
    _harness(source, r'''
        const pending = deferred();
        const c = context(path => path === '/status' ? Promise.resolve(baseStatus) :
            path === '/runs?limit=30' ? Promise.resolve({runs:[{run_id:'active',state:'running',updated_at:1}]}) : pending.promise);
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render()); page.render(); await settle();
        c.intervals.find(i => i.ms === 10000).fn(); await settle();
        button(page.render(),'Refresh').props.onClick(); await settle();
        assert.equal(c.calls.filter(p => p === '/runs/active').length,1);
        pending.resolve({tasks:[{id:'active-result',status:'done',summary:{verdict:'tests-passed'}}]});
        await settle(); assert(text(page.render()).includes('tests passed'));
        page.unmount();
    ''')
