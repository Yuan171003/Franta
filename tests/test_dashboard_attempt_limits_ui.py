from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard behavior checks")
class DashboardAttemptLimitUiTests(unittest.TestCase):
    def test_refresh_preserves_unsaved_input_and_displays_server_grace_period(self) -> None:
        app = (Path(__file__).resolve().parents[1] / "src/dashboard_system/static/app.js").read_text()
        functions = app[app.index("function updateAttemptLimitCountdown()"):app.index("function renderOverview(data)")]
        script = """
const assert = require('node:assert/strict');
const elements = new Map();
const $ = (id) => { if (!elements.has(id)) elements.set(id, {value: '', textContent: '', hidden: false}); return elements.get(id); };
const number = (value) => String(value);
let clock = Date.parse('2026-09-22T12:00:00Z');
Date.now = () => clock;
const state = {overview: {as_of: '2026-09-22T12:00:00Z'}, overviewAt: clock, attemptLimitsDirty: false, attemptLimitsSubmitting: false};
""" + functions + """
const budgets = {available:true,enabled:true,explorer: {limit:20,used:12,completed:8,running:4}, franta: {limit:30,used:0,completed:0,running:0}, pending: {command_id:'ATL-1', explorer_limit:25,franta_limit:35,submitted_at:'2026-09-22T12:00:00Z',effective_at:'2026-09-22T12:02:00Z'}};
renderAttemptBudgets(budgets);
assert.equal($('attempt-budget-section').hidden, false);
assert.equal($('explorer-attempt-limit').textContent, 'Active limit: 20');
assert.equal(String($('explorer-attempt-input').value), '25');
assert.equal($('explorer-attempt-used').textContent, '12');
assert.equal($('explorer-attempt-detail').textContent, '8 completed · 4 running');
assert.match($('attempt-limit-pending').textContent, /Takes effect in 2:00/);
state.attemptLimitsDirty = true;
$('explorer-attempt-input').value = '99';
renderAttemptBudgets({...budgets, pending:{...budgets.pending, explorer_limit:26}});
assert.equal($('explorer-attempt-input').value, '99');
clock += 119000;
updateAttemptLimitCountdown();
assert.match($('attempt-limit-pending').textContent, /Takes effect in 0:01/);
clock += 1000;
updateAttemptLimitCountdown();
assert.match($('attempt-limit-pending').textContent, /waiting for the runtime/);
assert.equal($('explorer-attempt-limit').textContent, 'Active limit: 20');
// A saved response remains visible when an older overview request completes.
state.attemptLimitsDirty = false;
state.attemptLimitsSaved = {command_id:'ATL-2',created_at:'2026-09-22T12:01:00Z',effective_at:'2026-09-22T12:03:00Z',explorer_limit:40,franta_limit:50};
renderAttemptBudgets(budgets);
assert.equal(String($('explorer-attempt-input').value), '40');
assert.equal(state.attemptLimitsPending.command_id, 'ATL-2');
renderAttemptBudgets({...budgets,pending:null,latest_command_id:'ATL-2',latest_submitted_at:'2026-09-22T12:01:00Z',explorer:{...budgets.explorer,limit:40}});
assert.equal(state.attemptLimitsSaved, null);
assert.equal($('attempt-limit-pending').hidden, true);
assert.equal($('explorer-attempt-limit').textContent, 'Active limit: 40');
renderAttemptBudgets({...budgets,available:false,enabled:false});
assert.equal($('attempt-budget-section').hidden, true);
assert.equal(state.attemptLimitsPending, null);
renderAttemptBudgets({...budgets,available:true,enabled:false});
assert.equal($('attempt-budget-section').hidden, false);
"""
        completed = subprocess.run([shutil.which("node"), "-"], input=script, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
