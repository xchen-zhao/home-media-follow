# 日常维护与故障恢复

下面示例假设已安装命令行，实例为 `C:\HomeMediaFollow`，作品 ID 为虚构的 `example`。状态查询只读，不触发下载。

```powershell
home-media-follow --root C:\HomeMediaFollow status
home-media-follow --root C:\HomeMediaFollow doctor
home-media-follow --root C:\HomeMediaFollow check
```

`doctor` 的存在性检查不证明凭据有效、服务可用或整片可播放。任务退出0只说明本轮进程正常结束，还要看逐作品错误、待核验画质和下载状态。

## 提交与重试

```powershell
home-media-follow --root C:\HomeMediaFollow prepare --action activate_latest --show-id example --request-file .\request-example.json
home-media-follow --root C:\HomeMediaFollow submit --request-file .\request-example.json
```

只通过 `prepare` 和 `submit` 提交，不要把自制文件直接复制到实例 `requests` 目录。入口会持久保存命令顺序，确保暂停、恢复和取消按接收顺序执行。请求文件只创建一次。超时或结果未知时，保留同一文件，先用其中的请求 ID 查询；需要重交时仍使用该文件。不要再次生成 UUID 规避去重。

暂停、恢复、取消分别使用 `--action pause`、`resume`、`cancel`，每个新的用户动作单独创建请求文件。`resume` 保留既有订阅起点。未知作品先登记，不猜 ID；精确补回某集不属于上述动作。

单轮恢复及人工补查：

```powershell
home-media-follow --root C:\HomeMediaFollow run
home-media-follow --root C:\HomeMediaFollow run --force-scan
```

强制扫描只绕过本小时发现门控，不绕过来源身份、画质、订阅范围或删集记录。计划任务与手动执行应避免重叠；不要启动多个实例共同写同一状态根。

## 常见状态与处理

| 现象 | 处理 |
|---|---|
| 分享失效或提取码变化 | 只核对该来源，不先重置全账号会话 |
| 确认账号授权失效 | 在目标 Windows 本机重新授权；不把 Cookie、令牌发到聊天或 issue |
| 转存结果未知 | 沿原持久意图查询源和目标，确认前不盲目重转 |
| 下载失败 | 核对原 GID、暂存文件、退避时间和错误类型，不另建同集请求 |
| 已下载但未完成 | 查看归档、实际分辨率、Jellyfin路径及播放探针；不能直接修改 complete |
| 已删集又出现候选 | 检查逻辑集不补回记录，不能用新画质绕过 |
| 媒体磁盘不在线 | 恢复磁盘可达性，保留状态和不补回记录 |
| 仅有低于1080p的最新集 | 等待合格候选，不下载历史一期充数 |

## 备份与恢复

先暂停本实例后续调度并等当前一轮结束，再同时备份代码版本、配置、完整状态日志和快照、完整请求目录（包含隐藏的 `.order` 顺序回执）、发布事务及必要的恢复文件。记录摘要与时间。状态日志和快照应作为一组恢复，不能仅拷贝某张清单或删除异常尾部。

秘密存储使用 Windows DPAPI，恢复到另一台机器可能无法解密，需要在新机器重新授权。备份中仍有账号和私人路径，应单独保护，不提交仓库。

升级前保存旧包与状态备份。验证新版本与状态格式兼容后再替换代码，未完成换版或在途下载尚未核对时不能删除旧版和暂存文件。回滚仅针对本项目，不替换其他服务或钩子。

## 停止与卸载

先暂停本实例计划任务，确认当前 worker 已退出；关闭调度不会自动取消既有 aria2 下载。核对本实例全部在途 GID及发布事务后再卸载任务和软件。

卸载不默认删除本地媒体、云端内容、账号授权或状态。保留这些资料，直到明确决定如何处理；不要以整个目录递归删除作为默认卸载方式。
