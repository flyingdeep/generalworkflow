# Fix script to replace invoke with SDK method
with open('scripts/gpu_idle_monitor.py', 'rb') as f:
    content = f.read()

# The old code with invoke
old_pattern = b'resp = client.ucompshare().invoke(\r\n            "GetCompShareInstanceMonitor",\r\n            {"Region": region, "UHostIds": [uhost_id]},\r\n        )'
new_pattern = b'resp = client.ucompshare().get_comp_share_instance_monitor(\r\n            UHostIds=[uhost_id]\r\n        )'

if old_pattern in content:
    content = content.replace(old_pattern, new_pattern)
    with open('scripts/gpu_idle_monitor.py', 'wb') as f:
        f.write(content)
    print("Success: Replaced invoke with get_comp_share_instance_monitor")
else:
    print("Pattern not found. Current content around that area:")
    idx = content.find(b'GetCompShareInstanceMonitor')
    if idx >= 0:
        print(repr(content[idx-80:idx+100]))
    else:
        print("get_comp_share_instance_monitor not found in file")
        print("File size:", len(content))
