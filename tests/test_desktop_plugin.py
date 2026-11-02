"""Offline contract, lifecycle, and rendering checks for the desktop package."""

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PLUGIN = Path(__file__).resolve().parents[1] / "hermes_control/desktop/plugin.js"
UNSCOPED_TIMER = re.compile(r"(?<!ctx\.)\b(?!ctx\.)(?:\w+\.)?(?:setInterval|setTimeout)\s*\(")


@pytest.fixture
def source():
    assert PLUGIN.is_file(), "The unified package must ship desktop/plugin.js"
    return PLUGIN.read_text(encoding="utf-8")


def _node(program):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable; static desktop checks still run")
    result = subprocess.run(
        [node, "--input-type=module", "-e", "import assert from 'node:assert/strict';\n" + program],
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr


def _pure(source, program):
    pure = source.split("// Pure helpers.", 1)[1].split("// End pure helpers.", 1)[0]
    _node(pure + "\n" + program)


def test_public_file_safety_and_imports(source):
    for text in (source, Path(__file__).read_text(encoding="utf-8")):
        assert not re.search(r"[\u0E00-\u0E7F]", text)
        assert not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        assert "/" + "Users" + "/" not in text
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|dangerouslySetInnerHTML", source)
    assert not re.search(r"\bwindow\s*\.\s*(?:setInterval|setTimeout)", source)
    assert not UNSCOPED_TIMER.search(source)
    assert not re.search(r"(?<!ctx\.)\baddEventListener\s*\(", source)
    assert re.findall(r"https?://[^'\"\s]+", source) == ["http://127.0.0.1:9119/engines"]
    assert "account" not in source.lower()
    assert "fontUrl" not in source
    assert re.search(r"defaultEnabled:\s*false", source)
    assert re.search(r"id:\s*['\"]hermes_control['\"]", source)
    outside_theme = re.sub(r"// Theme\.[\s\S]*?// End theme\.", "", source)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", outside_theme)
    assert not re.search(r"\b(?:rgba?|hsla?)\s*\(", outside_theme)
    imports = re.findall(r"^import .*", source, re.M)
    assert len(imports) == 3
    assert {re.fullmatch(r"import \{ [\w, ]+ \} from ['\"]([^'\"]+)['\"]", line)[1]
            for line in imports} == {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
    assert not re.search(r"\bimport\s*\(", source)
    pure = source.split("// Pure helpers.", 1)[1].split("// End pure helpers.", 1)[0]
    assert not re.search(r"\b(?:import|ctx|React|document|window|jsx|jsxs)\b", pure)


def test_unscoped_timer_regex():
    for sample in ["setInterval(", "setTimeout (", "globalThis.setTimeout(",
                   "self.setInterval(", "window.setTimeout("]:
        assert UNSCOPED_TIMER.search(sample), sample
    for sample in ["ctx.setInterval(", "ctx.setTimeout("]:
        assert not UNSCOPED_TIMER.search(sample), sample


def test_styles_use_host_tokens_and_readable_text(source):
    css = source.split("const styles = `", 1)[1].split("`", 1)[0]
    assert ".hc-popover.hc-popover" in css
    assert re.search(r"\.hc-popover\.hc-popover\s*\{[^}]*padding:0", css)
    assert ':root[data-hermes-theme="hermes-control"]' in css
    assert 'font-family:ui-serif,"New York",Georgia,serif' in css
    assert "--ui-text-tertiary" not in css
    assert "--ui-text-quaternary" not in css
    assert "--ui-blue" not in css
    assert "text-transform:uppercase" not in css
    assert set(re.findall(r"\.([a-z][\w-]*)", css)) <= set(re.findall(r"\.(hc-[\w-]+)", css))
    assert not re.search(r"(?:color|background):\s*(?!var\(|repeating-linear-gradient\(|inherit)[a-z#]", css)
    assert "white-space:nowrap" in css
    assert "color:inherit; font:inherit" in css
    assert "stroke: 'currentColor'" in source


def test_duration_and_formatting_helpers(source):
    _pure(source, r'''
        for (const [n, expected] of [[0,'0s'],[59,'59s'],[60,'1m 0s'],[3599,'59m 59s'],
            [3600,'1h 0m'],[86400,'1d 0h'],[190801,'2d 5h'],[-8,'now'],[null,'now'],[NaN,'now']]) {
            assert.equal(formatDuration(n), expected);
        }
        assert.equal(resetText(null, 900), 'now');
        assert.equal(resetText(700, 900), 'now');
        assert.equal(clampPercent(-17), 0);
        assert.equal(clampPercent(117), 100);
        assert.equal(clampPercent(37), 37);
        assert.equal(clampPercent(null), 0);
        const windows = [{label:'low',used_percent:17},{label:'missing',used_percent:null},
            {label:'high',used_percent:91},{label:'middle',used_percent:43}];
        assert.deepEqual(fullestWindows(windows).map(w => w.label), ['high','middle']);
        assert.equal(windows[0].label, 'low');
        assert.deepEqual(fullestWindows(null), []);
        assert.equal(workState({paused:true,blocked:true,held_back:'reason'}), 'Paused');
        assert.equal(workState({blocked:true,held_back:'reason'}), 'Blocked');
        assert.equal(workState({held_back:''}), 'Holding work back');
        assert.equal(workState({held_back:null}), 'Taking work');
        assert.equal(barTone({report_only:true,cap:67}, 91), 'hc-report');
        assert.equal(usageWord({report_only:true,cap:67},91), '');
        assert.equal(barTone({cap:67}, 91), 'hc-over');
        assert.equal(barTone({cap:67}, 53), 'hc-near');
        assert.equal(barTone({cap:67}, null), 'hc-normal');
        assert.equal(barTone({cap:67}, 17), 'hc-normal');
    ''')


def test_chip_all_kinds_and_missing_values(source):
    _pure(source, r'''
        const text = (chip, proof = null) => chipText({chip,proof}, 900);
        assert.equal(chipText(null, 900), 'Hermes Control');
        assert.equal(chipText(null, 900, true), 'Hermes Control · offline');
        assert.equal(chipText({error:true}, 900), 'Hermes Control · offline');
        assert.equal(text({kind:'running',engine:'sample-engine',started_at:803}), 'sample-engine running · 1m 37s');
        assert.equal(text({kind:'running',engine:null,started_at:883}), 'Task running · 17s');
        assert.equal(text({kind:'running',engine:'sample-engine',started_at:null}), 'sample-engine running');
        assert.equal(text({kind:'running',engine:null,started_at:null}), 'Task running');
        assert.equal(text({kind:'running',started_at:999}), 'Task running · 0s');
        assert.equal(text({kind:'blocked',engine:'sample-engine',resets_at:1237}), 'sample-engine blocked · resets 5m 37s');
        assert.equal(text({kind:'blocked',engine:'sample-engine',resets_at:null}), 'sample-engine blocked');
        assert.equal(text({kind:'blocked',engine:'sample-engine',resets_at:100}), 'sample-engine blocked · resets now');
        const proof = {not_proven:['synthetic-a','synthetic-b']};
        assert.equal(text({kind:'unproven',task_id:'synthetic-a'}, proof), '2 not proven · synthetic-a');
        assert.equal(text({kind:'unproven',task_id:null}, proof), '2 not proven');
        assert.equal(text({kind:'unproven',task_id:'synthetic-a'}), 'Not proven');
        assert.equal(text({kind:'unproven',task_id:'synthetic-a'},{not_proven:[]}), 'Not proven');
        assert.equal(text({kind:'near',engine:'sample-engine',percent:56.7}), 'sample-engine 57% · near cap');
        assert.equal(text({kind:'idle',engine:'sample-engine',percent:16.6}), 'Idle · sample-engine 17%');
        assert.equal(text({kind:'idle',engine:null,percent:17}), 'Idle');
        assert.equal(text({kind:'idle',engine:'sample-engine',percent:null}), 'Idle');
        assert.equal(text({kind:'idle',engine:'sample-engine',percent:0}), 'Idle · sample-engine 0%');
    ''')


def test_proof_and_notification_text(source):
    _pure(source, r'''
        assert.equal(proofText(null), '');
        assert.equal(proofText({written:0,proven:0,red_green:0}), 'Reviews only');
        assert.equal(proofText({written:7,proven:7,red_green:0}), '7/7 proven');
        const proof = {written:7,proven:5,red_green:3,not_proven:['synthetic-a','synthetic-b']};
        assert.equal(proofText(proof), '5/7 proven · 3 red-green');
        const run = {state:'finished',task_ids:['synthetic-a','synthetic-b','synthetic-review']};
        assert.equal(notifyText({last_run:run,proof}), 'Run finished · 5/7 proven · 3 red-green · not proven: synthetic-a, synthetic-b');
        assert.equal(notifyText({last_run:run,proof:null}), 'Run finished · 3 tasks');
        assert.equal(notifyText({last_run:{state:'finished'},proof:null}), 'Run finished · 0 tasks');
        assert.equal(notifyText({last_run:run,proof:{written:0,not_proven:[]}}), 'Run finished · 3 tasks');
        assert.equal(notifyText({last_run:{...run,state:'paused'},proof:{written:7,proven:7,red_green:0,not_proven:[]}}), 'Run paused · 7/7 proven');
        assert.equal(notifyText({last_run:{...run,state:'stopped'},proof:null}), 'Run stopped · 3 tasks');
    ''')


# A small hook runner exercises the actual components without React dependencies.
HARNESS = r'''
const navigation = [], styles = [];
let active = null;
const react = {
    useState(initial) {
        const instance = active, index = instance.index++;
        if (!instance.slots[index]) instance.slots[index] = {value: typeof initial === 'function' ? initial() : initial};
        return [instance.slots[index].value, value => {
            instance.setterCalls++;
            const slot = instance.slots[index];
            slot.value = typeof value === 'function' ? value(slot.value) : value;
        }];
    },
    useRef(value) {
        const instance = active, index = instance.index++;
        return instance.slots[index] ||= {current:value};
    },
    useEffect(effect, deps) {
        const instance = active, index = instance.index++;
        const old = instance.slots[index];
        if (old && deps && old.deps?.every((value,i) => Object.is(value,deps[i]))) return;
        instance.effects.push(() => {
            old?.cleanup?.();
            instance.slots[index] = {deps,cleanup:effect()};
        });
    },
};
function jsx(type, props, key) { return {type,props:props || {},key}; }
globalThis.__hc_stubs = {
    '@hermes/plugin-sdk': {
        ROUTES_AREA:'routes', SIDEBAR_NAV_AREA:'sidebar.nav', STATUSBAR_AREAS:{right:'statusBar.right'},
        PALETTE_AREA:'palette', THEMES_AREA:'themes', host:{navigate:path => navigation.push(path)},
    },
    react,
    'react/jsx-runtime':{jsx,jsxs:jsx},
};
globalThis.document = {
    createElement(tag) {
        assert.equal(tag,'style');
        return {tag,textContent:'',removed:false,remove() { this.removed = true; }};
    },
    head:{appendChild(element) { styles.push(element); }},
};
function context(answer = () => new Promise(() => {}), os = {}) {
    const c = {contributions:[],intervals:[],disposals:[],calls:[],requests:[],os,
        register(item) { this.contributions.push(item); return () => {}; },
        registerMany(items) { items.forEach(item => this.register(item)); return () => {}; },
        setInterval(fn,ms) { this.intervals.push({fn,ms}); return () => {}; },
        onDispose(fn) { this.disposals.push(fn); },
        rest(path, options) {
            this.calls.push(path); this.requests.push({path,options}); return answer(path, options);
        },
    };
    return c;
}
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes,no) => {resolve = yes; reject = no;});
    return {promise,resolve,reject};
}
async function settle() { for (let i=0;i<12;i++) await Promise.resolve(); }
function mount(root) {
    const instances = new Map();
    return {
        get setterCalls() { return [...instances.values()].reduce((sum, instance) => sum + instance.setterCalls, 0); },
        render() {
            function visit(node,path) {
                if (Array.isArray(node)) return node.map((n,i) => visit(n,path+'.'+i));
                if (!node || typeof node !== 'object') return node;
                if (typeof node.type === 'function') {
                    let instance = instances.get(path);
                    if (!instance) { instance = {slots:[],setterCalls:0}; instances.set(path,instance); }
                    instance.index = 0; instance.effects = []; active = instance;
                    const result = node.type(node.props);
                    active = null;
                    return visit(result,path+'.child');
                }
                return {...node,props:{...node.props,children:visit(node.props.children,path+'.children')}};
            }
            const tree = visit(root,'root');
            for (const instance of instances.values()) instance.effects.forEach(fn => fn());
            return tree;
        },
        unmount() { for (const instance of instances.values()) for (const slot of instance.slots) slot?.cleanup?.(); },
    };
}
function nodes(tree) {
    if (Array.isArray(tree)) return tree.flatMap(nodes);
    if (!tree || typeof tree !== 'object') return [];
    return [tree,...nodes(tree.props.children)];
}
function text(tree) {
    if (Array.isArray(tree)) return tree.map(text).join(' ');
    if (tree == null || typeof tree === 'boolean') return '';
    return typeof tree === 'object' ? text(tree.props.children) : String(tree);
}
function button(tree,label) {
    const result = nodes(tree).find(n => n.type === 'button' && text(n) === label);
    assert(result, 'Missing button: '+label); return result;
}
function contribution(c,id) { return c.contributions.find(item => item.id === id); }
const baseStatus = {chip:{kind:'idle',engine:null,percent:null},engines:[],last_run:null,proof:null,problem:null};
'''


def _harness(source, program):
    replaced = re.sub(
        r"^import \{ ([\w, ]+) \} from ['\"]([^'\"]+)['\"]$",
        lambda m: f"const {{ {m[1]} }} = globalThis.__hc_stubs[{json.dumps(m[2])}]",
        source, flags=re.M,
    )
    encoded = base64.b64encode(replaced.encode()).decode()
    _node(HARNESS + f"\nconst {{default:plugin}} = await import('data:text/javascript;base64,{encoded}');\n" + program)


def test_registration_theme_and_disposal(source):
    expected_light = dict(zip(
        "background foreground card cardForeground muted mutedForeground popover popoverForeground primary primaryForeground secondary secondaryForeground accent accentForeground border input ring midground destructive destructiveForeground sidebarBackground sidebarBorder userBubble userBubbleBorder".split(),
        "#f5f4ed #141413 #f5f4ed #141413 #efede4 #5e5d59 #faf9f5 #141413 #c96442 #faf9f5 #e8e6dc #3d3d3a #f0eee6 #141413 #e8e6dc #e8e6dc #c96442 #c96442 #b53333 #faf9f5 #efede4 #e8e6dc #efe9df #e8e6dc".split(), strict=True,
    ))
    expected_dark = dict(zip(
        expected_light,
        "#1a1918 #ece9df #262624 #ece9df #262624 #b0aea5 #30302e #ece9df #d97757 #141413 #30302e #e8e6dc #30302e #ece9df #30302e #30302e #d97757 #d97757 #e2715f #141413 #141413 #262624 #30302e #3d3d3a".split(), strict=True,
    ))
    _harness(source, "const light = " + json.dumps(expected_light) + ";\nconst dark = " + json.dumps(expected_dark) + r''';
        assert.equal(plugin.id,'hermes_control');
        assert.equal(plugin.name,'Hermes Control');
        assert.equal(plugin.defaultEnabled,false);
        const c = context(); plugin.register(c);
        assert.deepEqual(c.contributions.map(item => item.area).sort(),
            ['themes','statusBar.right','routes','routes','sidebar.nav','sidebar.nav','palette','palette'].sort());
        const theme = contribution(c,'theme').data;
        assert.equal(theme.name,'hermes-control'); assert.equal(theme.label,'Hermes Control');
        assert(theme.description); assert(theme.typography.fontSans); assert(theme.typography.fontMono);
        assert(!JSON.stringify(theme).includes('fontUrl'));
        assert.deepEqual(theme.colors,light); assert.deepEqual(theme.darkColors,dark);
        const chip = contribution(c,'chip');
        assert.equal(chip.render,undefined); assert.equal(chip.data.id,'hermes-control-chip');
        assert.equal(chip.data.variant,'menu'); assert.equal(chip.data.toggleLabel,'Hermes Control');
        assert.equal(chip.data.menuAlign,'end'); assert.equal(chip.data.menuClassName,'hc-popover');
        assert.equal(typeof chip.data.menuContent,'function');
        assert.deepEqual(c.contributions.filter(item => item.area === 'routes').map(item => item.data.path),
            ['/hermes-control/engines','/hermes-control/runs']);
        assert.deepEqual(c.contributions.filter(item => item.area === 'sidebar.nav').map(item => item.data), [
            {path:'/hermes-control/engines',label:'Engines',codicon:'dashboard'},
            {path:'/hermes-control/runs',label:'Runs',codicon:'list-unordered'}]);
        for (const command of c.contributions.filter(item => item.area === 'palette')) {
            assert(command.data.keywords.length); command.data.run();
        }
        assert.deepEqual(navigation,['/hermes-control/engines','/hermes-control/runs']);
        assert.equal(contribution(c,'open-engines').data.id,'hermes-control.open-engines');
        assert.equal(contribution(c,'open-runs').data.label,'Hermes Control: Open Runs');
        assert.deepEqual(c.calls,['/status']);
        assert(c.intervals.some(item => item.ms === 15000));
        assert(c.intervals.some(item => item.ms === 1000));
        assert.equal(styles.length,1); assert(styles[0].textContent.includes('.hc-page'));
        const runs = mount(contribution(c,'runs-page').render());
        assert.equal(text(runs.render()),'Runs Refresh Open in dashboard  Loading runs… ');
        assert.deepEqual(c.calls,['/status','/runs?limit=30']); runs.unmount();
        c.disposals.forEach(fn => fn()); assert.equal(styles[0].removed,true);
    ''')


def test_status_polling_notifications_offline_and_reload(source):
    _harness(source, r'''
        const proof = {run_id:'synthetic-run',written:7,proven:5,red_green:3,not_proven:['synthetic-a','synthetic-b']};
        const running = {...baseStatus,chip:{kind:'running',engine:'sample-engine',started_at:Date.now()/1000-97},
            last_run:{run_id:'synthetic-run',state:'running',task_ids:['synthetic-a','synthetic-b']}};
        const finished = {...running,chip:{kind:'unproven',task_id:'synthetic-a'},last_run:{...running.last_run,state:'finished'},proof};
        const messages = [], replies = [running,finished,finished,new Error('offline'),finished];
        const c = context(() => {
            const answer = replies.shift(); return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
        }, {notify:input => messages.push(input)});
        plugin.register(c); await settle();
        const chip = mount(contribution(c,'chip').data.label);
        assert(text(chip.render()).includes('sample-engine running'));
        const tick = c.intervals.find(item => item.ms === 1000).fn;
        const poll = c.intervals.find(item => item.ms === 15000).fn;
        tick(); assert.equal(c.calls.length,1);
        assert.equal(messages.length,0);
        await poll(); await settle();
        assert.deepEqual(messages,[{title:'Hermes Control',body:'Run finished · 5/7 proven · 3 red-green · not proven: synthetic-a, synthetic-b',activate:'/hermes-control/runs'}]);
        assert(text(chip.render()).includes('2 not proven · synthetic-a'));
        await poll(); assert.equal(messages.length,1);
        await poll(); assert(text(chip.render()).includes('Hermes Control · offline'));
        assert(!nodes(chip.render()).some(n => n.props.className?.includes('hc-dot')));
        await poll(); assert.equal(messages.length,1);
        chip.unmount(); c.disposals.forEach(fn => fn());
        const before = c.calls.length; await poll(); assert.equal(c.calls.length,before);
        for (const os of [undefined,{}, {notify:() => {throw new Error('OS failed');}}]) {
            const sequence = [running,finished]; const next = context(() => Promise.resolve(sequence.shift()),os);
            if (os === undefined) delete next.os;
            plugin.register(next); await settle();
            await next.intervals.find(item => item.ms === 15000).fn();
            const view = mount(contribution(next,'chip').data.label);
            assert(!text(view.render()).includes('offline')); view.unmount();
            next.disposals.forEach(fn => fn());
        }
        const firstEnded = [], reopened = context(() => Promise.resolve(finished),{notify:n => firstEnded.push(n)});
        plugin.register(reopened); await settle();
        await reopened.intervals.find(item => item.ms === 15000).fn(); assert.deepEqual(firstEnded,[]);
        reopened.disposals.forEach(fn => fn());
        const waiting = deferred(), late = context(() => waiting.promise,{notify:n => firstEnded.push(n)});
        plugin.register(late); late.disposals.forEach(fn => fn()); waiting.resolve(finished); await settle();
        assert.deepEqual(firstEnded,[]);
    ''')


def test_chip_subscription_ticks_and_request_options(source):
    _harness(source, r'''
        const running = {...baseStatus,chip:{kind:'running',engine:'sample-engine',started_at:1}};
        const replies = [baseStatus,running,new Error('timeout'),baseStatus,running,baseStatus];
        const c = context(() => {
            const reply = replies.shift();
            return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply);
        });
        plugin.register(c); await settle();
        const chip = mount(contribution(c,'chip').data.label);
        chip.render();
        const tick = c.intervals.find(item => item.ms === 1000).fn;
        const poll = c.intervals.find(item => item.ms === 15000).fn;
        const checkTick = expected => {
            const setters = chip.setterCalls, requests = c.calls.length;
            tick();
            assert.equal(chip.setterCalls, setters + expected);
            assert.equal(c.calls.length, requests);
        };
        checkTick(0); // Idle.
        const beforePoll = chip.setterCalls;
        await poll();
        assert(chip.setterCalls > beforePoll, 'Changed status must update the mounted chip');
        assert(text(chip.render()).includes('sample-engine running'));
        checkTick(1);
        await poll(); // Failed request retains the running snapshot but renders offline.
        assert(text(chip.render()).includes('Hermes Control · offline'));
        checkTick(0);
        await poll();
        assert(text(chip.render()).includes('Idle'));
        checkTick(0);
        chip.unmount();
        const afterUnmount = chip.setterCalls;
        await poll(); tick(); await poll();
        assert.equal(chip.setterCalls, afterUnmount, 'Unmount must remove the subscription');
        assert.equal(c.calls.length, 6);
        assert(c.calls.every(path => path === '/status'));
        assert.deepEqual(c.requests, c.calls.map(path => ({path,options:{timeoutMs:10000}})));
        c.disposals.forEach(fn => fn());
    ''')


def test_notification_run_identity_and_failed_poll(source):
    _harness(source, r'''
        const running = {...baseStatus,last_run:{run_id:'synthetic-run',state:'running',task_ids:[]}};
        const finished = {...running,last_run:{...running.last_run,state:'finished'}};
        for (const interrupted of [false,true]) {
            const messages = [];
            const replies = interrupted
                ? [running,new Error('offline'),finished,finished]
                : [running,{...finished,last_run:{...finished.last_run,run_id:'synthetic-other'}},finished];
            const c = context(() => {
                const reply = replies.shift();
                return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply);
            }, {notify:message => messages.push(message)});
            plugin.register(c); await settle();
            const poll = c.intervals.find(item => item.ms === 15000).fn;
            await poll(); assert.equal(messages.length,0);
            await poll(); assert.equal(messages.length,interrupted ? 1 : 0);
            if (interrupted) {
                await poll();
                assert.equal(messages.length,1);
                assert.equal(messages[0].body,'Run finished · 0 tasks');
            }
            c.disposals.forEach(fn => fn());
        }
    ''')


def test_popover_fetch_fallback_windows_proof_and_navigation(source):
    _harness(source, r'''
        const pending = deferred();
        const status = {...baseStatus,engines:[{name:'sample-engine',percent:37,window:'short',cap:67,report_only:false}],
            last_run:{run_id:'synthetic-run',state:'paused',task_ids:['synthetic-task']},
            proof:{written:7,proven:6,red_green:0,not_proven:['synthetic-task']}};
        const c = context(path => path === '/status' ? Promise.resolve(status) : pending.promise);
        plugin.register(c); await settle();
        let closed = 0;
        const view = mount(contribution(c,'chip').data.menuContent(() => {closed++;}));
        let tree = view.render(); assert(text(tree).includes('37%'));
        assert(text(tree).includes('6/7 proven')); assert(text(tree).includes('Not proven: synthetic-task'));
        assert.equal(c.calls.filter(p => p === '/limits').length,1);
        assert.deepEqual(c.requests.find(request => request.path === '/limits').options,{timeoutMs:15000});
        pending.resolve({engines:{'sample-engine':{windows:[{label:'small',used_percent:17},
            {label:'largest',used_percent:97},{label:'middle',used_percent:53}],cap:67,report_only:true}},problem:null});
        await settle(); tree = view.render();
        assert(text(tree).includes('largest')); assert(text(tree).includes('middle')); assert(!text(tree).includes('small'));
        assert(text(tree).includes('report only')); assert(!text(tree).includes('cap'));
        assert(!nodes(tree).some(n => n.props.className === 'hc-cap'));
        assert(nodes(tree).some(n => n.props.className === 'hc-meter hc-report'));
        button(tree,'Open Engines').props.onClick(); button(tree,'Open Runs').props.onClick();
        assert.equal(closed,2); assert.deepEqual(navigation,['/hermes-control/engines','/hermes-control/runs']);
        view.render(); assert.equal(c.calls.filter(p => p === '/limits').length,1);
        view.unmount(); c.disposals.forEach(fn => fn());
        const broken = context(path => path === '/status' ? Promise.resolve(status) : Promise.reject(new Error('cached usage unavailable')));
        plugin.register(broken); await settle();
        const failed = mount(contribution(broken,'chip').data.menuContent(() => {})); failed.render(); await settle();
        assert(text(failed.render()).includes('cached usage unavailable')); failed.unmount();
        broken.disposals.forEach(fn => fn());
    ''')


def test_engines_load_independently_refresh_and_errors(source):
    _harness(source, r'''
        const install = deferred(), planner = deferred(), limits = deferred(), opened = [];
        const c = context(path => {
            if (path === '/status') return Promise.resolve({...baseStatus,engines:[{name:'sample-engine'}]});
            if (path === '/engines') return install.promise;
            if (path === '/planner') return planner.promise;
            return limits.promise;
        },{openExternal:url => {opened.push(url); return Promise.resolve(true);}});
        plugin.register(c); await settle();
        const page = mount(contribution(c,'engines-page').render());
        let tree = page.render();
        assert(text(tree).includes('Checking installation and sign-in'));
        assert.deepEqual(c.calls,['/status','/engines','/planner','/limits']);
        assert(c.requests.slice(1).every(request => request.options?.timeoutMs === 30000));
        limits.resolve({problem:'Synthetic caps note',engines:{'sample-engine':{plan:'sample-plan',cap:67,source:'synthetic cache',
            paused:false,blocked:false,held_back:null,windows:[{label:'short',used_percent:53,resets_at:Date.now()/1000+137},
                {label:'long',used_percent:null,resets_at:null}],error:'A synthetic reading error'}}});
        await settle(); tree = page.render();
        for (const expected of ['Synthetic caps note','Plan: sample-plan','cap 67%','Source: synthetic cache',
            'Taking work','near cap','resets in','no reading','A synthetic reading error','Checking installation']) assert(text(tree).includes(expected),expected);
        assert(nodes(tree).some(n => n.props.className === 'hc-cap' && n.props.style.left === '67%'));
        planner.resolve({provider:'sample-provider',model:'sample-model'});
        install.resolve({engines:[{name:'sample-engine',installed:true,version:'synthetic-version',signed_in:true,
            reasoning_summary:'synthetic-summary',path:'/private/hidden-executable',sign_in_command:'hidden-command'}],sandbox:{available:false}});
        await settle(); tree = page.render();
        for (const expected of ['Planner: sample-provider · sample-model','Verify sandbox: unavailable (needs the codex CLI installed)',
            'synthetic-version','Signed in','Reasoning summary: synthetic-summary']) assert(text(tree).includes(expected),expected);
        assert(!text(tree).includes('hidden-executable')); assert(!text(tree).includes('hidden-command'));
        const count = c.calls.length; page.render(); assert.equal(c.calls.length,count);
        assert.equal(c.intervals.length,2);
        await button(tree,'Edit in dashboard').props.onClick(); assert.deepEqual(opened,['http://127.0.0.1:9119/engines']);
        button(tree,'Refresh').props.onClick(); await settle();
        assert.deepEqual(c.calls.slice(-3),['/engines','/planner','/limits?refresh=true']);
        assert(c.requests.slice(-3).every(request => request.options?.timeoutMs === 30000));
        page.unmount(); c.disposals.forEach(fn => fn());

        const broken = context(path => path === '/status' ? Promise.resolve(baseStatus) : Promise.reject(new Error('Unavailable '+path)));
        plugin.register(broken); await settle();
        const failed = mount(contribution(broken,'engines-page').render()); failed.render(); await settle();
        const errorTree = failed.render();
        for (const endpoint of ['/engines','/planner','/limits']) assert(text(errorTree).includes('Unavailable '+endpoint));
        failed.unmount(); broken.disposals.forEach(fn => fn());
    ''')


@pytest.mark.parametrize("cap", [None, 67])
def test_engines_report_only_row_omits_cap(source, cap):
    _harness(source, "const reportCap = " + json.dumps(cap) + r''';
        const c = context(path => Promise.resolve(path === '/status' ? baseStatus :
            path === '/limits' ? {engines:{
                'synthetic-report':{report_only:true,cap:reportCap,windows:[{label:'short',used_percent:17}]},
                'synthetic-normal':{report_only:false,cap:67,windows:[{label:'short',used_percent:17}]},
            }} : {}));
        plugin.register(c); await settle();
        const page = mount(contribution(c,'engines-page').render());
        page.render(); await settle();
        const tree = page.render();
        const row = name => nodes(tree).find(node => node.type === 'section' && node.props['aria-label'] === name);
        const report = row('synthetic-report'), normal = row('synthetic-normal');
        assert(report); assert(normal);
        assert(text(report).includes('Report only: this reading never holds work back.'));
        assert(!/cap/i.test(text(report)), text(report));
        assert(text(normal).includes('cap 67%'));
        page.unmount(); c.disposals.forEach(fn => fn());
    ''')
