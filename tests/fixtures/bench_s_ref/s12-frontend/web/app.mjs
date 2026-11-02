import {decodeHash,encodeHash} from './state.mjs';
import {selectRows} from './model.mjs';

export function mount(document,rows,browser) {
  const controls = Object.fromEntries(['q','sort','dir','rows','prev','next','status']
    .map(id => [id,document.querySelector('#'+id)]));
  let state = decodeHash(browser.location.hash);
  function render() {
    const result = selectRows(rows,state);
    state = {...state,page:result.page};
    for (const key of ['q','sort','dir']) controls[key].value = state[key];
    const children = result.rows.map(row => {
      const tr = document.createElement('tr');
      for (const value of [row.name,row.price === null ? '—' : String(row.price)]) {
        const td = document.createElement('td');
        td.textContent = value;
        tr.appendChild(td);
      }
      return tr;
    });
    controls.rows.replaceChildren(...children);
    controls.status.textContent = `${result.total} results; page ${result.page}/${result.pages}`;
    controls.prev.disabled = result.page === 1;
    controls.next.disabled = result.page === result.pages;
    const hash = encodeHash(state);
    if (browser.location.hash !== hash) browser.location.hash = hash;
  }
  for (const key of ['q','sort','dir']) {
    controls[key].addEventListener(key === 'q' ? 'input' : 'change', () => {
      state = decodeHash(encodeHash({...state,[key]:controls[key].value,page:1}));
      render();
    });
  }
  for (const [key,step] of [['prev',-1],['next',1]]) {
    controls[key].addEventListener('click', () => {
      state.page = Math.max(1,state.page+step);
      render();
    });
  }
  browser.addEventListener('hashchange', () => {
    state = decodeHash(browser.location.hash);
    render();
  });
  render();
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  mount(document,[{name:'Basic',price:10},{name:'Plus',price:null}],window);
}
