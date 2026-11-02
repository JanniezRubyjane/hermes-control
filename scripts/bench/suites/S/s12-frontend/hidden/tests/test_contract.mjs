// # hc-bench-canary: 7eed289b39f33880
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {decodeHash,encodeHash} from '../web/state.mjs';
import {selectRows} from '../web/model.mjs';
import {mount} from '../web/app.mjs';
const base={q:'',sort:'name',dir:'asc',page:1};
export function test_hash_roundtrip() {
  const state={q:'a &/é',sort:'price',dir:'desc',page:2};
  assert.equal(encodeHash(state),'#q=a%20%26%2F%C3%A9&sort=price&dir=desc&page=2');
  assert.deepEqual(decodeHash(encodeHash(state)),state);
  assert.deepEqual(decodeHash('#q=first&q=+last+&sort=price&dir=no&page=0002&other=1'),{...base,q:'last',sort:'price',page:2});
}
export function test_hash_invalid_fields() {
  for (const page of ['0','-1','1.5','1e2','1000001','١','']) {
    assert.deepEqual(decodeHash('q=ok&page='+encodeURIComponent(page)),{...base,q:'ok'});
  }
  for (const suffix of ['%','%FF','%Q0']) assert.deepEqual(decodeHash('q=ok&x='+suffix),base);
  assert.equal(encodeHash({q:' hi ',sort:'bad',dir:'bad',page:0}),'#q=hi&sort=name&dir=asc&page=1');
}
export function test_sort_filter_and_nulls() {
  const rows=[{name:'beta',price:null},{name:'Alpha',price:2},{name:'alpha',price:2},{name:'Zoo',price:1}];
  const before=JSON.stringify(rows);
  assert.deepEqual(selectRows(rows,base,10).rows,[rows[1],rows[2],rows[0],rows[3]]);
  assert.deepEqual(selectRows(rows,{...base,sort:'price',dir:'desc'},10).rows,[rows[1],rows[2],rows[3],rows[0]]);
  assert.deepEqual(selectRows(rows,{...base,q:' ALP '},10).rows,[rows[1],rows[2]]);
  assert.deepEqual(selectRows(rows,{...base,dir:'desc'},10).rows,[rows[3],rows[0],rows[1],rows[2]]);
  assert.equal(JSON.stringify(rows),before);
}
export function test_pagination() {
  const rows=['a','b','c','d'].map(name=>({name,price:0}));
  const state={...base,page:99};
  assert.deepEqual(selectRows(rows,state),{rows:[rows[3]],total:4,pages:2,page:2});
  assert.equal(state.page,99);
  assert.deepEqual(selectRows(rows,{...base,q:'absent'}),{rows:[],total:0,pages:1,page:1});
  for (const n of [0,-1,1.5,true]) assert.throws(()=>selectRows(rows,base,n),{name:'RangeError',message:'pageSize must be a positive integer'});
}
export function fixture(hash='') {
  class Element {
    constructor(tag='div') {
      this.tagName=tag.toUpperCase(); this.localName=tag.toLowerCase();
      this.children=[]; this.listeners={}; this.value=''; this._text='';
      this.parentNode=null; this.attributes=new Map();
    }
    get disabled() { return this.attributes.has('disabled'); }
    set disabled(value) { this.toggleAttribute('disabled',Boolean(value)); }
    setAttribute(name,value) { this.attributes.set(String(name).toLowerCase(),String(value)); }
    getAttribute(name) { return this.attributes.get(String(name).toLowerCase()) ?? null; }
    removeAttribute(name) { this.attributes.delete(String(name).toLowerCase()); }
    toggleAttribute(name,force) {
      name=String(name).toLowerCase();
      const enabled=force === undefined ? !this.attributes.has(name) : Boolean(force);
      if (enabled) this.setAttribute(name,''); else this.removeAttribute(name);
      return enabled;
    }
    insertRow(index=-1) { return this.insertElement('tr',index); }
    insertCell(index=-1) { return this.insertElement('td',index); }
    insertElement(tag,index) {
      if (index < -1 || index > this.children.length) throw new RangeError('invalid index');
      const element=new Element(tag);
      this.appendChild(element);
      if (index !== -1) {
        this.children.pop(); this.children.splice(index,0,element);
      }
      return element;
    }
    get textContent() { return this._text+this.children.map(child=>child.textContent).join(''); }
    set textContent(value) { this.replaceChildren(); this._text=String(value ?? ''); }
    get innerHTML() { return this.textContent; }
    set innerHTML(value) {
      if (value !== '') throw new Error('render literal text with textContent');
      this.replaceChildren();
    }
    addEventListener(name,fn) { (this.listeners[name] ??= []).push(fn); }
    removeEventListener(name,fn) { this.listeners[name]=(this.listeners[name] ?? []).filter(f=>f!==fn); }
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      this.children.push(child); child.parentNode=this; return child;
    }
    append(...children) {
      for (let child of children) {
        if (typeof child !== 'object') { const text=child; child=new Element('#text'); child.textContent=text; }
        this.appendChild(child);
      }
    }
    removeChild(child) {
      this.children.splice(this.children.indexOf(child),1); child.parentNode=null; return child;
    }
    replaceChildren(...children) {
      for (const child of this.children) child.parentNode=null;
      this.children=[]; this._text=''; this.append(...children);
    }
    fire(type) {
      const event={type,target:this,currentTarget:this,preventDefault(){}};
      for (const fn of this.listeners[type] ?? []) fn.call(this,event);
      this['on'+type]?.call(this,event);
    }
  }
  const tags={q:'input',sort:'select',dir:'select',rows:'tbody',prev:'button',next:'button',status:'output'};
  const controls=Object.fromEntries(Object.entries(tags).map(([id,tag])=>[id,new Element(tag)]));
  const document={
    getElementById:id=>controls[id] ?? null,
    querySelector(selector) {
      const match=/^(?:([a-z]+))?#([a-z]+)$/i.exec(selector);
      if (!match) return null;
      const element=controls[match[2]];
      return element && (!match[1] || element.localName===match[1].toLowerCase()) ? element : null;
    },
    createElement:tag=>new Element(tag),
    createTextNode(text) { const node=new Element('#text'); node.textContent=text; return node; },
  };
  const browser={
    location:{hash},listeners:{},
    addEventListener(name,fn) {
      const previous=this.listeners[name];
      this.listeners[name]=event=>{ previous?.(event); fn.call(this,event ?? {type:name,target:this}); };
    },
    fire(name) { this.listeners[name]?.({type:name,target:this}); this['on'+name]?.({type:name,target:this}); },
  };
  browser.history={replaceState(state,title,url) {
    if (url != null) browser.location.hash=new URL(String(url),'https://fixture.invalid/').hash;
    this.state=state;
  }};
  return {controls,document,browser};
}
export function test_dom_events_and_escaping() {
  const {controls:c,document,browser}=fixture('#sort=price&dir=desc&page=9');
  const rows=[{name:'<img>',price:null},...['a','b','c','d'].map((name,i)=>({name,price:i}))];
  mount(document,rows,browser);
  assert.equal(c.status.textContent,'5 results; page 2/2');
  assert.equal(c.rows.children[1].children[0].textContent,'<img>');
  assert.equal(c.rows.children[1].children[1].textContent,'—');
  assert.equal(c.rows.children[1].tagName.toLowerCase(),'tr');
  assert.equal(c.rows.children[1].children[0].tagName.toLowerCase(),'td');
  assert.equal(c.next.disabled,true); assert.equal(c.prev.disabled,false);
  assert.equal(browser.location.hash,'#q=&sort=price&dir=desc&page=2');
  c.prev.fire('click'); assert.equal(c.status.textContent,'5 results; page 1/2');
  c.next.fire('click'); assert.equal(c.status.textContent,'5 results; page 2/2');
  c.q.value=' a '; c.q.fire('input');
  assert.equal(c.status.textContent,'1 results; page 1/1');
  assert.equal(c.rows.children.length,1); assert.equal(c.prev.disabled,true);
  c.q.value='absent'; c.q.fire('input'); assert.equal(c.rows.children.length,0);
  browser.location.hash='#q=&sort=name&dir=asc&page=1'; browser.fire('hashchange');
  assert.equal(c.q.value,''); assert.equal(c.sort.value,'name'); assert.equal(c.dir.value,'asc');
  assert.equal(c.rows.children[0].children[0].textContent,'<img>');
  c.sort.value='price'; c.sort.fire('change');
  c.dir.value='desc'; c.dir.fire('change');
  assert.equal(c.rows.children[0].children[0].textContent,'d');
}
export async function test_html_entrypoint() {
  const html=await readFile(new URL('../index.html',import.meta.url),'utf8');
  for (const id of ['q','sort','dir','rows','prev','next','status']) assert.match(html,new RegExp(`id=["']${id}["']`));
  assert.match(html,/<script\b(?=[^>]*type=["']module["'])(?=[^>]*src=["'](?:\.\/)?web\/app\.mjs["'])[^>]*>/);
}
