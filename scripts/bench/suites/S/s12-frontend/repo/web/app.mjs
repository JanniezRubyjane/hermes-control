export function mount(document,rows,browser) {
  document.querySelector('#status').textContent = `${rows.length} results`;
}
