# 来源与第三方许可

本项目自有代码按 [MIT](LICENSE) 许可，README 与 docs 中自有说明文字按 [CC BY 4.0](LICENSE-DOCS) 许可。第三方材料保留其许可；这两个许可不覆盖第三方程序、商标、账号内容或媒体文件。

## BaiduPCS-Go

`src/home_media_follow/baidu.py` 的网页分享验证、列举与转存协议构造参考并以 Python 重写、扩展了 [qjfoidnh/BaiduPCS-Go v4.0.2](https://github.com/qjfoidnh/BaiduPCS-Go/tree/v4.0.2) 的下列实现：

- [baidupcs/transfer.go](https://github.com/qjfoidnh/BaiduPCS-Go/blob/v4.0.2/baidupcs/transfer.go)
- [baidupcs/prepare.go](https://github.com/qjfoidnh/BaiduPCS-Go/blob/v4.0.2/baidupcs/prepare.go)
- [internal/pcscommand/transfer.go](https://github.com/qjfoidnh/BaiduPCS-Go/blob/v4.0.2/internal/pcscommand/transfer.go)

上游许可为 Apache License 2.0，声明 `Copyright iikira.`。完整许可随本项目保存在 [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt)。本项目保留上游权利与署名，不将被参考的上游实现重新标为 MIT。该 Python 文件增加了受限网络请求、秘密隔离、持久转存意图、结果未知时对账、路径约束与分页验证；不包含上游 Go 程序、依赖包或可执行文件。发布和再分发时应一并保留本说明、该文件中的来源及修改说明，以及 Apache 2.0 许可文本。

上游 v4.0.2 根目录的 `NOTICE` 路径未找到；上游 LICENSE 附录中的版权声明已在此保留。许可核对依据为该版本的实际 LICENSE 和上述源码，而非仓库名称或语言差异。

## 独立安装的服务与工具

本项目通过接口调用下列程序，发行包不包含其二进制。安装者应从官方渠道取得程序并遵守对应版本的许可：

| 程序 | 用途 | 官方入口 |
|---|---|---|
| Python | 运行后台与命令行 | https://www.python.org/ |
| OpenList | 访问本人网盘文件与提供本机下载入口 | https://github.com/OpenListTeam/OpenList |
| aria2 | 下载与断点恢复 | https://github.com/aria2/aria2 |
| Jellyfin | 媒体索引与播放验证 | https://jellyfin.org/ |
| FFmpeg / ffprobe | 生成测试视频、探测媒体参数 | https://ffmpeg.org/ |

具体版本、构建选项与所附许可属于对应发行物的一部分。下载 Windows 程序不构成将其随本项目重新分发的授权。本项目不隶属于上述项目或百度网盘。

## 复用文档

复用本项目文档时，请注明“Home Media Follow contributors”、原文链接、CC BY 4.0 许可链接，并说明所做修改。首次公开发布前尚无公共仓库地址，分发本地副本时可附原文件和版本摘要，发布后使用实际仓库地址；不得编造尚不存在的链接。
