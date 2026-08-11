with open('scripts/gpu_idle_monitor.py', 'rb') as f:
    content = f.read()

# 替换 invoke 调用为 SDK 方法调用（不带 Region）
old_code = b'''resp = client.ucompshare().invoke(
            "GetCompShareInstanceMonitor",
            {"Region": region, "UHostIds": [uhost_id]},
        )'''
new_code = b'''resp = client.ucompshare().get_comp_share_instance_monitor(
            UHostIds=[uhost_id]
        )'''

if old_code in content:
    content = content.replace(old_code, new_code)
    with open('scripts/gpu_idle_monitor.py', 'wb') as f:
        f.write(content)
    print('File updated successfully')
else:
    print('Old code not found')
    # 尝试查找
    idx = content.find(b'GetCompShareInstanceMonitor')
    print(f'Found GetCompShareInstanceMonitor at {idx}')
    if idx >= 0:
        print('Context:', content[idx-50:idx+150])
