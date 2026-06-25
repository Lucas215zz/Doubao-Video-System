# 豆包 Cookie 池自动化后端

本后端参考 `E:\github\MJFlow4.0\services\seedance` 的小云雀服务结构，实现豆包视频生成的 Cookie 池管理与任务接口。

新增文件：

- `doubao_v3.py`：豆包接口适配、Cookie JSON 读取、Cookie 池轮询、额度缓存、Samantha 视频消息构造和结果解析。
- `app_doubao_v3.py`：Flask API 服务，提供 Cookie 管理、任务提交、任务查询和 OpenAI 兼容的视频生成接口。
- `requirements-doubao.txt`：Python 服务依赖。

## 启动

```powershell
python -m pip install -r requirements-doubao.txt
python app_doubao_v3.py
```

默认地址：

```text
http://127.0.0.1:8034
```

前端控制台：

```powershell
npm.cmd run build
npm.cmd start
```

打开：

```text
http://127.0.0.1:5174
```

## Cookie 池

Cookie 文件放在 `cookies/` 目录，或通过接口上传。文件格式支持浏览器导出的 JSON 数组，也支持 `{ "cookies": [...] }`。

常用接口：

```text
GET    /api/cookies
POST   /api/cookies
PATCH  /api/cookies/<name>/status
DELETE /api/cookies/<name>
GET    /api/cookies/<name>/test
POST   /api/cookies/check-all
```

`/api/cookies/<name>/test` 只调用豆包的视频额度查询接口，不会提交生成任务。

## 视频生成

```text
POST /v1/videos/generations
GET  /v1/videos/generations/<task_id>
POST /api/generate-video
GET  /api/task/<task_id>
```

示例：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8034/v1/videos/generations `
  -ContentType application/json `
  -Body '{"prompt":"一只橙色机器人在雨夜霓虹街头慢慢走过","ratio":"16:9","model":"doubao-seedance-2.0"}'
```

带参考图时使用 `multipart/form-data`，字段名与小云雀服务保持一致为 `files`，可以传多张：

```powershell
curl.exe -X POST http://127.0.0.1:8034/v1/videos/generations `
  -F "prompt=参考图片中的人物在雨夜霓虹街头慢慢走过" `
  -F "ratio=16:9" `
  -F "model=doubao-seedance-2.0" `
  -F "duration=5" `
  -F "files=@C:\path\ref1.png" `
  -F "files=@C:\path\ref2.jpg"
```

后端会用当前轮询到的 Cookie 账号上传参考图，再把豆包返回的 `ImageUri/fileKey` 组装成 Samantha 视频生成消息的 `attachments`。如果已经有豆包附件对象，也仍然可以通过 JSON 字段 `attachments` 传入。

注意：生成接口会真实提交豆包视频任务，可能消耗账号次数。

## 可选环境变量

豆包的 `/samantha/chat/completion` 会带页面运行时参数。服务会自动补齐 `aid`、`device_id`、`web_id`、`web_tab_id` 和 `fp`，并优先从 cookie 池文件里的 `s_v_web_id` 取得 cookie 对应的 `fp`。

只有在你明确想覆盖自动值时，才需要设置这些环境变量：

```powershell
$env:DOUBAO_COMMON_PARAMS='{"aid":"...","device_id":"...","web_id":"...","web_tab_id":"..."}'
$env:DOUBAO_FP='...'
```

服务不会读取 Chrome 本地 cookie、localStorage、sessionStorage 或浏览器 Profile 数据，只使用你上传到 `cookies/` 目录的 cookie JSON 文件。
