const defaults = () => ({q:'', sort:'name', dir:'asc', page:1});

function normalize(input) {
  const state = defaults();
  state.q = typeof input.q === 'string' ? input.q.trim() : '';
  if (['name','price'].includes(input.sort)) state.sort = input.sort;
  if (['asc','desc'].includes(input.dir)) state.dir = input.dir;
  const page = String(input.page ?? '');
  if (/^[0-9]+$/.test(page) && Number(page) >= 1 && Number(page) <= 1000000) {
    state.page = Number(page);
  }
  return state;
}

export function decodeHash(hash) {
  const text = hash.startsWith('#') ? hash.slice(1) : hash;
  const input = {};
  try {
    for (const pair of text.split('&')) {
      const index = pair.indexOf('=');
      const key = index < 0 ? pair : pair.slice(0,index);
      const value = index < 0 ? '' : pair.slice(index+1);
      const decode = x => decodeURIComponent(x.replace(/\+/g,' '));
      const decodedKey = decode(key);
      const decodedValue = decode(value);
      if (['q','sort','dir','page'].includes(decodedKey)) input[decodedKey] = decodedValue;
    }
  } catch {
    return defaults();
  }
  return normalize(input);
}

export function encodeHash(input) {
  const state = normalize(input);
  return '#' + ['q','sort','dir','page'].map(key =>
    `${key}=${encodeURIComponent(state[key])}`).join('&');
}
