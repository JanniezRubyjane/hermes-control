"""Wordmark DOM lifecycle and usage-column regression checks."""

import re

from test_desktop_plugin import PLUGIN, _harness


DOM = r'''
const records = [], structuralCalls = [];
let bodyQueries = 0;
class FakeNode {
    constructor(type, value = null, attrs = {}, children = []) {
        this.nodeType = type;
        this.isConnected = true;
        this._value = value;
        this.attrs = {...attrs};
        this.childNodes = children;
        for (const child of children) child.parentElement = this;
    }
    get nodeValue() { return this._value; }
    set nodeValue(value) {
        this._value = value;
        records.push({type:'characterData',target:this});
    }
    getAttribute(name) { return this.attrs[name] ?? null; }
    setAttribute(name, value) {
        this.attrs[name] = value;
        records.push({type:'attributes',attributeName:name,target:this});
    }
    matches(selector) {
        assert(['[aria-label="HERMES AGENT"]',
            '[aria-label="HERMES AGENT"],[aria-label="HERMES CONTROL"]'].includes(selector));
        return this.nodeType === 1 && (this.attrs['aria-label'] === 'HERMES AGENT' ||
            (selector.includes('HERMES CONTROL') && this.attrs['aria-label'] === 'HERMES CONTROL'));
    }
    closest(selector) {
        return this.matches(selector) ? this : this.parentElement?.closest(selector) ?? null;
    }
    querySelectorAll(selector) {
        if (this === document.body) bodyQueries++;
        return this.childNodes.flatMap(child => [
            ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector),
        ]);
    }
    appendChild() { structuralCalls.push('appendChild'); }
    removeChild() { structuralCalls.push('removeChild'); }
    replaceChild() { structuralCalls.push('replaceChild'); }
}
const element = (attrs = {}, children = []) => new FakeNode(1, null, attrs, children);
const textNode = value => new FakeNode(3, value);
function wordmark() {
    const texts = [textNode(' HERMES AGENT '), textNode('HERMES AGENT')];
    const other = textNode('HERMES AGENT elsewhere');
    const root = element({'aria-label':'HERMES AGENT'}, [
        element({}, [element({}, [texts[0]])]),
        element({'aria-hidden':'true'}, [texts[1]]), other,
    ]);
    return {root, texts, other};
}
const first = wordmark(), chat = element({}, [textNode('HERMES AGENT')]);
document.body = element({}, [first.root, chat]);
const observers = [];
globalThis.MutationObserver = class {
    constructor(callback) { this.callback = callback; this.connected = false; observers.push(this); }
    observe(target, options) { this.target = target; this.options = options; this.connected = true; }
    disconnect() { this.connected = false; }
    deliver(batch) { if (this.connected) this.callback(batch, this); }
};
function assertWordmark(mark, label) {
    assert.equal(mark.root.getAttribute('aria-label'), label);
    assert.deepEqual(mark.texts.map(node => node.nodeValue), [label, label]);
    assert.equal(mark.other.nodeValue, 'HERMES AGENT elsewhere');
}
'''


def run_dom(program):
    _harness(PLUGIN.read_text(encoding="utf-8"), DOM + program)


def test_existing_wordmark_and_chat_without_node_mutation():
    run_dom(r'''
        const c = context(); plugin.register(c);
        assertWordmark(first, 'HERMES CONTROL');
        assert.equal(chat.childNodes[0].nodeValue, 'HERMES AGENT');
        assert.deepEqual(structuralCalls, []);
        assert.equal(observers.length, 1);
        assert.equal(observers[0].target, document.body);
        assert.deepEqual(observers[0].options, {
            childList:true, subtree:true, characterData:true,
            attributes:true, attributeFilter:['aria-label'],
        });
    ''')


def test_added_wordmarks_and_react_updates_stay_local_and_do_not_loop():
    run_dom(r'''
        const c = context(); plugin.register(c);
        assert.equal(observers.length, 1);
        const observer = observers[0], initialQueries = bodyQueries;
        const later = wordmark(), direct = wordmark();
        const wrapper = element({}, [later.root]);
        observer.deliver([{type:'childList',target:document.body,addedNodes:[wrapper, direct.root]}]);
        assertWordmark(later, 'HERMES CONTROL');
        assertWordmark(direct, 'HERMES CONTROL');
        // React can rewrite the existing nodes and their accessible label.
        first.texts.forEach(node => { node.nodeValue = 'HERMES AGENT'; });
        first.root.setAttribute('aria-label', 'HERMES AGENT');
        observer.deliver(records.splice(0));
        assertWordmark(first, 'HERMES CONTROL');
        const ownWrites = records.splice(0);
        observer.deliver(ownWrites);
        assert.equal(records.length, 0);
        observer.deliver([{type:'characterData',target:chat.childNodes[0]}]);
        assert.equal(chat.childNodes[0].nodeValue, 'HERMES AGENT');
        assert.equal(bodyQueries, initialQueries, 'callbacks must not scan the body');
        assert.deepEqual(structuralCalls, []);
    ''')


def test_dispose_disconnects_and_restores_all_changed_elements():
    run_dom(r'''
        const c = context(); plugin.register(c);
        assert.equal(observers.length, 1);
        const later = wordmark();
        observers[0].deliver([{type:'childList',target:document.body,addedNodes:[later.root]}]);
        c.disposals.forEach(dispose => dispose());
        assert.equal(observers[0].connected, false);
        assertWordmark(first, 'HERMES AGENT');
        assertWordmark(later, 'HERMES AGENT');
        assert.deepEqual(structuralCalls, []);
    ''')


def test_character_data_alone_repairs_renamed_wordmark():
    run_dom(r'''
        const c = context(); plugin.register(c);
        records.length = 0;
        first.texts[0].nodeValue = 'HERMES AGENT';
        const batch = records.splice(0);
        assert.equal(batch.length, 1);
        assert.equal(batch[0].type, 'characterData');
        observers[0].deliver(batch);
        assertWordmark(first, 'HERMES CONTROL');
        assert.deepEqual(records.map(record => record.type), ['characterData']);
        observers[0].deliver(records.splice(0));
        assert.equal(records.length, 0);
    ''')


def test_added_chat_subtree_keeps_agent_text():
    run_dom(r'''
        const c = context(); plugin.register(c);
        records.length = 0;
        const message = textNode('HERMES AGENT');
        const wrapper = element({}, [element({}, [message])]);
        observers[0].deliver([{type:'childList',target:document.body,addedNodes:[wrapper]}]);
        assert.equal(message.nodeValue, 'HERMES AGENT');
        assert.equal(records.length, 0);
    ''')


def test_detached_wordmarks_are_not_restored_after_new_wordmark():
    run_dom(r'''
        const c = context(); plugin.register(c);
        first.root.isConnected = false;
        const later = wordmark();
        observers[0].deliver([{type:'childList',target:document.body,addedNodes:[later.root]}]);
        assertWordmark(later, 'HERMES CONTROL');
        records.length = 0;
        c.disposals.forEach(dispose => dispose());
        assertWordmark(first, 'HERMES CONTROL');
        assertWordmark(later, 'HERMES AGENT');
        assert(!records.some(record => record.target === first.root || first.texts.includes(record.target)));
    ''')


def test_detached_wordmark_is_not_restored_without_replacement():
    run_dom(r'''
        const c = context(); plugin.register(c);
        first.root.isConnected = false;
        records.length = 0;
        c.disposals.forEach(dispose => dispose());
        assertWordmark(first, 'HERMES CONTROL');
        assert.equal(records.length, 0);
    ''')


def test_initial_query_failure_does_not_prevent_registration():
    run_dom(r'''
        document.body.querySelectorAll = () => { throw new Error('DOM unavailable'); };
        const c = context();
        assert.doesNotThrow(() => plugin.register(c));
        assert.deepEqual(c.contributions.map(item => item.id), [
            'theme', 'chip', 'engines-page', 'runs-page',
            'engines-nav', 'runs-nav', 'open-engines', 'open-runs',
        ]);
        c.disposals.forEach(dispose => dispose());
    ''')


def test_observe_failure_does_not_prevent_registration():
    run_dom(r'''
        MutationObserver.prototype.observe = () => { throw new Error('observe failed'); };
        const c = context();
        assert.doesNotThrow(() => plugin.register(c));
        assert.equal(c.contributions.length, 8);
        c.disposals.forEach(dispose => dispose());
    ''')


def test_callback_failure_is_contained():
    run_dom(r'''
        const c = context(); plugin.register(c);
        const wrapper = element();
        wrapper.querySelectorAll = () => { throw new Error('query failed'); };
        assert.doesNotThrow(() => observers[0].deliver([
            {type:'childList',target:document.body,addedNodes:[wrapper]},
        ]));
        assert.equal(observers[0].connected, false);
        c.disposals.forEach(dispose => dispose());
    ''')


def test_added_text_nodes_are_not_queried_and_do_not_rewrite_label():
    run_dom(r'''
        const c = context(); plugin.register(c);
        const token = textNode('streamed token');
        token.parentElement = first.root;
        token.querySelectorAll = () => { throw new Error('text nodes cannot be queried'); };
        records.length = 0;
        assert.doesNotThrow(() => observers[0].deliver([
            {type:'childList',target:first.root,addedNodes:[token]},
        ]));
        assert.equal(records.length, 0);
        assert.equal(observers[0].connected, true);
        assertWordmark(first, 'HERMES CONTROL');
    ''')


def test_missing_mutation_observer_is_harmless():
    run_dom(r'''
        delete globalThis.MutationObserver;
        const c = context();
        assert.doesNotThrow(() => plugin.register(c));
        assert.equal(first.root.getAttribute('aria-label'), 'HERMES AGENT');
        assert.deepEqual(first.texts.map(node => node.nodeValue), [' HERMES AGENT ', 'HERMES AGENT']);
        c.disposals.forEach(dispose => dispose());
    ''')


def test_missing_body_is_harmless():
    run_dom(r'''
        delete document.body;
        const c = context();
        assert.doesNotThrow(() => plugin.register(c));
        assert.equal(observers.length, 0);
        c.disposals.forEach(dispose => dispose());
    ''')


def test_usage_columns_have_fixed_percent_and_reset_widths():
    source = PLUGIN.read_text(encoding="utf-8")
    css = source.split("const styles = `", 1)[1].split("`", 1)[0]
    rule = re.search(r"\.hc-window\s*\{([^}]+)\}", css)[1]
    columns = re.search(r"grid-template-columns:([^;]+)", rule)[1]
    assert "auto" not in columns
    assert re.search(r"\)\s+\d+px\s+\d+px$", columns)
    assert re.search(r"@media \(max-width:760px\).*\.hc-window \{ grid-template-columns:1fr 1fr;", css)
