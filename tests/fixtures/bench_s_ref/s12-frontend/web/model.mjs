import {decodeHash,encodeHash} from './state.mjs';

export function selectRows(rows, input, pageSize=3) {
  if (!Number.isInteger(pageSize) || pageSize <= 0) {
    throw new RangeError('pageSize must be a positive integer');
  }
  const state = decodeHash(encodeHash(input));
  const query = state.q.toLowerCase();
  const filtered = rows.map((row,index) => ({row,index}))
    .filter(({row}) => row.name.toLowerCase().includes(query));
  filtered.sort((a,b) => {
    let order;
    if (state.sort === 'price') {
      if (a.row.price === null && b.row.price !== null) return 1;
      if (b.row.price === null && a.row.price !== null) return -1;
      order = (a.row.price ?? 0) - (b.row.price ?? 0);
    } else {
      const left = a.row.name.toLowerCase();
      const right = b.row.name.toLowerCase();
      order = left < right ? -1 : left > right ? 1 : 0;
    }
    if (state.dir === 'desc') order = -order;
    return order || a.index-b.index;
  });
  const total = filtered.length;
  const pages = Math.max(1,Math.ceil(total/pageSize));
  const page = Math.min(state.page,pages);
  const start = (page-1)*pageSize;
  return {rows:filtered.slice(start,start+pageSize).map(x=>x.row),total,pages,page};
}
