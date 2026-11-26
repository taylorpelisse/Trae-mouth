<p align="center">
  <img src="admin-web/public/logo.svg" width="104" alt="Trae Mouth">
</p>

# Trae Mouth

把 Trae CN 账号池转换成 OpenAI 兼容 API，并附带管理后台。

## 功能

- 默认全局 IDE + 最大上下文
- 支持 IDE / Solo / Work / Remote 通道
- 多账号粘性、轮转、失败换号、并发限制
- 账号、模型、Key、日志、积分、设置、测试台
- 自动签到、错峰的令牌刷新 / 账号保活 / 积分刷新

## 快速开始

要求 Python 3.10+。

```bat
start.bat
```

或：

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python run.py
```

打开 `http://127.0.0.1:18792/admin`，首次访问创建管理员。

远程部署参考 `.env.example`。

## 账号接入

1. **登录助手**：管理端生成导入码并下载 `.bat`，运行后浏览器授权，账号自动回传。
2. **手动录入**：粘贴 Cloud-IDE-JWT / refreshToken。
3. **本机导入**：服务端与 Trae 同机时读取 `storage.json`。

Token 只以 AES-256-GCM 密文入库。

## API

```text
GET  /v1/models
POST /v1/chat/completions
```

鉴权使用 `Authorization: Bearer <key>` 或 `x-api-key`。未配置 `TRAE_MOUTH_API_KEY` 时只允许本机回环访问。

通道：

- 默认 IDE，普通模型 ID
- `remote/<模型>`、`work/<模型>`、`design/<模型>`、`solo/<模型>`
- 或请求头 `X-Trae-Channel`

工具调用使用提示词桥接；Web 通道不支持下游工具。

## 管理端

仪表盘、账号导入/刷新/签到、模型目录、Key 配额、日志、设置、测试台。

管理端使用 HttpOnly session + CSRF；下游 Key 只存哈希，明文仅展示一次。

## 账号维护

针对全部账号的操作都按错峰方式执行：每次打乱顺序，账号之间留出间隔和随机抖动，与官方客户端一个设备对应一个账号的使用方式贴近。

- **自动签到**：在「每日签到」里设置，当天没签成的会自动补签
- **令牌刷新**：到期前 24 小时才刷新，优先走带设备签名的原生接口；另有每小时的兜底检查，失败后逐次加长等待
- **账号保活**：每天在设定时刻逐个确认账号状态
- **积分刷新**：每天在设定时刻拉取；积分页面只读本地记录，不请求上游
- **模型目录**：缓存 1 小时，各账号到期时间随机错开；IDE 部分一次批量请求取回，网页端目录每天最多拉一次；列出模型只读缓存，过期部分在后台错峰刷新

时刻、间隔和抖动在设置页的「账号设置」里调整。

## 限制

- 单实例运行；同一 `data/` 目录有文件锁
- SQLite，不支持多机共享
- Web 通道不支持下游工具
- 原生工具仅预留开关

## 安全

- `.env`、`data/`、`master.key`、SQLite 数据库不要提交
- `/healthz`、`/metrics`、`/admin/api/status` 不需要登录

仅供学习和个人使用。请遵守 Trae 服务条款。
