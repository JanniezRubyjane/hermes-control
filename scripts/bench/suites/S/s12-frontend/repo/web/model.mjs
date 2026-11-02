export function selectRows(rows,state,pageSize=3) {
  return {rows:rows.slice(0,pageSize),total:rows.length,pages:1,page:1};
}
