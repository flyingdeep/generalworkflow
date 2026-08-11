"""
GPU Idle Monitor - 濡偓濞村鈹栫捄鎴濈杽娓氬鑻熼崗鎶芥４
濮ｅ繘娈ф稉鈧亸蹇旀鏉╂劘顢戦敍灞绢梾閺屻儴绻嶇悰灞艰厬鐎圭偘绶ラ惃?GPU 娴ｈ法鏁ら幆鍛枌閿?婵″倹鐏夋潻鍥у箵娑撯偓鐏忓繑妞傞崘?GPU 娴ｈ法鏁ら悳鍥ь潗缂佸牅缍嗘禍?5%閿涘苯鍨崗鎶芥４鐠囥儱鐤勬笟瀣ㄢ偓?"""

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
GPU_THRESHOLD = 5.0  # GPU 娴ｈ法鏁ら悳鍥閸婄》绱?閿?LOOKBACK_MINUTES = 60  # 閺屻儳婀呮潻鍥у箵婢舵艾鐨崚鍡涙寭閻ㄥ嫭鏆熼幑?POLL_INTERVAL_SEC = 15  # 閻樿埖鈧浇鐤嗙拠銏ゆ？闂?MAX_RETRIES = 3
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
    """閼惧嘲褰囩€圭偘绶ユ潻鍥у箵娑撯偓濞堝灚妞傞梻瀵告畱 GPU 娴ｈ法鏁ら悳鍥ㄦ殶閹诡喓鈧?""
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
        
        # 閺屻儲澹?GPU 娴ｈ法鏁ら悳鍥ㄥ瘹閺?        gpu_values = []
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
    濡偓閺屻儱鐤勬笟瀣Ц閸氾妇鈹栭梻璇х礄GPU 娴ｈ法鏁ら悳鍥ㄥ瘮缂侇厺缍嗘禍搴ㄦ閸婄》绱?    鏉╂柨娲? (閺勵垰鎯佺粚娲＝, 閸樼喎娲?
    """
    gpu_values = get_gpu_monitor_data(client, uhost_id, region)
    
    if gpu_values is None:
        return False, "no_data"
    
    # 鏉╁洦鎶ら幒澶庣箖閸?LOOKBACK_MINUTES 閸愬懐娈戦弫鐗堝祦閻?    cutoff_time = time.time() -LOOKBACK_MINUTES * 60
    recent_values = [v for v in gpu_values if True]  # API 鏉╂柨娲栭惃鍕厴閺勵垵绻庨張鐔告殶閹?    
    if not recent_values:
        return False, "no_recent_data"
    
    # 濡偓閺屻儲妲搁崥锔芥箒娴犵粯鍓伴弫鐗堝祦閻愬綊鐝禍搴ㄦ閸?    max_gpu = max(recent_values)
    min_gpu = min(recent_values)
    avg_gpu = sum(recent_values) / len(recent_values)
    
    # 婵″倹鐏夐幍鈧張澶嬫殶閹诡喚鍋ｉ柈鎴掔秵娴滃酣妲囬崐纭风礉閸掓瑨顓绘稉铏光敄闂?    is_idle = max_gpu < GPU_THRESHOLD
    
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
    """閸忔娊妫寸€圭偘绶ラ敍灞界敨闁插秷鐦妴?""
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
