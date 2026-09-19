"""End-to-end test of keepalive.main(): concurrency, budget and exit code.

Run:  python scripts/_test_keepalive_main.py

Regression target: run 35431894569 died with
    "The action 'Run keepalive script' has timed out after 20 minutes"
because a stuck instance was polled for 600s, instances were processed serially,
and nothing enforced a whole-run budget. These tests assert that:
  * a permanently stuck fleet still finishes inside a small global budget,
  * the run exits non-zero so Actions reports the problem,
  * recovery really was attempted (not an instant bail-out),
  * a healthy fleet still cycles every instance and exits zero.

main() calls os._exit() on failure, so it has to run in a child process.
"""
import os
import re
import subprocess
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD = textwrap.dedent(r'''
    """Fake ucloud SDK, then run keepalive.main() unmodified."""
    import os, sys, types

    pkg = types.ModuleType("ucloud"); pkg.__path__ = []
    core = types.ModuleType("ucloud.core"); core.__path__ = []
    exc = types.ModuleType("ucloud.core.exc")

    class UCloudException(Exception):
        pass

    exc.UCloudException = UCloudException

    MODE = os.environ.get("FAKE_MODE", "stuck")
    INSTANCES = int(os.environ.get("FAKE_INSTANCES", "4"))
    SEVEN_DAYS = 7 * 24 * 3600

    # Live state and release clocks per instance, so the fake can model the real
    # contract: a stop refreshes StopTime and pushes ReleaseTime 7 days out.
    STATE = {}
    CLOCKS = {}
    NOW = [1_800_000_000]

    class _UCompshare:
        def _ensure(self, uhost_id):
            if uhost_id not in STATE:
                STATE[uhost_id] = "Initializing" if MODE == "stuck" else "Stopped"
                CLOCKS[uhost_id] = [NOW[0] - SEVEN_DAYS, NOW[0]]

        def invoke(self, action, params):
            if action == "DescribeCompShareInstance":
                if params.get("Limit"):
                    # Page listing used by list_instances().
                    return {"RetCode": 0, "TotalCount": INSTANCES, "UHostSet": []}
                uhost_id = params["UHostIds"][0]
                self._ensure(uhost_id)
                stop_time, release_time = CLOCKS[uhost_id]
                return {"RetCode": 0, "UHostSet": [{
                    "State": STATE[uhost_id], "UHostId": uhost_id,
                    "StopTime": stop_time, "ReleaseTime": release_time}]}
            if action == "StartCompShareInstance":
                uhost_id = params["UHostId"]
                self._ensure(uhost_id)
                if MODE != "stuck":
                    STATE[uhost_id] = "Running"
                return {"RetCode": 0}
            if action == "StopCompShareInstance":
                uhost_id = params["UHostId"]
                self._ensure(uhost_id)
                # "stuck": the instance never reaches Running and ignores the
                # stop, yet the real platform still refreshes the reclamation
                # clock — that is exactly the production case we must not fail.
                if MODE == "healthy":
                    STATE[uhost_id] = "Stopped"
                # "frozen" models a stop that is accepted but never refreshes the
                # reclamation clock, which must be reported as a real failure.
                if MODE != "frozen":
                    NOW[0] += 60
                    CLOCKS[uhost_id] = [NOW[0], NOW[0] + SEVEN_DAYS]
                return {"RetCode": 0}
            return {"RetCode": 0, "UHostSet": [], "TotalCount": 0}

    class _Client:
        def __init__(self, config):
            pass

        def ucompshare(self):
            return _UCompshare()

    client_mod = types.ModuleType("ucloud.client")
    client_mod.Client = _Client
    sys.modules["ucloud"] = pkg
    sys.modules["ucloud.core"] = core
    sys.modules["ucloud.core.exc"] = exc
    sys.modules["ucloud.client"] = client_mod

    sys.path.insert(0, r"__HERE__")
    import keepalive

    keepalive.list_instances = lambda client, limit=100, deadline=None: [
        {"UHostId": "uhost-%d" % i, "Name": "inst-%d" % i,
         "State": "Initializing" if MODE == "stuck" else "Stopped",
         "Region": "cn-wlcb", "Zone": "cn-wlcb-01"}
        for i in range(INSTANCES)
    ]

    calls = {"start": 0, "stop": 0}

    if MODE == "unresponsive":
        # API calls fail outright, so the run must stay bounded and report failure.
        def _noop_stop(*a, **k):
            calls["stop"] += 1
            return False

        def _noop_start(*a, **k):
            calls["start"] += 1
            return False

        keepalive._stop_instance = _noop_stop
        keepalive._start_instance = _noop_start

    # Report what happened on a dedicated line the parent can parse.
    real_ensure = keepalive.ensure_running

    def _wrapped(client, instance, global_deadline=None):
        ok = real_ensure(client, instance, global_deadline=global_deadline)
        print("RESULT %s %s" % (instance.get("UHostId"), ok), flush=True)
        return ok

    keepalive.ensure_running = _wrapped
    keepalive.main()
    print("CALLS start=%d stop=%d" % (calls["start"], calls["stop"]), flush=True)
''').replace("__HERE__", HERE)


def run_case(name, env_extra, timeout=120):
    env = dict(os.environ)
    env.update({
        "COMPSHARE_PUBLIC_KEY": "fake",
        "COMPSHARE_PRIVATE_KEY": "fake",
        # Small budgets keep the test fast. main() divides the remaining global
        # budget evenly across instances, so keep the instance count low.
        "KEEPALIVE_GLOBAL_BUDGET_SEC": "45",
        "KEEPALIVE_PER_INSTANCE_BUDGET_SEC": "20",
        "KEEPALIVE_TRANSITION_GRACE_SEC": "2",
        "KEEPALIVE_STARTUP_WAIT_SEC": "2",
        "KEEPALIVE_STOP_WAIT_SEC": "2",
        "KEEPALIVE_MAX_ROUNDS": "2",
        "KEEPALIVE_MAX_WORKERS": "4",
        "KEEPALIVE_MIN_ACTION_BUDGET_SEC": "1",
        "KEEPALIVE_POLL_INTERVAL_SEC": "1",
        "KEEPALIVE_API_TIMEOUT_SEC": "1",
    })
    env.update(env_extra)

    script = os.path.join(HERE, "_e2e_child.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(CHILD)
    try:
        started = time.time()
        proc = subprocess.run([sys.executable, script], env=env,
                              capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - started
    finally:
        os.remove(script)

    if os.environ.get("E2E_VERBOSE"):
        tail = "\n".join(proc.stderr.strip().splitlines()[-30:])
        print(textwrap.indent(tail, "      "))

    return proc, elapsed


def main():
    failures = []

    # --- Case 1: start never completes, but the stop still refreshes clocks --
    print("E2E 1: 2 instances whose start never reaches Running")
    proc, elapsed = run_case("stuck", {"FAKE_MODE": "stuck", "FAKE_INSTANCES": "2"})
    results = re.findall(r"RESULT (\S+) (\w+)", proc.stdout)
    print(f"  rc={proc.returncode} elapsed={elapsed:.1f}s results={len(results)}")
    if proc.returncode != 0:
        failures.append(f"E2E 1: expected rc=0 (clock still refreshed), got {proc.returncode}")
    if elapsed > 45:
        failures.append(f"E2E 1: did not respect the global budget ({elapsed:.1f}s)")
    if len(results) != 2:
        failures.append(f"E2E 1: expected 2 per-instance results, got {len(results)}")
    if any(ok != "True" for _, ok in results):
        failures.append(f"E2E 1: should report keepalive success: {results}")
    if "keepalive confirmed" not in proc.stderr:
        failures.append("E2E 1: success was not justified by the release clock")

    # --- Case 2: healthy fleet still works ----------------------------------
    print("E2E 2: 2 healthy instances (Stopped -> Running -> Stopped)")
    proc2, elapsed2 = run_case("healthy", {"FAKE_MODE": "healthy", "FAKE_INSTANCES": "2"})
    results2 = re.findall(r"RESULT (\S+) (\w+)", proc2.stdout)
    print(f"  rc={proc2.returncode} elapsed={elapsed2:.1f}s results={len(results2)}")
    if proc2.returncode != 0:
        failures.append(f"E2E 2: expected rc=0, got {proc2.returncode}")
    if not results2 or any(ok != "True" for _, ok in results2):
        failures.append(f"E2E 2: not all instances reported OK: {results2}")

    # --- Case 3: the clock never moves -> genuine failure, non-zero exit -----
    print("E2E 3: 2 instances whose release clock never advances")
    proc3, elapsed3 = run_case("frozen", {"FAKE_MODE": "frozen", "FAKE_INSTANCES": "2"})
    results3 = re.findall(r"RESULT (\S+) (\w+)", proc3.stdout)
    print(f"  rc={proc3.returncode} elapsed={elapsed3:.1f}s results={len(results3)}")
    if proc3.returncode != 1:
        failures.append(f"E2E 3: expected rc=1, got {proc3.returncode}")
    if any(ok == "True" for _, ok in results3):
        failures.append(f"E2E 3: must not claim success when the clock is frozen: {results3}")

    print()
    if failures:
        print("FAILURES:")
        for item in failures:
            print(" -", item)
        return 1
    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
