"""Verify GetCompShareInstanceMonitor API capabilities."""
import os
import sys
import time

sys.path.insert(0, "e:/Work/generalworkflow")
from ucloud.core import exc
from ucloud.client import Client

PUBLIC_KEY = os.environ.get("COMPSHARE_PUBLIC_KEY", "")
PRIVATE_KEY = os.environ.get("COMPSHARE_PRIVATE_KEY", "")

if not PUBLIC_KEY or not PRIVATE_KEY:
    print("ERROR: Set COMPSHARE_PUBLIC_KEY and COMPSHARE_PRIVATE_KEY")
    sys.exit(1)

client = Client({
    "region": "",
    "public_key": PUBLIC_KEY,
    "private_key": PRIVATE_KEY,
    "base_url": "https://api.compshare.cn",
})

# Step 1: List all instances
print("=" * 60)
print("Step 1: List all instances")
print("=" * 60)
try:
    resp = client.ucompshare().invoke("DescribeCompShareInstance", {"Limit": 100})
    if resp.get("RetCode") != 0:
        print(f"ERROR: {resp.get('Message')}")
        sys.exit(1)
    
    instances = resp.get("UHostSet", [])
    print(f"Total instances: {len(instances)}")
    for inst in instances:
        state = inst.get("State", "?")
        region = inst.get("Region", "?")
        zone = inst.get("Zone", "?")
        name = inst.get("Name", "?")
        uhost_id = inst.get("UHostId", "?")
        # Check MonitorMessages from Describe response
        monitor = inst.get("MonitorMessages", {})
        gpu_info = monitor.get("GpuInfo")
        print(f"  {uhost_id} | {name} | {state} | {region}/{zone} | GpuInfo={gpu_info}")
    
    # Separate instances by region
    regions = {}
    for inst in instances:
        r = inst.get("Region", "")
        if r not in regions:
            regions[r] = []
        regions[r].append(inst)
    
    print(f"\nRegions found: {list(regions.keys())}")
    
    # Step 2: Test GetCompShareInstanceMonitor for each region
    print("\n" + "=" * 60)
    print("Step 2: Test GetCompShareInstanceMonitor")
    print("=" * 60)
    
    for region, region_instances in regions.items():
        print(f"\n--- Region: {region} ---")
        for inst in region_instances:
            uhost_id = inst.get("UHostId")
            state = inst.get("State")
            name = inst.get("Name")
            
            if state != "Running":
                print(f"  Skipping {uhost_id} ({name}): {state}")
                continue
            
            try:
                # Try calling with Region only (no time range params)
                monitor_resp = client.ucompshare().invoke(
                    "GetCompShareInstanceMonitor",
                    {"Region": region, "UHostIds": [uhost_id]}
                )
                if monitor_resp.get("RetCode") != 0:
                    print(f"  ERROR for {uhost_id}: {monitor_resp.get('Message')}")
                    continue
                
                data = monitor_resp.get("Data", {})
                lst = data.get("List", [])
                if not lst:
                    print(f"  No data for {uhost_id}")
                    continue
                
                inst_data = lst[0]
                metrics = inst_data.get("Metrics", [])
                print(f"  {uhost_id} ({name}):")
                for metric in metrics:
                    key = metric.get("MetricKey")
                    results = metric.get("Results", [])
                    values = results[0].get("Values", []) if results else []
                    if values:
                        # Show first and last few data points
                        ts_first = values[0].get("Timestamp", "?")
                        val_first = values[0].get("Value", "?")
                        ts_last = values[-1].get("Timestamp", "?")
                        val_last = values[-1].get("Value", "?")
                        count = len(values)
                        print(f"    {key}: {count} points, first={val_first}@{ts_first}, last={val_last}@{ts_last}")
                time.sleep(1)  # Rate limiting
                
            except exc.UCloudException as e:
                print(f"  Exception for {uhost_id}: {e}")
    
    print("\n" + "=" * 60)
    print("Verification complete!")
    print("=" * 60)
    
except exc.UCloudException as e:
    print(f"ERROR: {e}")
    sys.exit(1)
