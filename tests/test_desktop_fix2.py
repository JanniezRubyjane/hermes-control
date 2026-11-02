"""Regression checks for Runs page outcome and label clarity."""

from test_desktop_plugin import _harness, _pure, source  # noqa: F401


def test_worktree_repo_names(source):
    _pure(source, r'''
        const cases = [
            ['/tmp/x/hermes-control/.worktrees/int-desktop-runs', 'hermes-control \u00b7 int-desktop-runs'],
            ['/tmp/x/hermes-control/.worktrees/int-desktop-runs/', 'hermes-control \u00b7 int-desktop-runs'],
            ['/tmp/x/outer/.worktrees/inner/.worktrees/last', 'inner \u00b7 last'],
            ['/tmp/x/demo-repo', 'demo-repo'],
            ['/tmp/x/demo-repo/', 'demo-repo'],
            [null, ''], ['', ''], ['/', ''],
        ];
        for (const [path, expected] of cases) {
            assert.equal(repoName(path), expected);
            assert(!repoName(path).includes('/'));
        }
    ''')


def test_ended_run_outcome_dots(source):
    _pure(source, r'''
        const failed = {policy:'write',status:'done',summary:{verdict:'verify-failed'}};
        const passed = {policy:'write',status:'done',summary:{verdict:'tests-passed'}};
        const skipped = {policy:'write',status:'done',summary:{verdict:'red-skipped'}};
        const review = {policy:'read-only',status:'done'};
        for (const state of ['finished', 'done']) {
            assert.equal(runOutcomeDot(state, [failed, passed]), 'hc-run-failed');
            assert.equal(runOutcomeDot(state, [skipped, failed]), 'hc-run-failed');
            assert.equal(runOutcomeDot(state, [failed, skipped]), 'hc-run-failed');
            assert.equal(runOutcomeDot(state, [null, {}, passed]), 'hc-run-passed');
            assert.equal(runOutcomeDot(state, [null, failed]), 'hc-run-failed');
            assert.equal(runOutcomeDot(state, [passed, skipped]), 'hc-run-other');
            assert.equal(runOutcomeDot(state, [passed]), 'hc-run-passed');
            assert.equal(runOutcomeDot(state, [review, review]), 'hc-run-passed');
            assert.equal(runOutcomeDot(state, [{policy:'read-only',status:'failed'}]), 'hc-run-failed');
            assert.equal(runOutcomeDot(state, [review, {status:'running'}]), 'hc-run-passed');
        }
        for (const state of ['running', 'paused'])
            assert.equal(runOutcomeDot(state, [failed]), runDot(state));
        for (const tasks of [undefined, [], null, {}, 'tasks'])
            assert.equal(runOutcomeDot('finished', tasks), runDot('finished'));
    ''')


def test_verification_exit_code_is_not_repeated(source):
    _pure(source, r'''
        const result = (reason, exit_code) => taskResult({status:'done',policy:'write',
            summary:{verdict:'verify-failed',unproven_reason:reason,verify:{exit_code}}});
        const reason = 'verification failed (exit 1); x';
        assert.deepEqual(result(reason, 1).lines, [reason]);
        assert.deepEqual(result(reason, 2).lines, [reason, 'exit 2']);
        const reason10 = 'verification failed (exit 10); x';
        assert.deepEqual(result(reason10, 1).lines, [reason10, 'exit 1']);
        assert.deepEqual(result(reason10, 10).lines, [reason10]);
        assert.deepEqual(result('guard', 7).lines, ['guard', 'exit 7']);
    ''')


def test_runs_render_outcome_worktree_and_task_id(source):
    _harness(source, r'''
        const detail = deferred();
        const c = context(path => path === '/status' ? Promise.resolve(baseStatus) :
            path === '/runs?limit=30' ? Promise.resolve({runs:[{
                run_id:'finished-run',state:'finished',repo:'/tmp/x/demo-repo/.worktrees/int-demo',
                tasks:[{id:'check-task',status:'done'}],
            }]}) : detail.promise);
        plugin.register(c);
        const page = mount(contribution(c,'runs-page').render());
        page.render(); await settle();
        let tree = page.render();
        let heading = nodes(tree).find(n => n.props.className === 'hc-run-heading');
        assert(nodes(heading).some(n => n.props.className === 'hc-dot hc-run-passed'));
        detail.resolve({tasks:[{id:'check-task',policy:'write',status:'done',
            summary:{verdict:'verify-failed'}}]});
        await settle(); tree = page.render();
        heading = nodes(tree).find(n => n.props.className === 'hc-run-heading');
        assert(nodes(heading).some(n => n.props.className === 'hc-dot hc-run-failed'));
        assert(text(heading).includes('finished'));
        assert(text(tree).includes('demo-repo \u00b7 int-demo'));
        assert(!text(tree).includes('/tmp/'));
        const taskState = nodes(tree).find(n => n.props.className === 'hc-run-state' && text(n).includes('check-task'));
        assert(taskState.props.children.some(n => n?.type === 'span' &&
            n.props.className === 'hc-run-id' && n.props.children === 'check-task'));
        assert(styles.some(s => /\.hc-run-id\s*\{[^}]*font-weight:\s*600;\s*margin-right:\s*5px;\s*\}/.test(s.textContent)));
        page.unmount();
    ''')
