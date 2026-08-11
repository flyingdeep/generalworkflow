"""
compshare keepalive script
Wakes up stopped instances in without-gpu mode to prevent them from being reclaimed.
"""

import os
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

try:
    from ucloud.core import exc
    from ucloud.client import Client
except ImportError:
    logger.error("Please install ucloud-sdk-python3: pip install --upgrade ucloud-sdk-python3")
    sys.exit(1)


API_BASE_URL = "https://api.compshare.cn"
POLL_INTERVAL_SEC = 15
STARTUP_WAIT_SEC = 180  # maximum expected startup time
MAX_RETRIES = 3
RETRY_DELAY_SEC = 5

# Without-gpu spec: A = 2核4G, B = 8核16G
WITHOUT_GPU_SPEC = "A"


def get_client(public_key: str, private_key: str) -> Client:
    return Client({
        "region": "",
        "public_key": public_key,
        "private_key": private_key,
        "base_url": API_BASE_URL,
    })


def list_instances(client: Client, limit: int = 100) -> list[dict]:
    """Fetch all instances with pagination support."""
    all_instances = []
    offset = 0
    while True:
        resp = client.ucompshare().invoke(
            "DescribeCompShareInstance",
            {"Limit": limit, "Offset": offset},
        )
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


def ensure_running(client: Client, instance: dict):
    """Ensure an instance is in Running state (wake if stopped, then shut down)."""
    uhost_id = instance["UHostId"]
    name = instance.get("Name", "?")
    state = instance.get("State", "?")
    region = instance.get("Region", "")
    zone = instance.get("Zone", "")

    if state == "Running":
        logger.info(f"[{uhost_id}] {name} already Running — skipping")
        return True

    if state in ("Stopped", "stopped"):
        logger.info(f"{uhost_id}] {name} is Stopped — waking up (WithoutGpuSpec={WITHOUT_GPU_SPEC}, Region={region}, Zone={zone})")
        if not _start_instance(client, uhost_id, region, zone):
            return False
        logger.info(f"[{uhost_id}] {name} starting, waiting up to {STARTUP_WAIT_SEC}s for Running...")
        if not _wait_for_state(client, uhost_id, region, timeout_sec=STARTUP_WAIT_SEC):
            logger.warning(f"[{uhost_id}] {name} did not reach Running within timeout")
            return False
        logger.info(f"[{uhost_id}] {name} is now Running — shutting down")
        if not _stop_instance(client, uhost_id, region, zone):
            return False
        logger.info(f"[{uhost_id}] {name} stopped successfully")
        return True

    logger.warning(f"[{uhost_id}] {name} unexpected state: {state} — skipping")
    return False


def _start_instance(client: Client, uhost_id: str, region: str, zone: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            params = {"Region": region, "UHostId": uhost_id, "WithoutGpuSpec": WITHOUT_GPU_SPEC}
            if zone:
                params["Zone"] = zone
            resp = client.ucompshare().invoke("StartCompShareInstance", params)
            if resp.get("RetCode") != 0:
                logger.error(f"[{uhost_id}] Start failed (attempt {attempt}): {resp.get('Message')}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                return False
            logger.info(f"[{uhost_id}] StartCompShareInstance OK")
            return True
        except exc.UCloudException as e:
            logger.error(f"[{uhost_id}] StartCompShareInstance exception (attempt {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    return False


def _stop_instance(client: Client, uhost_id: str, region: str, zone: str) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            params = {"Region": region, "UHostId": uhost_id}
            if zone:
                params["Zone"] = zone
            resp = client.ucompshare().invoke("StopCompShareInstance", params)
            if resp.get("RetCode") != 0:
                logger.error(f"[{uhost_id}] Stop failed (attempt {attempt}): {resp.get('Message')}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                return False
            logger.info(f"[{uhost_id}] StopCompShareInstance OK")
            return True
        except exc.UCloudException as e:
            logger.error(f"[{uhost_id}] StopCompShareInstance exception (attempt {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    return False


def _wait_for_state(client: Client, uhost_id: str, target_state: str, region: str, timeout_sec: int = 180) -> bool:
    """Poll DescribeCompShareInstance until the instance reaches target_state or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            resp = client.ucompshare().invoke(
                "DescribeCompShareInstance",
                {"Region": region, "UHostIds": [uhost_id]},
            )
            if resp.get("RetCode") != 0:
                logger.warning(f"[{uhost_id}] DescribeCompShareInstance error: {resp.get('Message')}")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            instances = resp.get("UHostSet", [])
            if not instances:
                logger.warning(f"[{uhost_id}] Instance not found in response")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            state = instances[0].get("State", "")
            if state == target_state:
                return True
            logger.info(f"[{uhost_id}] Current state={state}, still waiting for {target_state}")
        except exc.UCloudException as e:
            logger.warning(f"[{uhost_id}] DescribeCompShareInstance exception: {e}")
        time.sleep(POLL_INTERVAL_SEC)
    return False


def main():
    public_key = os.environ.get("COMPSHARE_PUBLIC_KEY", "")
    private_key = os.environ.get("COMPSHARE_PRIVATE_KEY", "")
    if not public_key or not private_key:
        logger.error('Set COMPSHARE_PUBLIC_KEY and COMPSHARE_PRIVATE_KEY environment variables')
        sys.exit(1)

    client = get_client(public_key, private_key)

    logger.info("Listing all instances...")
    instances = list_instances(client)
    logger.info(f"Total instances found: {len(instances)}")

    success_count = 0
    for inst in instances:
        try:
            if ensure_running(client, inst):
                success_count += 1
        except Exception as e:
            logger.error(f"Error processing {inst.get('UHostId')}: {e}")

    logger.info(f"Done. Processed {success_count}/{len(instances)} instances successfully.")


if __name__ == "__main__":
    main()
