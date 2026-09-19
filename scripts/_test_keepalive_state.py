"""Local simulation of keepalive state handling. Not part of the workflow.

Run:  python scripts/_test_keepalive_state.py
It stubs the UCloud SDK and drives the state machine with a fake CompShare API
so every abnormal-state path can be verified without network access.
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


class Sim:
    """Fake CompShare backend.

    state_transitions maps (current_state, action) -> next_state. A missing
    entry keeps the current state, which models an instance that ignores the
    request (the real-world "stuck" case).
    """

    def __init__(self, name, initial_state, transitions=None, start_fails=False):
        self.name = name
        self.state = initial_state
        self.transitions = transitions or {}
        self.start_fails = start_fails
        self.start_calls = 0
        self.stop_calls = 0
        self.describe_calls = 0

    def handle(self, action, params):
        if action == "DescribeCompShareInstance":
            self.describe_calls += 1
            return {"RetCode": 0, "UHostSet": [{"State": self.state, "UHostId": params["UHostIds"][0]}]}
        if action == "StartCompShareInstance":
            self.start_calls += 1
            if self.start_fails:
                return {"RetCode": 1, "Message": "StartCompShareInstance failed"}
            self.state = self.transitions.get((self.state, "start"), self.state)
            return {"RetCode": 0}
        if action == "StopCompShareInstance":
            self.stop_calls += 1
            self.state = self.transitions.get((self.state, "stop"), self.state)
            return {"RetCode": 0}
        raise AssertionError(action)


class FakeClient:
    def __init__(self, sim):
        self.sim = sim

    def ucompshare(self):
        return self

    def invoke(self, action, params):
        return self.sim.handle(action, params)


def run(sim, label):
    inst = {"UHostId": "uhost-test", "Name": label, "Region": "cn-wlcb", "Zone": "cn-wlcb-01"}
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
    # 1) Stuck in Initializing forever: must force-recover, give up quickly, and
    #    never sit in a 600s wait.
    print("Case 1: stuck in Initializing forever")
    s = Sim("stuck", "Initializing")  # ignores both start and stop
    ok, elapsed = run(s, "stuck-initializing")
    check(not ok, "Case 1: should not report success")
    check(s.stop_calls > 0, "Case 1: must attempt recovery stops")
    check(elapsed < 15, f"Case 1: must give up fast, took {elapsed:.1f}s")

    # 2) Stuck Initializing that a stop request clears -> then normal cycle works.
    print("Case 2: Initializing cleared by forced stop")
    s = Sim("recover", "Initializing", transitions={
        ("Initializing", "stop"): "Stopped",
        ("Stopped", "start"): "Running",
        ("Running", "stop"): "Stopped",
    })
    ok, elapsed = run(s, "recover-then-cycle")
    check(ok, "Case 2: should succeed")
    check(s.start_calls == 1, f"Case 2: expected exactly 1 start, got {s.start_calls}")
    check(s.stop_calls == 2, f"Case 2: expected 2 stops, got {s.stop_calls}")
    check(s.state == "Stopped", f"Case 2: should end Stopped, got {s.state}")

    # 3) Chinese failure state: recovery must be immediate, not a long wait.
    print("Case 3: failed state 初始化失败")
    s = Sim("failed", "初始化失败", transitions={
        ("初始化失败", "stop"): "Stopped",
        ("Stopped", "start"): "Running",
        ("Running", "stop"): "Stopped",
    })
    ok, elapsed = run(s, "failed-state")
    check(ok, "Case 3: should succeed")
    check(elapsed < 10, f"Case 3: must not wait long, took {elapsed:.1f}s")

    # 4) Start always rejected: bounded behaviour, no hang.
    print("Case 4: Start always fails")
    s = Sim("starterr", "Stopped", start_fails=True)
    ok, elapsed = run(s, "start-error")
    check(not ok, "Case 4: should report failure")
    check(elapsed < 25, f"Case 4: must stay bounded, took {elapsed:.1f}s")
    # Each round performs at most API_MAX_RETRIES attempts, and there are at
    # most MAX_RECOVERY_ROUNDS rounds.
    check(s.start_calls <= keepalive.MAX_RECOVERY_ROUNDS * keepalive.API_MAX_RETRIES,
          f"Case 4: too many start attempts {s.start_calls}")

    # 5) Unknown state -> recovery path -> normal cycle.
    print("Case 5: unknown state")
    s = Sim("unknown", "SomethingWeird", transitions={
        ("SomethingWeird", "stop"): "Stopped",
        ("Stopped", "start"): "Running",
        ("Running", "stop"): "Stopped",
    })
    ok, elapsed = run(s, "unknown-state")
    check(ok, "Case 5: should succeed")

    # 6) Already Running: only needs the stop half of the cycle.
    print("Case 6: already Running")
    s = Sim("running", "Running", transitions={("Running", "stop"): "Stopped"})
    ok, elapsed = run(s, "already-running")
    check(ok, "Case 6: should succeed")
    check(s.start_calls == 0, "Case 6: must not start an already-running instance")
    check(s.stop_calls == 1, f"Case 6: expected 1 stop, got {s.stop_calls}")

    # 7) Start works but it never reaches Running (the real timeout cause).
    print("Case 7: never reaches Running after start")
    s = Sim("nostart", "Stopped", transitions={
        ("Stopped", "start"): "Initializing",  # stuck there
        ("Initializing", "stop"): "Stopped",
    })
    ok, elapsed = run(s, "never-running")
    check(not ok, "Case 7: should report failure after bounded retries")
    check(elapsed < 30, f"Case 7: must respect budget, took {elapsed:.1f}s")

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
