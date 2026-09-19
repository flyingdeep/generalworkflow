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
# How long to wait for Running after a successful Start call.
STARTUP_WAIT_SEC = int(os.environ.get("KEEPALIVE_STARTUP_WAIT_SEC", "180"))
# How long to wait for Stopped after a successful Stop call.
STOP_WAIT_SEC = int(os.environ.get("KEEPALIVE_STOP_WAIT_SEC", "150"))
# Rounds of "act -> observe -> recover" per instance.
MAX_RECOVERY_ROUNDS = int(os.environ.get("KEEPALIVE_MAX_ROUNDS", "3"))
# Minimum remaining budget required before starting another action. Starting a
# recovery with less time than this only wastes API calls.
MIN_ACTION_BUDGET_SEC = int(os.environ.get("KEEPALIVE_MIN_ACTION_BUDGET_SEC", "30"))
# Cap for the initial instance listing, so a slow API cannot eat the whole run.
LIST_BUDGET_SEC = int(os.environ.get("KEEPALIVE_LIST_BUDGET_SEC", "240"))
LIST_RETRY_DELAY_SEC = int(os.environ.get("KEEPALIVE_LIST_RETRY_DELAY_SEC", "15"))
# Parallel workers (each gets its own API client).
MAX_WORKERS = int(os.environ.get("KEEPALIVE_MAX_WORKERS", "4"))
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


def _recover_instance(client: Client, uhost_id: str, name: str, region: str, zone: str, deadline: float) -> bool:
    """Force the instance back to Stopped, so the normal cycle can restart.

    This is what clears a stuck "Initializing" / "初始化失败" instance: a Stop
    request on a stuck instance eventually resolves it to Stopped.
    """
    for attempt in range(1, 3):
        if _remaining(deadline) < MIN_ACTION_BUDGET_SEC:
            logger.error(f"[{uhost_id}] {name} no budget left for recovery")
            return False
        logger.info(f"[{uhost_id}] {name} forcing stop to recover (attempt {attempt})")
        stopped = _stop_instance(client, uhost_id, region, zone, deadline=deadline)
        if not stopped:
            logger.warning(f"[{uhost_id}] {name} stop request was rejected; waiting anyway")
        state, _ = wait_for_any_state(
            client, uhost_id, region, STOPPED_STATES,
            _inner_budget(deadline, STOP_WAIT_SEC), "Stopped (recovery)"
        )
        if state is not None:
            logger.info(f"[{uhost_id}] {name} recovered to Stopped")
            return True
    logger.error(f"[{uhost_id}] {name} recovery failed: never reached Stopped")
    return False


def ensure_running(client: Client, instance: dict, global_deadline: float | None = None) -> bool:
    """Cycle the instance: Stopped -> Running -> Stopped.

    Each round re-reads the live state and dispatches:
      * Running          -> stop, done
      * Stopped          -> start, wait for Running, stop, done
      * transitional     -> watch for a short grace period, then force-recover
      * failed/unknown   -> force-recover immediately (never wait for the default
                            600s, which is what used to blow the job timeout)
    All waits are clamped by a hard per-instance and global deadline, so an
    instance can never extend a run past its budget.
    """
    uhost_id = instance["UHostId"]
    name = instance.get("Name", "?")
    region = instance.get("Region", "")
    zone = instance.get("Zone", "")

    budget_deadline = time.time() + PER_INSTANCE_BUDGET_SEC
    deadline = min(budget_deadline, global_deadline) if global_deadline else budget_deadline

    for round_no in range(1, MAX_RECOVERY_ROUNDS + 1):
        if _remaining(deadline) < MIN_ACTION_BUDGET_SEC:
            logger.error(f"[{uhost_id}] {name} out of time budget (round {round_no})")
            return False

        inst = describe_instance(client, uhost_id, region, deadline=deadline)
        if inst is None:
            logger.warning(f"[{uhost_id}] {name} state unknown (describe failed) — recovering")
            if not _recover_instance(client, uhost_id, name, region, zone, deadline):
                return False
            continue

        state = inst.get("State", "")
        logger.info(f"[{uhost_id}] {name} round {round_no}/{MAX_RECOVERY_ROUNDS}, "
                    f"state={state}, budget left={int(_remaining(deadline))}s")

        # 1. Running -> completing the cycle only needs a stop.
        if state in RUNNING_STATES:
            logger.info(f"[{uhost_id}] {name} is Running — shutting down")
            if not _stop_instance(client, uhost_id, region, zone, deadline=deadline):
                logger.warning(f"[{uhost_id}] {name} stop request failed — will retry next round")
                continue
            settled = _settle_after_stop(client, uhost_id, name, region, deadline)
            if settled:
                return True
            continue

        # 2. Stopped -> wake it up, wait for Running, then stop again.
        if state in STOPPED_STATES:
            logger.info(f"[{uhost_id}] {name} is Stopped — waking up "
                        f"(WithoutGpuSpec={WITHOUT_GPU_SPEC}, region={region})")
            if not _start_instance(client, uhost_id, region, zone, deadline=deadline):
                logger.warning(f"[{uhost_id}] {name} start request failed — recovering")
                if not _recover_instance(client, uhost_id, name, region, zone, deadline):
                    return False
                continue
            reached, _ = wait_for_any_state(
                client, uhost_id, region, RUNNING_STATES,
                _inner_budget(deadline, STARTUP_WAIT_SEC), "Running"
            )
            if reached is None:
                logger.warning(f"[{uhost_id}] {name} did not reach Running — recovering")
                if not _recover_instance(client, uhost_id, name, region, zone, deadline):
                    return False
                continue
            logger.info(f"[{uhost_id}] {name} is Running — shutting down")
            if not _stop_instance(client, uhost_id, region, zone, deadline=deadline):
                logger.warning(f"[{uhost_id}] {name} stop request failed — will retry next round")
                continue
            settled = _settle_after_stop(client, uhost_id, name, region, deadline)
            if settled:
                return True
            continue

        # 3. Failed / abnormal state -> recover at once.
        if state in FAILED_STATES:
            logger.warning(f"[{uhost_id}] {name} is in failed state '{state}' — recovering")
            if not _recover_instance(client, uhost_id, name, region, zone, deadline):
                return False
            continue

        # 4. Transitional state -> watch briefly; force recovery if it sticks.
        if state in TRANSITIONAL_STATES:
            logger.info(f"[{uhost_id}] {name} is '{state}' — observing up to {TRANSITION_GRACE_SEC}s")
            reached, _ = wait_for_any_state(
                client, uhost_id, region, RUNNING_STATES | STOPPED_STATES,
                _inner_budget(deadline, TRANSITION_GRACE_SEC), "Running/Stopped"
            )
            if reached is None:
                logger.warning(f"[{uhost_id}] {name} stuck in '{state}' — recovering")
                if not _recover_instance(client, uhost_id, name, region, zone, deadline):
                    return False
            # Loop again so the freshly observed state is handled normally.
            continue

        # 5. Anything else -> treat as abnormal and recover.
        logger.warning(f"[{uhost_id}] {name} unexpected state '{state}' — recovering")
        if not _recover_instance(client, uhost_id, name, region, zone, deadline):
            return False
        continue

    logger.error(f"[{uhost_id}] {name} exhausted {MAX_RECOVERY_ROUNDS} rounds without cycling")
    return False


def _settle_after_stop(client: Client, uhost_id: str, name: str, region: str, deadline: float) -> str:
    """Return the state after a stop request, waiting for it to settle."""
    state, _ = wait_for_any_state(
        client, uhost_id, region, STOPPED_STATES,
        _inner_budget(deadline, STOP_WAIT_SEC), "Stopped"
    )
    if state is None:
        return ""
    logger.info(f"[{uhost_id}] {name} stopped successfully")
    return state


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
                   deadline: float | None = None) -> bool:
    params = {"Region": region, "UHostId": uhost_id}
    if zone:
        params["Zone"] = zone
    resp = _invoke(client, "StopCompShareInstance", params, deadline=deadline)
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


def process_instance(public_key: str, private_key: str, instance: dict, global_deadline: float) -> tuple[str, bool]:
    """Worker entry point: each instance gets its own client (clients are not thread-safe)."""
    uhost_id = instance.get("UHostId", "?")
    name = instance.get("Name", "?")
    client = get_client(public_key, private_key)
    try:
        ok = ensure_running(client, instance, global_deadline=global_deadline)
    except Exception as e:  # noqa: BLE001 - never let one instance kill the run
        logger.error(f"[{uhost_id}] {name} unexpected error: {e}")
        ok = False
    logger.info(f"[{uhost_id}] {name} => {'OK' if ok else 'FAILED'}")
    return uhost_id, ok


def main():
    public_key = os.environ.get("COMPSHARE_PUBLIC_KEY", "")
    private_key = os.environ.get("COMPSHARE_PRIVATE_KEY", "")
    if not public_key or not private_key:
        logger.error('Set COMPSHARE_PUBLIC_KEY and COMPSHARE_PRIVATE_KEY environment variables')
        sys.exit(1)

    only = _parse_only(sys.argv[1:])
    global_deadline = time.time() + GLOBAL_BUDGET_SEC

    client = get_client(public_key, private_key)
    logger.info(
        f"Budget: global={GLOBAL_BUDGET_SEC}s, per-instance={PER_INSTANCE_BUDGET_SEC}s, "
        f"workers={MAX_WORKERS}, transition-grace={TRANSITION_GRACE_SEC}s"
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

    workers = max(1, min(MAX_WORKERS, len(instances)))
    # Reserve room before the global deadline so the final recovery/stop action
    # still gets a realistic window instead of a zero-length one.
    reserve = POLL_INTERVAL_SEC + STOP_WAIT_SEC + API_TIMEOUT_SEC
    worker_deadline = global_deadline - reserve * (1 + 1.0 / workers)
    if worker_deadline <= time.time():
        worker_deadline = global_deadline

    for inst in instances:
        logger.info(
            f"  - {inst.get('UHostId')} | {inst.get('Name', '?')} | "
            f"state={inst.get('State', '?')} | {inst.get('Region', '?')}"
        )

    results: dict[str, bool] = {}
    pending = set()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            executor.submit(process_instance, public_key, private_key, inst, worker_deadline): inst
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
