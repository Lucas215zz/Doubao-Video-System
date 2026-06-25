# 豆包视频工作台

本项目是一个本地半自动视频生成工作台，用来集中管理账号标签、视频任务、智能参考图、本地下载监听和视频归档。

它不会自动登录、自动提交网页任务或绕过平台限制。工作台负责把重复的整理工作集中起来：建任务、复制提示词、打开对应浏览器环境、监听下载目录，并把下载完成的视频归档回任务。

## 启动

```powershell
npm.cmd install
npm.cmd run build
npm.cmd start
```

打开：

```text
http://localhost:5174
```

开发模式：

```powershell
npm.cmd run dev
```

## 工作流

1. 在“添加账号”里创建账号标签。
2. 可选填写浏览器路径和 Profile 目录，用来打开不同账号的浏览器环境。
3. 在“新建视频任务”里填写标题、提示词、目标账号和智能参考图。
4. 在任务队列里复制提示词、打开对应账号浏览器。
5. 你在豆包网页里手动提交并下载视频。
6. 工作台监听下载目录，发现视频后显示到“下载收件箱”。
7. 选择对应任务并点击“归档”，视频会移动到 `outputs/YYYY-MM-DD/账号名/`。

## 数据目录

- `data/store.json`：本地数据文件。
- `uploads/`：智能参考图上传目录。
- `watched-downloads/`：默认监听目录。
- `outputs/`：归档后的视频目录。

这些目录默认不会提交到 Git。

## 常用配置

默认监听目录是项目里的 `watched-downloads`。如果你想监听真实下载目录，可以在页面的“下载监听”里改成类似：

```text
C:\Users\你的用户名\Downloads
```

账号的浏览器路径可以填写 Chrome 或 Edge 的完整路径，例如：

```text
C:\Program Files\Google\Chrome\Application\chrome.exe
C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
```

Profile 目录建议每个账号独立设置，例如：

```text
C:\Users\你的用户名\Documents\doubao-profiles\account-a
```
