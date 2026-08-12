'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { matchesBridgeHealth } = require('../bridge-health.cjs');

const expected = {
  version: '0.2.5',
  token: 'new-session-token',
  childPid: 200,
  parentPid: 100,
};

test('bridge identity accepts only the child created by this Electron session', () => {
  const health = {
    service: 'k6-telemetry-bridge',
    version: '0.2.5',
    token: 'new-session-token',
    pid: 200,
    parentPid: 100,
  };
  assert.equal(matchesBridgeHealth(health, expected), true);
  assert.equal(matchesBridgeHealth({ ...health, token: 'old-session-token' }, expected), false);
  assert.equal(matchesBridgeHealth({ ...health, pid: 201 }, expected), false);
  assert.equal(matchesBridgeHealth({ ...health, version: '0.2.4' }, expected), false);
  assert.equal(matchesBridgeHealth({ ...health, service: 'http-server' }, expected), false);
});
