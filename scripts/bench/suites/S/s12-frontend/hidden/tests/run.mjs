// # hc-bench-canary: 6f67013866cd8ed9
import {readdir} from 'node:fs/promises';
let count=0;
for (const file of (await readdir(new URL('.',import.meta.url))).filter(x=>/^test_.*\.mjs$/.test(x)).sort()) {
  const tests=await import(new URL(file,import.meta.url));
  for (const name of Object.keys(tests).sort()) {
    if (name.startsWith('test_')) { await tests[name](); count++; }
  }
}
console.log(`${count} assertions groups passed`);
