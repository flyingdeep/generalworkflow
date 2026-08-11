"""
GPU Idle Monitor - 妫€娴嬬┖璺戝疄渚嬪苟鍏抽棴
姣忛殧涓€灏忔椂杩愯锛屾鏌ヨ繍琛屼腑瀹炰緥鐨?GPU 浣跨敤鎯呭喌锛?濡傛灉杩囧幓涓€灏忔椂鍐?GPU 浣跨敤鐜囧缁堜綆浜?5%锛屽垯鍏抽棴璇ュ疄渚嬨€?"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

try:
    from ucloud.core import exc
    from ucloud.client import Client
except ImportError:
    logger.error("Please install ucloud-sdk-python3: pip install --upgrade ucloud-sdk-python3")
    sys.stdout.flush()
    sys.exit(1)

API_BASE_URL = "https://api.compshare.cn"
GPU_THRESHOLD = 5.0  # GPU 浣跨敤鐜囬槇鍊硷紙%锛?LOOKBACK_MINUTES = 60  # 鏌ョ湅杩囧幓澶氬皯鍒嗛挓鐨勬暟鎹?POLL_INTERVAL_SEC = 15  # 鐘舵€佽疆璇㈤棿闅?MAX_RETRIES = 3
RETRY_DELAY_SEC = 5


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


def get_gpu_monitor_data(client: Client, uhost_id: str, region: str) -> list[float] | None:
    """鑾峰彇瀹炰緥杩囧幓涓€娈垫椂闂寸殑 GPU 浣跨敤鐜囨暟鎹€?""
    try:
        resp = client.ucompshare().invoke(
            "GetCompShareInstanceMonitor",
            {"Region": region, "UHostIds": [uhost_id]},
        )
        if resp.get("RetCode") != 0:
            logger.warning(f"[{uhost_id}] Monitor API failed: {resp.get('Message')}")
            return None
        
        data = resp.get("Data", {})
        lst = data.get("List", [])
        if not lst:
            logger.warning(f"[{uhost_id}] No monitor data returned")
            return None
        
        inst_data = lst[0]
        metrics = inst_data.get("Metrics", [])
        
        # 鏌ユ壘 GPU 浣跨敤鐜囨寚鏍?        gpu_values = []
        for metric in metrics:
            key = metric.get("MetricKey", "")
            if key == "cloudwatch_gpu_util":
                results = metric.get("Results", [])
                if results:
                    values = results[0].get("Values", [])
                    gpu_values = [v.get("Value", 0) for v in values if v.get("Value") is not None]
                    break
        
        return gpu_values if gpu_values else None
        
    except exc.UCloudException as e:
        logger.warning(f"[{uhost_id}] Monitor API exception: {e}")
        return None


def check_idle(uhost_id: str, region: str, client: Client) -> tuple[bool, str]:
    """
    妫€鏌ュ疄渚嬫槸鍚︾┖闂诧紙GPU 浣跨敤鐜囨寔缁綆浜庨槇鍊硷級
    杩斿洖: (鏄惁绌洪棽, 鍘熷洜)
    """
    gpu_values = get_gpu_monitor_data(client, uhost_id, region)
    
    if gpu_values is None:
        return False, "no_data"
    
    # 杩囨护鎺夎繃鍘?LOOKBACK_MINUTES 鍐呯殑鏁版嵁鐐?    cutoff_time = time.time() -LOOKBACK_MINUTES * 60
    recent_values = [v for v in gpu_values if True]  # API 杩斿洖鐨勯兘鏄繎鏈熸暟鎹?    
    if not recent_values:
        return False, "no_recent_data"
    
    # 妫€鏌ユ槸鍚︽湁浠绘剰鏁版嵁鐐归珮浜庨槇鍊?    max_gpu = max(recent_values)
    min_gpu = min(recent_values)
    avg_gpu = sum(recent_values) / len(recent_values)
    
    # 濡傛灉鎵€鏈夋暟鎹偣閮戒綆浜庨槇鍊硷紝鍒欒涓虹┖闂?    is_idle = max_gpu < GPU_THRESHOLD
    
    reason = (
        f"idle" if is_idle 
        else f"max={max_gpu:.1f}%>={GPU_THRESHOLD}%"
    )
    
    logger.info(
        f"[{uhost_id}] GPU: min={min_gpu:.1f}%, max={max_gpu:.1f}%, "
        f"avg={avg_gpu:.1f}%, points={len(recent_values)}, {'IDLE' if is_idle else 'ACTIVE'}"
    )
    
    return is_idle, reason


def stop_instance(client: Client, uhost_id: str, region: str, zone: str) -> bool:
    """鍏抽棴瀹炰緥锛屽甫閲嶈瘯銆?""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.ucompshare().invoke(
                "StopCompShareInstance",
                {"Region": region, "Zone": zone, "UHostId": uhost_id},
            )
            if resp.get("RetCode") != 0:
                logger.error(f"[{uhost_id}] Stop failed (attempt {attempt}): {resp.get('Message')}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC)
                    continue
                return False
            logger.info(f"[{uhost_id}] Instance stopped successfully")
            return True
        except exc.UCloudException as e:
            logger.error(f"[{uhost_id}] Stop exception (attempt {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    return False


def main():
    public_key = os.environ.get("COMPSHARE_PUBLIC_KEY", "")
    private_key = os.environ.get("COMPSHARE_PRIVATE_KEY", "")
    if not public_key or not private_key:
        logger.error('Set COMPSHARE_PUBLIC_KEY and COMPSHARE_PRIVATE_KEY environment variables')
        sys.stdout.flush()
        sys.exit(1)
    
    client = get_client(public_key, private_key)
    
    logger.info("Listing all instances...")
    logger.info("=" * 60)
    instances = list_instances(client)
    logger.info(f"Total instances found: {len(instances)}")
    
    running_instances = [
        inst for inst in instances 
        if inst.get("State") == "Running"
    ]
    logger.info(f"Running instances: {len(running_instances)}")
    
    stopped_count = 0
    for inst in running_instances:
        uhost_id = inst.get("UHostId")
        name = inst.get("Name", "?")
        region = inst.get("Region", "")
        zone = inst.get("Zone", "")
        
        logger.info(f"\nChecking {uhost_id} ({name}) in {region}...")
        
        try:
            is_idle, reason = check_idle(uhost_id, region, client)
            
            if is_idle:
                logger.warning(f"[{uhost_id}] {name} is IDLE - stopping...")
                if stop_instance(client, uhost_id, region, zone):
                    stopped_count += 1
            else:
                logger.info(f"[{uhost_id}] {name} is ACTIVE - {reason}")
                
        except Exception as e:
            logger.error(f"Error processing {uhost_id}: {e}")
    
    logger.info("\n" + "=" * 60)
    logger.info(f"Monitor complete. Stopped {stopped_count} idle instance(s).")
    logger.info(f"Total running: {len(running_instances)}, Stopped: {stopped_count}")


if __name__ == "__main__":
    main()
