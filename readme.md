# CompShare Keepalive Workflow

每天 4:00 AM (北京时间) 自动运行，对 CompShare 上停止状态的实例执行"无卡模式唤醒 → 等待启动完成 → 再次关闭"的操作，防止实例因长期关机被回收。

## 使用方式

### GitHub 上设置 Secret

1. 进入仓库 → **Settings** → **Secrets and variables** → **Actions**
2. 添加两个 Secret：
   - `COMPSHARE_PUBLIC_KEY` — 填入你的 Public Key
   - `COMPSHARE_PRIVATE_KEY` — 填入你的 Private Key

### 手动触发

在 **Actions** 标签页，点击 `compshare-keepalive` → **Run workflow**。

### 本地测试

```bash
export COMPSHARE_PUBLIC_KEY="你的Public Key"
export COMPSHARE_PRIVATE_KEY="你的Private Key"
pip install -r requirements.txt
python scripts/keepalive.py
```

## 工作流程

1. 调用 `DescribeCompShareInstance` 拉取全部实例列表
2. 筛选 `State == Stopped` 的实例
3. 调用 `StartCompShareInstance` 以无卡模式（`WithoutGpuSpec=A` = 2核4G）启动
4. 轮询等待实例状态变为 `Running`（最多 3 分钟）
5. 确认 Running 后，调用 `StopCompShareInstance` 关闭实例

## 注意事项

- API 基础地址：`https://api.compshare.cn`
- 默认 Region：`cn-wlcb`，Zone：`cn-wlcb-01`（可在脚本中修改）
- 无卡模式规格档位：`A` = 2 核 4G，`B` = 8 核 16G（脚本中为 `WITHOUT_GPU_SPEC`）
- 同一实例并发操作有 10 分钟 TTL 锁，脚本已内置重试逻辑
- **密钥通过 GitHub Secrets 配置，不要硬编码到代码或 README 中**
