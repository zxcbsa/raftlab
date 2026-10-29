#!/usr/bin/env bash
# T1~T4 acceptance: contracts + unit tests (no cluster required).
set -u
cd "$(dirname "$0")/.." || exit 2

pass=0
fail=0

check() {
  local desc=$1 want=$2
  shift 2
  "$@" >/tmp/raftlab-selftest.out 2>&1
  local got=$?
  if [ "$got" -eq "$want" ]; then
    echo "PASS  $desc"
    pass=$((pass + 1))
  else
    echo "FAIL  $desc (expected exit $want, got $got)"
    sed 's/^/      /' /tmp/raftlab-selftest.out
    fail=$((fail + 1))
  fi
}

check "valid run directory"    0 python3 -m raftlab schema --check tests/fixtures/valid-run
check "valid scenario"         0 python3 -m raftlab schema --check scenarios/baseline-kill-leader.yaml
check "invalid run (counts)"   1 python3 -m raftlab schema --check tests/fixtures/invalid-run --allow-missing
check "invalid scenario (mix)" 1 python3 -m raftlab schema --check tests/fixtures/bad-scenario.yaml
check "unit tests"             0 python3 -m unittest discover -s tests -p "test_*.py"

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ]
