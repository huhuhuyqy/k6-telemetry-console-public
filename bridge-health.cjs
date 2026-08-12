'use strict';

function matchesBridgeHealth(health, expected) {
  return Boolean(
    health
    && health.service === 'k6-telemetry-bridge'
    && health.version === expected.version
    && health.token === expected.token
    && Number(health.pid) === expected.childPid
    && Number(health.parentPid) === expected.parentPid
  );
}

module.exports = { matchesBridgeHealth };
