"""
compshare keepalive script
Wakes up stopped instances in without-gpu mode to prevent them from being reclaimed.
"""

import os
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, wait

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# The SDK dumps every request/response body at INFO level (hundreds of KB per run).
logging.getLogger("ucloud").setLevel(logging.WARNING)

try:
    from ucloud.client import Client
except ImportError:
    logger.error("Please install ucloud-sdk-python3: pip install --upgrade ucloud-sdk-python3")
    sys.exit(1)


API_BASE_URL = "https://api.compshare.cn"

# ---------------------------------------------------------------------------
# Timing budget. Every wait loop is clamped by the remaining budget, so a run
# can never outlive GLOBAL_BUDGET_SEC (the workflow timeout must be bigger).
# ---------------------------------------------------------------------------
POLL_INTERVAL_SEC = int(os.environ.get("KEEPALIVE_POLL_INTERVAL_SEC", "10"))
API_MAX_RETRIES = 3
API_RETRY_DELAY_SEC = 5
# Per-request HTTP timeout. CompShare's list endpoint can legitimately take
# 20-40s, so this must stay generous; the run-level budgets below are what
# actually bound the job.
API_TIMEOUT_SEC = int(os.environ.get("KEEPALIVE_API_TIMEOUT_SEC", "60"))

# Hard cap for a single instance (start + wait + stop + recovery).
PER_INSTANCE_BUDGET_SEC = int(os.environ.get("KEEPALIVE_PER_INSTANCE_BUDGET_SEC", "540"))
# Hard cap for the whole run. Workflow timeout-minutes must exceed this.
GLOBAL_BUDGET_SEC = int(os.environ.get("KEEPALIVE_GLOBAL_BUDGET_SEC", "900"))
# How long to passively watch a transitional state before forcing a recovery.
TRANSITION_GRACE_SEC = int(os.environ.get("KEEPALIVE_TRANSITION_GRACE_SEC", "90"))
# How long to wait for Running after a successful Start call. Real "无卡模式"
# starts of heavy community images (e.g. Minimax/ComfyUI) routinely exceed 3
# minutes, so this must stay generous while remaining under the per-instance
# budget together with the stop and recovery windows.
STARTUP_WAIT_SEC = int(os.environ.get("KEEPALIVE_STARTUP_WAIT_SEC", "300"))
# How long to wait for Stopped after a successful Stop call.
STOP_WAIT_SEC = int(os.environ.get("KEEPALIVE_STOP_WAIT_SEC", "150"))
# Rounds of "act -> observe -> recover" per instance. Each round can consume a
# full STARTUP_WAIT_SEC, so this stays small to avoid burning the global budget
# on a single stubborn instance.
MAX_RECOVERY_ROUNDS = int(os.environ.get("KEEPALIVE_MAX_ROUNDS", "2"))
# Minimum remaining budget required before starting another action. Starting a
# recovery with less time than this only wastes API calls.
MIN_ACTION_BUDGET_SEC = int(os.environ.get("KEEPALIVE_MIN_ACTION_BUDGET_SEC", "30"))
# Cap for the initial instance listing, so a slow API cannot eat the whole run.
LIST_BUDGET_SEC = int(os.environ.get("KEEPALIVE_LIST_BUDGET_SEC", "240"))
LIST_RETRY_DELAY_SEC = int(os.environ.get("KEEPALIVE_LIST_RETRY_DELAY_SEC", "15"))
# Parallel workers (each gets its own API client). CompShare's 无卡模式 appears to
# have a limited CPU-only stream pool, so concurrent starts can leave every
# instance stuck in Initializing — keep this low and raise it only if verified.
MAX_WORKERS = int(os.environ.get("KEEPALIVE_MAX_WORKERS", "1"))
# Exit non-zero when any instance could not be cycled, so Actions reports it.
FAIL_ON_ERROR = os.environ.get("KEEPALIVE_FAIL_ON_ERROR", "true").strip().lower() not in (
    "0", "false", "no", "off",
)

RUNNING_STATES = {"Running", "running"}
STOPPED_STATES = {"Stopped", "stopped"}
# Normal transitional states: wait a little, they usually resolve themselves.
TRANSITIONAL_STATES = {
    "Initializing", "Starting", "Restarting", "Stopping", "Pending",
    "Creating", "ShuttingDown", "Rebooting", "Upgrading", "Migrating",
    "初始化中", "启动中", "停止中", "重启中",
}
# Failure/abnormal states: waiting is pointless, force a recovery immediately.
FAILED_STATES = {
    "InitializeFailed", "StartFailed", "StopFailed", "Error", "Abnormal",
    "Failure", "StoppedWithError", "Unknown", "初始化失败", "启动失败", "异常",
}

# Without-gpu spec: A = 2核4G, B = 8核16G
WITHOUT_GPU_SPEC = "A"


def _invoke(client: Client, action: str, params: dict, deadline: float | None = None,
            max_attempts: int = API_MAX_RETRIES) -> dict:
    """Call an API action with retries that are bounded by `deadline`.

    The first attempt always runs (each HTTP call is already capped by
    API_TIMEOUT_SEC); only retries are skipped once the budget is gone, so a
    near-expired deadline degrades to "one attempt" rather than "no call".
    Network errors are returned as a synthetic RetCode so callers have a single
    failure shape to handle.
    """
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            return client.ucompshare().invoke(action, params)
        except Exception as e:  # noqa: BLE001 - SDK raises several exception types
            last_error = str(e)
            logger.warning(f"[{params.get('UHostId', '-')}] {action} exception "
                           f"(attempt {attempt}/{max_attempts}): {e}")
        if attempt >= max_attempts:
            break
        if deadline is not None and _remaining(deadline) <= API_RETRY_DELAY_SEC:
            last_error = f"{last_error} (no budget left to retry)"
            break
        time.sleep(API_RETRY_DELAY_SEC)
    return {"RetCode": -1, "Message": last_error or "request failed"}


def get_client(public_key: str, private_key: str) -> Client:
    # `timeout` caps each HTTP call so a stalled connection can never hang a
    # worker indefinitely; retries are handled by this script instead.
    return Client({
        "region": "",
        "public_key": public_key,
        "private_key": private_key,
        "base_url": API_BASE_URL,
        "timeout": API_TIMEOUT_SEC,
        "max_retries": 0,
        "log_level": logging.WARNING,
    })


def list_instances(client: Client, limit: int = 100, deadline: float | None = None) -> list[dict]:
    """Fetch all instances with pagination support."""
    all_instances = []
    offset = 0
    while True:
        resp = _invoke(client, "DescribeCompShareInstance",
                       {"Limit": limit, "Offset": offset}, deadline=deadline)
        if resp.get("RetCode") != 0:
            raise RuntimeError(f"DescribeCompShareInstance failed: {resp.get('Message')}")
        instances = resp.get("UHostSet", [])
        if not instances:
            break
        all_instances.extend(instances)
        total = resp.get("TotalCount", 0)
        if offset + limit >= total:
            break
        offset += limit
    return all_instances


def describe_instance(client: Client, uhost_id: str, region: str,
                      deadline: float | None = None) -> dict | None:
    """Return the instance dict, or None when the API did not return it."""
    resp = _invoke(client, "DescribeCompShareInstance",
                   {"Region": region, "UHostIds": [uhost_id]}, deadline=deadline)
    if resp.get("RetCode") != 0:
        logger.warning(f"[{uhost_id}] Describe failed: {resp.get('Message')}")
        return None
    instances = resp.get("UHostSet", [])
    if instances:
        return instances[0]
    if resp.get("TotalCount", 0) > 0:
        # Rare: the API returned a page without our instance. Fall back to a
        # full listing, which is the only way to resolve it.
        try:
            for inst in list_instances(client):
                if inst.get("UHostId") == uhost_id:
                    return inst
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{uhost_id}] fallback listing failed: {e}")
    logger.warning(f"[{uhost_id}] Describe returned no instance")
    return None


def wait_for_any_state(
    client: Client,
    uhost_id: str,
    region: str,
    targets: set[str],
    timeout_sec: float,
    label: str,
) -> tuple[str, dict] | tuple[None, None]:
    """Poll until the instance reaches one of `targets`.

    Returns (state, instance) on success, (None, None) on timeout. The wait is
    also abandoned early when the instance falls into a failed state, so the
    caller can start recovery immediately instead of burning the whole budget.
    """
    deadline = time.time() + max(0.0, timeout_sec)
    last_state = None
    first_poll = True
    while first_poll or time.time() < deadline:
        first_poll = False
        inst = describe_instance(client, uhost_id, region, deadline=deadline)
        if inst is None:
            logger.warning(f"[{uhost_id}] Instance not visible while waiting for {label}")
            time.sleep(min(POLL_INTERVAL_SEC, max(0.0, deadline - time.time())))
            continue
        state = inst.get("State", "")
        if state in targets:
            return state, inst
        if state in FAILED_STATES:
            logger.warning(f"[{uhost_id}] Hit failed state '{state}' while waiting for {label}")
            return None, None
        if state != last_state:
            logger.info(f"[{uhost_id}] state={state} (waiting for {label})")
            last_state = state
        time.sleep(min(POLL_INTERVAL_SEC, max(0.0, deadline - time.time())))
    logger.info(f"[{uhost_id}] waited {int(timeout_sec)}s for {label}; last state={last_state}")
    return None, None


def _remaining(deadline: float) -> float:
    return deadline - time.time()


def _inner_budget(deadline: float, cap: float) -> float:
    """Time a nested wait may use: the cap, minus one poll interval as margin,
    and never more than what is actually left on the parent deadline."""
    return max(min(_remaining(deadline) - POLL_INTERVAL_SEC, cap), 0.0)


def _finish_cycle(client: Client, uhost_id: str, name: str, region: str, zone: str,
                  deadline: float, clock_before: tuple[int, int] | None,
                  note: str = "") -> bool:
    """Stop the instance and confirm the reclamation countdown moved forward.

    Success is judged by the *business* signal (StopTime/ReleaseTime advanced),
    not by reaching Running. This matters because CompShare's 无卡模式 start can
    stay in Initializing far longer than any sane wait budget, yet the Stop that
    follows still refreshes the 7-day reclamation timer — which is the whole
    point of keepalive.
    """
    _stop_instance(client, uhost_id, region, zone, deadline=deadline)
    state, inst = wait_for_any_state(
        client, uhost_id, region, STOPPED_STATES,
        _inner_budget(deadline, STOP_WAIT_SEC), "Stopped"
    )
    if inst is None:
        inst = describe_instance(client, uhost_id, region, deadline=deadline)
    clock_after = _release_clock(inst)
    verdict = _reclaim_deadline_advanced(clock_before, clock_after)

    if verdict is True:
        before_rel, _ = clock_before
        after_rel, _ = clock_after
        delta = f"+{int(after_rel - before_rel)}s" if before_rel and after_rel else "advanced"
        logger.info(f"[{uhost_id}] {name} keepalive confirmed: release clock "
                    f"extended{note} ({delta}, "
                    f"new release={_fmt_ts(after_rel)})")
        return True
    if verdict is False:
        logger.warning(f"[{uhost_id}] {name} release clock did NOT advance"
                       f"{note} (before={_fmt_ts(clock_before[0])}, "
                       f"after={_fmt_ts(clock_after[0])})")
        return False

    # The API did not expose the clocks, so fall back to the state signal.
    if state is not None:
        logger.info(f"[{uhost_id}] {name} stopped successfully (release clock not exposed)")
        return True
    logger.warning(f"[{uhost_id}] {name} could not be confirmed as stopped{note}")
    return False


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "n/a"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def ensure_running(client: Client, instance: dict, global_deadline: float | None = None,
                   instance_budget: float | None = None) -> bool:
    """Keep an instance alive and finish with it Stopped.

    The objective is to push back CompShare's 7-day reclamation countdown
    (ReleaseTime = StopTime + 7 days). Because the countdown is refreshed by the
    stop, the cycle is: wake the instance, then stop it, then verify that the
    release clock actually advanced.

    Each round re-reads the live state and dispatches:
      * Running          -> stop, verify
      * Stopped          -> start, wait up to STARTUP_WAIT_SEC for Running,
                            then stop and verify
      * transitional     -> watch for a short grace period, then stop and verify
      * failed/unknown   -> stop and verify immediately
    All waits are clamped by a hard per-instance and global deadline, so any
    instance finishes inside its budget even when startup never completes.
    """
    uhost_id = instance["UHostId"]
    name = instance.get("Name", "?")
    region = instance.get("Region", "")
    zone = instance.get("Zone", "")

    # Both the start waiting and the final cleanup need their own room, so the
    # action deadline is smaller than the overall instance deadline. Without this
    # split a slow startup would leave nothing for the stop that actually
    # refreshes the reclamation clock.
    budget = PER_INSTANCE_BUDGET_SEC if instance_budget is None else instance_budget
    instance_deadline = time.time() + budget
    if global_deadline:
        instance_deadline = min(instance_deadline, global_deadline)
    cleanup_reserve = min(STOP_WAIT_SEC, max(10, budget * 0.3))
    deadline = instance_deadline - cleanup_reserve
    if deadline <= time.time():
        # Too late to start anything: still do the best-effort cleanup.
        logger.warning(f"[{uhost_id}] {name} no time left to cycle; cleanup only")
        return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                             _release_clock(instance), note=" [no-budget]")

    original_clock = _release_clock(instance)
    if original_clock:
        logger.info(f"[{uhost_id}] {name} release clock before: "
                    f"stop={_fmt_ts(original_clock[1])}, release={_fmt_ts(original_clock[0])}")

    for round_no in range(1, MAX_RECOVERY_ROUNDS + 1):
        if _remaining(deadline) < MIN_ACTION_BUDGET_SEC:
            logger.warning(f"[{uhost_id}] {name} out of action budget (round {round_no})")
            return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                 original_clock, note=" [budget-exhausted cleanup]")

        inst = describe_instance(client, uhost_id, region, deadline=deadline)
        if inst is None:
            logger.warning(f"[{uhost_id}] {name} state unknown — stopping to refresh the clock")
            return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                 original_clock, note=" [state-unknown]")

        state = inst.get("State", "")
        clock_before = _release_clock(inst) or original_clock
        logger.info(f"[{uhost_id}] {name} round {round_no}/{MAX_RECOVERY_ROUNDS}, "
                    f"state={state}, budget left={int(_remaining(deadline))}s")

        # 1. Already Running -> the stop half alone completes the keepalive.
        if state in RUNNING_STATES:
            logger.info(f"[{uhost_id}] {name} is Running — shutting down")
            return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                 clock_before)

        # 2. Stopped -> wake it up first (that is what makes the stop meaningful).
        if state in STOPPED_STATES:
            logger.info(f"[{uhost_id}] {name} is Stopped — waking up "
                        f"(WithoutGpuSpec={WITHOUT_GPU_SPEC}, region={region})")
            if not _start_instance(client, uhost_id, region, zone, deadline=deadline):
                logger.warning(f"[{uhost_id}] {name} start request failed — stopping anyway")
                return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                     clock_before, note=" [start-rejected]")
            reached, _ = wait_for_any_state(
                client, uhost_id, region, RUNNING_STATES,
                _inner_budget(deadline, STARTUP_WAIT_SEC), "Running"
            )
            if reached is None:
                # Slow or stuck startup is expected for heavy community images.
                # The instance was resumed, so the stop below still refreshes the
                # reclamation clock — do not fail just because Running was missed.
                logger.warning(f"[{uhost_id}] {name} did not reach Running within "
                               f"{STARTUP_WAIT_SEC}s — stopping to refresh the clock")
                return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                     clock_before, note=" [startup-slow]")
            logger.info(f"[{uhost_id}] {name} is Running — shutting down")
            return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                 clock_before)

        # 3. Transitional -> brief grace period, then stop to refresh the clock.
        if state in TRANSITIONAL_STATES:
            logger.info(f"[{uhost_id}] {name} is '{state}' — observing up to {TRANSITION_GRACE_SEC}s")
            reached, _ = wait_for_any_state(
                client, uhost_id, region, RUNNING_STATES | STOPPED_STATES,
                _inner_budget(deadline, TRANSITION_GRACE_SEC), "Running/Stopped"
            )
            if reached in RUNNING_STATES:
                logger.info(f"[{uhost_id}] {name} reached Running — shutting down")
            else:
                logger.warning(f"[{uhost_id}] {name} still '{state}' — stopping to refresh the clock")
            return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                                 clock_before,
                                 note="" if reached in RUNNING_STATES else " [transitional]")

        # 4. Failed / unknown state -> stop to refresh the clock.
        logger.warning(f"[{uhost_id}] {name} state '{state}' — stopping to refresh the clock")
        return _finish_cycle(client, uhost_id, name, region, zone, instance_deadline,
                             clock_before, note=" [abnormal-state]")

    logger.error(f"[{uhost_id}] {name} exhausted rounds without confirming keepalive")
    return False


def _release_clock(inst: dict | None) -> tuple[int, int] | None:
    """Return (ReleaseTime, StopTime) when the API exposes them.

    The keepalive goal is not "the instance reached Running" — it is "the
    reclamation countdown was pushed back". CompShare sets
    ReleaseTime = StopTime + 7 days, so an advanced StopTime/ReleaseTime proves
    the instance was kept alive. Returns None when the API omits both fields
    (then callers fall back to state-based success).
    """
    if not inst:
        return None
    release = inst.get("ReleaseTime")
    stop = inst.get("StopTime")
    if not release and not stop:
        return None
    return int(release or 0), int(stop or 0)


def _reclaim_deadline_advanced(before: tuple[int, int] | None,
                               after: tuple[int, int] | None) -> bool | None:
    """True/False when both clocks are known, None when unverifiable."""
    if before is None or after is None:
        return None
    return after[0] > before[0] or after[1] > before[1]


def _start_instance(client: Client, uhost_id: str, region: str, zone: str,
                    deadline: float | None = None) -> bool:
    logger.info(f"[{uhost_id}] starting without GPU (WithoutGpuSpec={WITHOUT_GPU_SPEC})")
    params = {"Region": region, "UHostId": uhost_id, "WithoutGpuSpec": WITHOUT_GPU_SPEC}
    if zone:
        params["Zone"] = zone
    resp = _invoke(client, "StartCompShareInstance", params, deadline=deadline)
    if resp.get("RetCode") == 0:
        logger.info(f"[{uhost_id}] StartCompShareInstance OK")
        return True
    message = resp.get("Message", "")
    logger.error(f"[{uhost_id}] Start failed: {message}")
    # Answers like "instance is already running" are not real failures.
    lowered = message.lower()
    if "already" in lowered or "running" in lowered:
        logger.info(f"[{uhost_id}] treating start response as benign")
        return True
    return False


def _stop_instance(client: Client, uhost_id: str, region: str, zone: str,
                   deadline: float | None = None, max_attempts: int = API_MAX_RETRIES) -> bool:
    params = {"Region": region, "UHostId": uhost_id}
    if zone:
        params["Zone"] = zone
    resp = _invoke(client, "StopCompShareInstance", params,
                   deadline=deadline, max_attempts=max_attempts)
    if resp.get("RetCode") == 0:
        logger.info(f"[{uhost_id}] StopCompShareInstance OK")
        return True
    message = resp.get("Message", "")
    logger.warning(f"[{uhost_id}] Stop failed: {message}")
    lowered = message.lower()
    if "already" in lowered or "stopped" in lowered:
        logger.info(f"[{uhost_id}] treating stop response as benign")
        return True
    return False


def _parse_only(argv: list[str]) -> set[str]:
    """--only <id>[,<id>...] restricts the run to specific instances."""
    if "--only" in argv:
        idx = argv.index("--only")
        if idx + 1 < len(argv):
            return {item.strip() for item in argv[idx + 1].split(",") if item.strip()}
    return set()


def _dry_run_requested(argv: list[str]) -> bool:
    """--dry-run only reports instance states and never changes anything."""
    return "--dry-run" in argv or os.environ.get("KEEPALIVE_DRY_RUN", "").lower() in (
        "1", "true", "yes", "on",
    )


def process_instance(public_key: str, private_key: str, instance: dict,
                     global_deadline: float, instance_budget: float) -> tuple[str, bool]:
    """Worker entry point: each instance gets its own client (clients are not thread-safe).

    `instance_budget` is applied here, at the moment the instance is actually
    processed, rather than when the task was queued. With serial workers the
    queueing delay can be many minutes, so a deadline computed at submit time
    would leave the later instances with no usable budget at all.
    """
    uhost_id = instance.get("UHostId", "?")
    name = instance.get("Name", "?")
    client = get_client(public_key, private_key)
    try:
        ok = ensure_running(client, instance, global_deadline=global_deadline,
                            instance_budget=instance_budget)
    except Exception as e:  # noqa: BLE001 - never let one instance kill the run
        logger.error(f"[{uhost_id}] {name} unexpected error: {e}")
        ok = False
    logger.info(f"[{uhost_id}] {name} => {'OK' if ok else 'FAILED'}")
    return uhost_id, ok


def report_states(client: Client, instances: list[dict]) -> None:
    """Read-only survey of every instance and the state it is currently in."""
    logger.info(f"{'INSTANCE':<24} {'NAME':<18} {'REGION':<10} STATE")
    for inst in instances:
        uhost_id = inst.get("UHostId", "?")
        region = inst.get("Region", "")
        live = describe_instance(client, uhost_id, region)
        if live is None:
            state = "<unreachable>"
        else:
            state = live.get("State", "?")
        logger.info(f"{uhost_id:<24} {inst.get('Name', '?'):<18} {region:<10} {state}")


def main():
    public_key = os.environ.get("COMPSHARE_PUBLIC_KEY", "")
    private_key = os.environ.get("COMPSHARE_PRIVATE_KEY", "")
    if not public_key or not private_key:
        logger.error('Set COMPSHARE_PUBLIC_KEY and COMPSHARE_PRIVATE_KEY environment variables')
        sys.exit(1)

    dry_run = _dry_run_requested(sys.argv[1:])
    only = _parse_only(sys.argv[1:])
    global_deadline = time.time() + GLOBAL_BUDGET_SEC

    client = get_client(public_key, private_key)
    logger.info(
        f"Budget: global={GLOBAL_BUDGET_SEC}s, per-instance<= {PER_INSTANCE_BUDGET_SEC}s, "
        f"workers={MAX_WORKERS}, transition-grace={TRANSITION_GRACE_SEC}s, "
        f"startup-wait={STARTUP_WAIT_SEC}s, dry-run={dry_run}"
    )

    logger.info("Listing all instances...")
    list_deadline = min(global_deadline, time.time() + LIST_BUDGET_SEC)
    instances = []
    while True:
        try:
            instances = list_instances(client, deadline=list_deadline)
            break
        except Exception as e:  # noqa: BLE001 - transient API/network failures
            remaining = _remaining(list_deadline)
            logger.error(f"Failed to list instances: {e}")
            if remaining <= LIST_RETRY_DELAY_SEC:
                logger.error("Giving up on instance listing")
                sys.exit(1)
            logger.info(f"Retrying listing in {LIST_RETRY_DELAY_SEC}s "
                        f"(budget left={int(remaining)}s)")
            time.sleep(LIST_RETRY_DELAY_SEC)

    if only:
        instances = [i for i in instances if i.get("UHostId") in only]
    logger.info(f"Total instances to process: {len(instances)}")

    if not instances:
        logger.info("Nothing to do.")
        return

    for inst in instances:
        logger.info(
            f"  - {inst.get('UHostId')} | {inst.get('Name', '?')} | "
            f"state={inst.get('State', '?')} | {inst.get('Region', '?')}"
        )

    if dry_run:
        report_states(client, instances)
        logger.info("Dry run finished; no instance was modified.")
        return

    # Keep the CPU-only stream pool from being over-subscribed (see MAX_WORKERS)
    # and give every instance a fair share of what is left, so one stubborn
    # instance cannot consume the whole run. The reserve only needs to cover the
    # final stop request, not a full startup wait.
    workers = max(1, min(MAX_WORKERS, len(instances)))
    reserve = STOP_WAIT_SEC + API_TIMEOUT_SEC
    available = max(0.0, _remaining(global_deadline) - reserve)
    fair_share = available / len(instances) if instances else 0.0
    instance_budget = max(min(PER_INSTANCE_BUDGET_SEC, fair_share), MIN_ACTION_BUDGET_SEC)
    logger.info(f"Per-instance budget for this run: <= {int(instance_budget)}s "
                f"(fair share of {int(available)}s across {len(instances)} instances)")

    results: dict[str, bool] = {}
    pending = set()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(process_instance, public_key, private_key, inst,
                            global_deadline, instance_budget): inst
            for inst in instances
        }
        done, pending = wait(futures, timeout=max(0, _remaining(global_deadline)))
        for future in done:
            inst = futures[future]
            uhost_id = inst.get("UHostId", "?")
            try:
                _, ok = future.result()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{uhost_id}] worker crashed: {e}")
                ok = False
            results[uhost_id] = ok
        if pending:
            leftover = ", ".join(futures[f].get("UHostId", "?") for f in pending)
            logger.error(f"Global budget of {GLOBAL_BUDGET_SEC}s exhausted; abandoning: {leftover}")
    finally:
        # Do not wait for stuck workers: threads that are mid-HTTP cannot be
        # cancelled, so the process exits below instead of hanging until the
        # workflow timeout.
        executor.shutdown(wait=False)

    ok_count = sum(1 for v in results.values() if v)
    logger.info(f"Summary: {ok_count}/{len(instances)} instances cycled successfully "
                f"(elapsed={int(GLOBAL_BUDGET_SEC - _remaining(global_deadline))}s)")

    if pending:
        logger.error("Run ended with unfinished instances")
    if FAIL_ON_ERROR and (pending or ok_count != len(instances)):
        # Flush logs, then hard-exit so abandoned threads cannot block the job,
        # but still report a failure to GitHub Actions.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


if __name__ == "__main__":
    main()
