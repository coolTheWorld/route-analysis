---
status: accepted
---

# 把 Linux x64 作为第二个发布平台

Linux x86_64 与 Windows x64 并列为受支持发布平台，两者共用同一份 `route-analysis.spec` 和文件夹模式发布物，`data/` 与 `log/` 仍位于入口可执行文件同级，因此运行期路径语义、备份和回退步骤不分叉。PyInstaller 不做交叉编译，两个平台各自在本平台构建；Linux 产物动态链接构建机的 glibc，所以基线明确声明为 x86_64 加 glibc 2.43（Ubuntu 26.04），更旧的发行版不在支持范围内。Linux 开发环境接受 `>=3.12,<3.15` 区间内任一解释器，Windows 继续以 Python 3.12 为准；`scripts/build.sh` 在既有质量门禁之后先用 `QT_QPA_PLATFORM=offscreen` 跑一次打包烟雾保证无头可重复，再在存在显示会话时用真实平台插件跑第二次，用来覆盖 Qt 平台插件在目标机加载失败这一 Linux 特有故障。代价是两个平台的 Python 运行时可能不是同一版本，测试结论不能直接互相迁移；Linux 发布物无法在低于构建机 glibc 的系统上启动；构建机未安装 UPX 时 PyInstaller 跳过压缩，Linux 产物体积明显大于 Windows 版。
