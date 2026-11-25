# user tool

Trae Mouth 本机登录助手，只依赖 Python 标准库。

## 文件

- `login_helper.py`：回环授权监听 + 干净浏览器 profile + 提交凭据
- `start_login.bat`：Windows 启动脚本

## 用法

### 网页授权

```bat
start_login.bat "http://127.0.0.1:18792" "面板生成的导入码"
```

不传参数双击也行，默认中转站是 `http://127.0.0.1:18792`。

面板生成导入码后，本机助手会自动读取最近一次码，不需要手动粘贴。码文件写在：

- `data/last_import_code.json`
- `%LOCALAPPDATA%\TraeMouth\last_import_code.json`

使用成功后会删除。也可以显式指定：`--code-file <文件>`。

助手会：

1. 在 `%LOCALAPPDATA%\TraeMouth\auth-profiles` 创建全新 Chrome/Edge profile
2. 直接打开 `https://www.trae.cn/authorization`（只有一个浏览器窗口）
3. 监听 `127.0.0.1:<随机端口>/authorize`
4. 把回调参数和设备信息提交到中转站
5. 自动关闭浏览器并退出

### 本机已登录账号直接导入

```bat
start_login.bat "http://127.0.0.1:18792" "导入码" --import-local
```

读取 `%APPDATA%\Trae CN\User\globalStorage\storage.json`，不需要打开浏览器。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--no-browser` | 不自动打开浏览器，手动访问助手本地页面 |
| `--browser chrome\|edge` | 指定浏览器 |
| `--profile-dir <dir>` | 复用指定 profile 目录 |
| `--keep-profile` | 成功后保留临时 profile |
| `--code-file <file>` | 从指定文件读取导入码 |
| `--device-json <file>` | 使用指定设备指纹 |
| `--timeout <秒>` | 等待授权超时，默认 300 |

## 注意

- 助手只监听 `127.0.0.1`，不暴露局域网端口。
- 导入码 10 分钟内有效，只能使用一次。
- 每次网页授权默认使用全新 profile，不复用日常浏览器 cookie。
