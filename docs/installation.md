# 安装与首次运行

建议先用短测试视频完成独立实例验收，再登记实际来源。安装过程不接管已有家庭影音服务。

## 准备程序

从官方渠道取得 Python 3.13、OpenList、aria2、Jellyfin 及 FFmpeg/ffprobe，程序和版本记录见[第三方说明](../THIRD_PARTY_NOTICES.md)。自行完成 OpenList 网盘授权，将本人的网盘挂载到例如 `/Baidu`；确认服务运行且目标账户可访问。

为本项目准备独立目录。示例使用 `C:\HomeMediaFollow`，其中只放本实例配置、状态和测试媒体。供任务使用的 Python 3.13 基础安装应选择所有用户可读的位置；任务虚拟环境放在实例根目录内。不要使用仅当前用户可访问的商店版或用户资料目录中的解释器。Jellyfin媒体库只读取本实例的分类子目录。默认布局为：

| 内容 | Jellyfin 库目录 | 内容类型 |
|---|---|---|
| 动漫 | `C:\HomeMediaFollow\media\动漫` | 电视剧 |
| 电视剧 | `C:\HomeMediaFollow\media\电视剧` | 电视剧 |
| 综艺 | `C:\HomeMediaFollow\media\综艺` | 电视剧 |

可以分别建三个库，也可以在同一“电视剧”库中添加这些分类目录。目录还不存在时，先在实例内创建对应空目录。不要把分类父目录 `C:\HomeMediaFollow\media` 直接作为剧集库根，否则 Jellyfin 可能把分类名识别为系列。自定义 `media_root` 时使用对应的三个子目录；不要把测试库指向已有家庭媒体。

已有同类服务时，测试实例使用不同端口；不要停止原服务以腾出端口。项目控制客户端只连接回环地址，外部服务的管理与网络访问权限由其自身配置决定。

## 安装软件包并初始化

在源代码根目录打开 PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m home_media_follow --root C:\HomeMediaFollow init
.\.venv\Scripts\python.exe -m home_media_follow --root C:\HomeMediaFollow check
```

从发行 wheel 安装时，将 `pip install .` 替换为该 wheel 的实际路径。后续命令中的 `home-media-follow` 是当前虚拟环境 Scripts 中的入口；可始终使用同一环境的 `python.exe -m home_media_follow`，避免调用系统中另一版本。

初始化必须使用空目录。它创建目录、实例标记及空作品配置，不启动任何订阅，也不修改第三方服务。

编辑 `C:\HomeMediaFollow\config\shows.json`，核对媒体、暂存、恢复目录、ffprobe、网盘根目录和三个本机服务地址，然后重新运行 `check`。完整字段含义见[配置说明](configuration.md)。

## aria2 的必要持久化配置

后台提交下载后会调用 aria2 的 `saveSession`；只有 RPC 能访问还不够。使用[配置示例](../examples/aria2.example.conf)，在本机另存为 UTF-8 的私有配置，并至少设置：

```ini
enable-rpc=true
rpc-listen-all=false
rpc-listen-port=6800
rpc-secret=REPLACE_LOCALLY_WITH_A_NEW_RPC_SECRET
input-file=C:/HomeMediaFollow/aria2/session.txt
save-session=C:/HomeMediaFollow/aria2/session.txt
save-session-interval=30
```

先创建实例专用 `aria2` 目录，并在首次启动前创建空的 `session.txt`：

```powershell
New-Item -ItemType Directory -Path C:\HomeMediaFollow\aria2 -Force
if (-not (Test-Path C:\HomeMediaFollow\aria2\session.txt)) {
    New-Item -ItemType File -Path C:\HomeMediaFollow\aria2\session.txt
}
```

以后启动不可清空已有会话文件。`input-file` 与 `save-session` 必须指向同一实例拥有的文件；aria2实际运行账户需要该目录和文件的读写权限。下载、会话和配置目录都不能借用其他家庭实例。

在本机将占位秘密替换成新生成的 RPC secret，配置文件仅允许实际 aria2 运行账户、管理员和系统读取；在后面的 `authorize aria2` 隐藏提示中输入同一秘密。不要用示例占位值作为实际秘密，不要提交填写后的配置。端口可更换，但须与 `shows.json` 中 `services.aria2` 一致，并确认未占用。

使用该私有配置启动 aria2，例如 `aria2c.exe --conf-path=C:/HomeMediaFollow/aria2/aria2.conf`（路径按实际文件位置替换），随后验证重启会重新读取会话。缺少 `save-session` 时，RPC提交可能已成功，而后续保存会话报错；此时先按原 GID 对账，不能再创建一个下载请求。

本后台不要求配置全局 `on-download-complete` 钩子。不要复用或改写既有家庭服务的完成钩子；恢复轮次会核对本实例 GID并执行校验、归档和Jellyfin确认。

## 本机授权

在目标 Windows 的交互 PowerShell 中执行；首次及后续授权都不要通过远程非交互 stdin 传秘密。计划任务安装后，秘密目录限制给管理员与服务账户，应以提升权限的本机终端维护授权。

```powershell
home-media-follow --root C:\HomeMediaFollow authorize openlist
home-media-follow --root C:\HomeMediaFollow authorize aria2
home-media-follow --root C:\HomeMediaFollow authorize jellyfin
home-media-follow --root C:\HomeMediaFollow authorize baidu
```

OpenList账号、aria2 RPC秘密和Jellyfin账号通过本机提示输入。Jellyfin授权会取得专用 API key。`authorize baidu` 在目标 Windows 打开专用 Edge 登录环境，展示官方登录二维码，由用户在本机完成登录；登录结果写入本实例 DPAPI，不回显会话。专用 profile 和二维码保存在受保护的实例 `login` 目录，不附着或关闭用户已有浏览器。配置与操作结果不会返回 Cookie。

登录 profile 会保留到实际服务账户能力验证完成，不能把“已保存会话”当作授权长期有效。高级入口 `authorize baidu-cookie` 仅为已有专用本机会话的隐藏导入方式；正常首次安装优先使用二维码。不要从其他电脑导出会话，也不要把会话放进配置、终端参数、issue或聊天。网页会话授权与 OpenList refresh token 是两个独立环节。

授权保存成功只代表本机已写入秘密，必须以真实分享读取、服务接口和测试媒体播放继续核验有效性。

## 登记来源与首次订阅

先在本人的网盘专用测试目录放置自有生成视频，建立用于验证的分享。分享顶层需要包含作品文件夹。在本机检查分享并保存来源提案：

```powershell
home-media-follow --root C:\HomeMediaFollow inspect-source --show-id example --output C:\HomeMediaFollow\config\example-source.json
```

该命令隐藏输入分享引用和提取码，实际读取分享顶层目录，将候选目录的稳定ID、路径及秘密引用写入指定本机文件，并保存提取码到 DPAPI。它不会登记或激活订阅，不覆盖已有提案。

打开该提案，选择与预期作品一致的 `bindings` 项，将其中的 `source` 对象复制到 `shows.json` 中该作品的 `source`。作品条目还需要下列字段（这是结构片段，`source` 必须使用刚核验的完整对象）：

```json
{
  "show_id": "example",
  "title": "Example",
  "media_kind": "tv",
  "season": 1,
  "episode_mode": "absolute",
  "cloud_path": "/HomeMediaFollow/Example",
  "source": {}
}
```

分享含多季时明确限制本季目录；不可只复制第一个候选目录。来源提案包含私人分享身份，留在实例目录，不提交仓库。

`inspect-source` 已保存提取码；后续只更新提取码时使用下列命令，配置仍只保存秘密引用：

```powershell
home-media-follow --root C:\HomeMediaFollow authorize source --show-id example
```

完成登记并通过 `check` 后，用新的请求文件激活该作品：

```powershell
home-media-follow --root C:\HomeMediaFollow prepare --action activate_latest --show-id example --request-file .\activate-example.json
home-media-follow --root C:\HomeMediaFollow submit --request-file .\activate-example.json
home-media-follow --root C:\HomeMediaFollow run
home-media-follow --root C:\HomeMediaFollow status --show-id example
```

不要手工写入状态来制造激活成功。首次扫描应看到全部候选后再确定最新逻辑集，综艺还要核对上下和来源季数。

## 安装后台任务

在提升权限的 PowerShell 中，使用本项目脚本和实际运行环境绝对路径。从已初始化实例建立专门运行环境，保持当前目录为源码根目录：

```powershell
py -3.13 -m venv C:\HomeMediaFollow\.venv
C:\HomeMediaFollow\.venv\Scripts\python.exe -m pip install .
```

确保这里的基础解释器是所有用户可读的安装。随后安装任务：

```powershell
.\tools\windows-task.ps1 -Action install -Root C:\HomeMediaFollow -Python C:\HomeMediaFollow\.venv\Scripts\python.exe -TaskName HomeMediaFollow-Worker -DryRun
.\tools\windows-task.ps1 -Action install -Root C:\HomeMediaFollow -Python C:\HomeMediaFollow\.venv\Scripts\python.exe -TaskName HomeMediaFollow-Worker
.\tools\windows-task.ps1 -Action status -Root C:\HomeMediaFollow -Python C:\HomeMediaFollow\.venv\Scripts\python.exe -TaskName HomeMediaFollow-Worker
```

脚本拒绝覆盖同名任务，并要求自动安装管理的媒体、暂存、恢复和发布锁路径都位于实例根目录内；外部目录布局需自行审查权限并手动部署。任务使用 `LOCAL SERVICE` 最低运行权限，安装需管理员配置目录ACL和任务定义。脚本不会改已有 aria2 完成钩子；后台恢复轮次负责继续处理自己的下载。

触发器为每小时整点、每15分钟恢复以及开机后2分钟补跑，采用 `IgnoreNew` 防止任务重入。电脑关机期间无法扫描或通知；恢复后补查当前来源，不逐小时重放所有错过的时隙。

暂停、恢复与卸载将 `-Action` 分别替换为 `pause`、`resume`、`uninstall`；脚本核验任务描述及执行命令属于该实例。卸载任务不删除媒体、网盘内容或状态。

## 验收后再使用

核对实际服务账户能够加载包、读取配置与秘密，并完成一次真实短视频入库及Jellyfin路径/播放探针。仅手动运行成功、任务退出0或接口HTTP 200都不足以证明完整链路通过。按[测试说明](testing.md)保留版本、结果与限制，再登记实际有权处理的来源。
