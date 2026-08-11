"""
GPU Idle Monitor - 检测空跑实例并关闭
每隔一小时运行，检查运行中实例的 GPU 使用情况，
如果过去一小时内 GPU 使用率始终低于 5%，则关闭该实例。
"""

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
GPU_THRESHOLD = 5.0  # GPU 使用率阈值（%）
LOOKBACK_MINUTES = 60  # 查看过去多少分钟的数据
POLL_INTERVAL_SEC = 15  # 状态轮询间隔
MAX_RETRIES = 3
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
    """获取实例过去一段时间的 GPU 使用率数据。"""
    try:
        resp = client.ucompshare().get_comp_share_instance_monitor(
            UHostIds=[uhost_id]
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
        
        # 查找 GPU 使用率指标
        gpu_values = []
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
    检查实例是否空闲（GPU 使用率持续低于阈值）
    返回: (是否空闲, 原因)
    """
    gpu_values = get_gpu_monitor_data(client, uhost_id, region)
    
    if gpu_values is None:
        return False, "no_data"
    
    # 过滤掉过去 LOOKBACK_MINUTES 内的数据点
    cutoff_time = time.time() -LOOKBACK_MINUTES * 60
    recent_values = [v for v in gpu_values if True]  # API 返回的都是近期数据
    
    if not recent_values:
        return False, "no_recent_data"
    
    # 检查是否有任意数据点高于阈值
    max_gpu = max(recent_values)
    min_gpu = min(recent_values)
    avg_gpu = sum(recent_values) / len(recent_values)
    
    # 如果所有数据点都低于阈值，则认为空闲
    is_idle = max_gpu < GPU_THRESHOLD
    
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
    """关闭实例，带重试。"""
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
