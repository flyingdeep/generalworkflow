"""Local simulation of keepalive state handling. Not part of the workflow.

Run:  python scripts/_test_keepalive_state.py

It stubs the UCloud SDK and drives the state machine against a fake CompShare
API that models the real reclamation contract:

    StopTime/ReleaseTime are refreshed by a *stop*, and ReleaseTime = StopTime + 7d

so keepalive success means "the release clock advanced", not "the instance
reached Running". The fake also models instances whose 无卡模式 start never
completes, which is what made the old Running-based success test report bogus
failures in production.
"""
import os
import sys
import time
import types

# --- Stub the UCloud SDK -----------------------------------------------------
_ucloud = types.ModuleType("ucloud")
_core = types.ModuleType("ucloud.core")
_exc = types.ModuleType("ucloud.core.exc")


class _UCloudException(Exception):
    pass


_exc.UCloudException = _UCloudException


class _Client:
    def __init__(self, config):
        self.config = config


_client_mod = types.ModuleType("ucloud.client")
_client_mod.Client = _Client
_ucloud.core = _core
_core.exc = _exc
sys.modules.setdefault("ucloud", _ucloud)
sys.modules.setdefault("ucloud.core", _core)
sys.modules.setdefault("ucloud.core.exc", _exc)
sys.modules.setdefault("ucloud.client", _client_mod)

# Tight budgets so tests finish in seconds.
os.environ["KEEPALIVE_PER_INSTANCE_BUDGET_SEC"] = "40"
os.environ["KEEPALIVE_GLOBAL_BUDGET_SEC"] = "60"
os.environ["KEEPALIVE_TRANSITION_GRACE_SEC"] = "3"
os.environ["KEEPALIVE_STARTUP_WAIT_SEC"] = "4"
os.environ["KEEPALIVE_STOP_WAIT_SEC"] = "3"
os.environ["KEEPALIVE_MAX_ROUNDS"] = "3"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keepalive  # noqa: E402

keepalive.POLL_INTERVAL_SEC = 0.2
keepalive.API_RETRY_DELAY_SEC = 0
keepalive.TRANSITION_GRACE_SEC = 1
keepalive.STARTUP_WAIT_SEC = 2
keepalive.STOP_WAIT_SEC = 2

SEVEN_DAYS = 7 * 24 * 3600


class Sim:
    """Fake CompShare backend modelling state transitions and release clocks.

    transitions maps (current_state, action) -> next_state; a missing entry
    keeps the current state, which models an instance that ignores the request
    (the real-world stuck case).

    On `stop` the release clock is refreshed exactly like the real API:
    StopTime = now, ReleaseTime = now + 7 days.
    """

    def __init__(self, name, initial_state, transitions=None, start_fails=False,
                 clock_exposed=True, clock_refresh_on_stop=True):
        self.name = name
        self.state = initial_state
        self.transitions = transitions or {}
        self.start_fails = start_fails
        self.clock_exposed = clock_exposed
        self.clock_refresh_on_stop = clock_refresh_on_stop
        self.now = 1_800_000_000  # fixed base epoch
        self.stop_time = self.now - SEVEN_DAYS
        self.release_time = self.now
        self.start_calls = 0
        self.stop_calls = 0
        self.describe_calls = 0

    def _payload(self, uhost_id):
        inst = {"State": self.state, "UHostId": uhost_id}
        if self.clock_exposed:
            inst["StopTime"] = self.stop_time
            inst["ReleaseTime"] = self.release_time
        return inst

    def handle(self, action, params):
        if action == "DescribeCompShareInstance":
            self.describe_calls += 1
            return {"RetCode": 0, "UHostSet": [self._payload(params["UHostIds"][0])]}
        if action == "StartCompShareInstance":
            self.start_calls += 1
            if self.start_fails:
                return {"RetCode": 1, "Message": "StartCompShareInstance failed"}
            self.state = self.transitions.get((self.state, "start"), self.state)
            return {"RetCode": 0}
        if action == "StopCompShareInstance":
            self.stop_calls += 1
            self.state = self.transitions.get((self.state, "stop"), self.state)
            if self.clock_refresh_on_stop:
                self.now += 60  # a minute passes between snapshot and refresh
                self.stop_time = self.now
                self.release_time = self.now + SEVEN_DAYS
            return {"RetCode": 0}
        raise AssertionError(action)


class FakeClient:
    def __init__(self, sim):
        self.sim = sim

    def ucompshare(self):
        return self

    def invoke(self, action, params):
        return self.sim.handle(action, params)


def run(sim, label, clock_before=None):
    inst = {"UHostId": "uhost-test", "Name": label, "Region": "cn-wlcb", "Zone": "cn-wlcb-01"}
    if clock_before:
        inst["StopTime"], inst["ReleaseTime"] = clock_before
    started = time.time()
    ok = keepalive.ensure_running(FakeClient(sim), inst, global_deadline=time.time() + 40)
    elapsed = time.time() - started
    print(f"  -> {label}: ok={ok} elapsed={elapsed:.1f}s "
          f"start={sim.start_calls} stop={sim.stop_calls} final={sim.state}")
    return ok, elapsed


FAILURES = []


def check(cond, message):
    if not cond:
        FAILURES.append(message)


def main():
    # 1) THE production case: 无卡模式 start never reaches Running, but the stop
    #    refreshes the release clock. This must now report SUCCESS.
    print("Case 1: start stuck in Initializing, stop still refreshes the clock")
    s = Sim("slow-start", "Initializing", transitions={
        ("Initializing", "stop"): "Stopped",
    })
    ok, elapsed = run(s, "startup-slow")
    check(ok, "Case 1: should succeed because the release clock advanced")
    check(s.stop_calls >= 1, "Case 1: must send a stop")
    check(s.state == "Stopped", f"Case 1: should end Stopped, got {s.state}")
    check(elapsed < 15, f"Case 1: must not hang, took {elapsed:.1f}s")

    # 2) Healthy cycle: Stopped -> Running -> Stopped.
    print("Case 2: normal Stopped -> Running -> Stopped cycle")
    s = Sim("healthy", "Stopped", transitions={
        ("Stopped", "start"): "Running",
        ("Running", "stop"): "Stopped",
    })
    ok, elapsed = run(s, "healthy")
    check(ok, "Case 2: should succeed")
    check(s.start_calls == 1, f"Case 2: expected 1 start, got {s.start_calls}")
    check(s.state == "Stopped", f"Case 2: should end Stopped, got {s.state}")

    # 3) Already Running: only the stop half is needed.
    print("Case 3: already Running")
    s = Sim("running", "Running", transitions={("Running", "stop"): "Stopped"})
    ok, elapsed = run(s, "already-running")
    check(ok, "Case 3: should succeed")
    check(s.start_calls == 0, "Case 3: must not start an already-running instance")

    # 4) Stop is rejected and the clock never moves -> genuine failure.
    print("Case 4: stop rejected and clock frozen")
    s = Sim("frozen", "Running", clock_refresh_on_stop=False)
    ok, elapsed = run(s, "clock-frozen")
    check(not ok, "Case 4: should fail when the release clock never advances")
    check(elapsed < 20, f"Case 4: must stay bounded, took {elapsed:.1f}s")

    # 5) API omits the clock fields -> fall back to the state signal.
    print("Case 5: clocks not exposed -> state-based fallback")
    s = Sim("noclocks", "Running", clock_exposed=False,
            transitions={("Running", "stop"): "Stopped"})
    ok, elapsed = run(s, "no-clocks")
    check(ok, "Case 5: should fall back to the Stopped signal and succeed")

    # 6) Failed state -> stop to refresh the clock.
    print("Case 6: failure state 初始化失败")
    s = Sim("failed", "初始化失败", transitions={("初始化失败", "stop"): "Stopped"})
    ok, elapsed = run(s, "failed-state")
    check(ok, "Case 6: should succeed by refreshing the clock")
    check(elapsed < 10, f"Case 6: must not wait long, took {elapsed:.1f}s")

    # 7) Budget already gone -> still attempt the stop (best effort), report fail.
    print("Case 7: no budget left")
    s = Sim("nobudget", "Initializing")
    inst = {"UHostId": "uhost-test", "Name": "nobudget", "Region": "cn-wlcb", "Zone": "cn-wlcb-01"}
    ok = keepalive.ensure_running(FakeClient(s), inst, global_deadline=time.time() - 1)
    print(f"  -> nobudget: ok={ok} stop={s.stop_calls}")
    check(s.stop_calls >= 1, f"Case 7: expected a best-effort stop, got {s.stop_calls}")

    # 8) Clock must actually be compared, not assumed: an unchanged clock with a
    #    Stopped state is still a failure.
    print("Case 8: unchanged clock must not be treated as success")
    before = (1_800_000_000, 1_800_000_000 - SEVEN_DAYS)
    s = Sim("stale", "Running", clock_refresh_on_stop=False)
    ok, _ = run(s, "stale-clock", clock_before=before)
    check(not ok, "Case 8: unchanged release clock must fail")

    print()
    if FAILURES:
        print("FAILURES:")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("ALL STATE-MACHINE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
