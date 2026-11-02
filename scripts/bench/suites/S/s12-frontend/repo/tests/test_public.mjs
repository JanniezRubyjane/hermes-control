import assert from 'node:assert/strict';
import {decodeHash} from '../web/state.mjs';
export function test_existing() {
  assert.equal(decodeHash('').page,1);
}
