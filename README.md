# 立德研学中心自动抢座脚本

人大立德研学中心（`yxkj.ruc.edu.cn`）自习工位的纯 HTTP 自动化预约脚本。
每天固定时刻（默认 07:10，即次日座位预约窗口开放时刻）自动登录、扫描空位、
破解滑块验证码并提交预约，全程不依赖浏览器。

> ⚠️ 仅供本人单账号自用。脚本内置节流、请求硬上限与看门狗，请求量与人类手动操作同量级。

---

## 一、原理详解

### 1. 总体架构

```
CAS 登录链 ──▶ OAuth 跳转 ──▶ appletBind 换 Bearer token ──┐
                                                             ▼
                定时调度(07:10) ──▶ 查座 getRoomList ──▶ 滑块验证码破解 ──▶ saveReservation 提交
                                                             │
                                          成功 → 记录幂等标记退出；被抢 → 换下一座；验证码失败 → 重解
```

所有请求复用同一个 `requests.Session`（跨域共享 cookie），
CAS → 应用广场 → 研学中心是一条完整的认证链。

### 2. 登录与 token 获取

#### 2.1 为什么必须走 CAS

研学中心自身没有账密登录接口（`accountPasswordLogin` 实测返回 500），
认证完全依赖人大统一身份认证（CAS）+ 应用广场 OAuth 授权。

#### 2.2 完整链路（已逐步实测）

| 步骤 | 请求 | 说明 |
|---|---|---|
| ① 进入登录页 | `GET m.ruc.edu.cn/uc/wap/login?redirect=广场页` | 302 链最终落到 `cas.ruc.edu.cn/cas/login?service=...` |
| ② 解析表单 | 正则提取登录页 | `execution`（一次性令牌）、表单 `action` |
| ③ 验证码 | `GET cas.ruc.edu.cn/cas/captcha.jpg` | 4 位数字，用 **ddddocr**（onnxruntime）识别；非 4 位自动换图重试 ≤8 次 |
| ④ 提交登录 | `POST` 明文账密表单 | 字段：`username / password / authcode / rememberMe / execution / encrypted / _eventId / loginType`。**302 = 成功，200 = 失败**（重新渲染登录页，可提取错误文案区分"验证码错"与"密码错"） |
| ⑤ OAuth 跳转 | `GET m.ruc.edu.cn/uc/api/oauth/index?redirect=...rdAuth&appid=200230106162633619&state=STATE` | 服务端带票据多级 302，最终落地 `yxkj.ruc.edu.cn/kyq-v/#/main/my?...`，URL 的 hash 段携带 `ticketCode / openid / account / sign` 四个参数 |
| ⑥ 换 token | `POST /kyq/static/public/appletBind?openid=..&sign=..&openId=..` | body `{account, ticketCode, token:"", authorization:false}` → `{data:{token}}`，即后续所有业务接口的 Bearer token |

登录页的 `login.js` 中虽有 `RSAUtils.encryptedString` 密码加密代码，
但浏览器实测该库未加载（`typeof RSAUtils === 'undefined'`），原生表单实际**明文提交**。

#### 2.3 token 缓存与探活

- token 连同签发时间写入 `state.json`；
- 每次使用前用 `queryAllRoomList` **探活式使用**，不假设固定 TTL；
- **实测陷阱**：探活接口短时连续请求会返回 `200 + "请勿频繁操作"`（服务端限流），
  限流 ≠ token 失效。`token_alive()` 遇限流退避 3 秒重探，仍限流视为存活，避免误杀触发无谓重登。

### 3. 滑块验证码破解（核心）

研学中心使用 tianai-captcha 滑块，请求体为 **AES-128-CTR + RSA-1024 信封加密**。

#### 3.1 加密协议（逆向 `tac.min.js` + `chunk-detail.js` 确认并实测对拍）

```
明文 JSON ──AES-128-CTR(key,iv)──▶ base64 ──▶ 请求体的 data / custom 字段
key|iv   ──RSA-1024-PKCS1v1.5(站点公钥)──▶ base64 ──▶ ki 字段
```

- `key = iv = 16 随机字节`；CTR 模式无填充，与前端 CryptoJS NoPadding 天然一致；
- 请求体：gen 为 `{"custom", "ki"}`；check 为 `{"id", "data", "custom", "ki"}`；
- 明文 JSON 必须用**紧凑分隔符**（等价 `JSON.stringify`），键序不排序。

#### 3.2 两个实测发现的关键坑

1. **check 的 `id` 取 gen 响应的顶层 `id`**（前端 `currentCaptchaId = response.id`）；
2. **时间字段必须是 ISO 8601 字符串**：该站点未启用 `timeToTimestamp` 配置，
   前端 `startSlidingTime`/`endSlidingTime` 是 JS `Date` 对象，经 `JSON.stringify`
   序列化为 `"2026-09-01T01:23:45.678Z"` 形式。传毫秒整数会被服务端解析抛 500"执行异常"。

#### 3.3 缺口定位算法（numpy 向量化，单张 <100ms）

1. 滑块 PNG 取 alpha 通道，`alpha > 128` 得掩码 `mask` 及其**左边界像素集** `bnd`；
2. 背景 JPEG 转灰度，计算水平梯度 `grad[y, x] = |lum[y, x+1] − lum[y, x]|`；
3. 掩码沿 x 轴平移，对每个候选位置评分：

   \[
   score(x) = 4 \cdot \overline{grad}\big|_{bnd}(x) - \overline{grad}\big|_{mask}(x)
   \]

   即"左边界处梯度要强（缺口边缘）、内部梯度要弱"；
4. 最高分位置 = 缺口；**退化检测**：top1−top2 分差 < 0.5 视为定位不可靠，丢弃换图；
5. 拖动距离 = `(缺口x − 掩码左边界x) / 2`（背景自然尺寸 600×360，显示尺寸 300×180，÷2）。

#### 3.4 轨迹拟人化

- 30–50 个采样点，ease-out 缓动（`1 − (1 − p/0.9)^2.2`）；
- 末段 2–4px **过冲后回拉**（人类拖拽特征）；
- y 轴 ±2px 正弦抖动；总时长 800–1600ms 随机；`t` 毫秒单调递增；
- 首尾事件类型 `start` / `move` / `up`。

#### 3.5 验证码生命周期（实测结论）

- check 成功后 token 形如 `SLIDER_xxxx`，**TTL ≈ 30 秒**；
- **一次性**：提交 saveReservation 即消费（即使业务层拒绝），因此换座必须重新破解；
- 无效/过期码统一返回 `{"code":500, "message":"验证码错误"}` → 脚本自动重解；
- 破解失败（定位退化/校验失败）自动换图重试 ≤5 次。

### 4. 座位查询与预约提交

| 接口 | 请求体（实测格式） | 响应 |
|---|---|---|
| `POST /kyq/static/frontApi/room/getRoomList` | `{endDate: "YYYY-MM-DD", roomId, statDate: "YYYY-MM-DD", authorization: true}` | `[{no, status}]`，`status=0` 可约 |
| `POST /kyq/static/frontApi/reservation/saveReservation` | `{code: 滑块token, endDate: "YYYY-MM-DD", onDate: "YYYY-MM-DD", roomId, mobilePhone: "", seatNumber, authorization: true}` | 成功 `{status: true, code: 200}` |

房间常量（`queryAllRoomList` 实测）：
- 15 层研学自习位 `1661583581389099008`（250 座）
- 14 层研学自习位 `1666357033522270208`（250 座）

**预约窗口**：立德研学中心开放时间为 07:00-23:00。窗口未开时提交返回
`"房间在<日期>未开放，请重新选择日期"`，脚本识别后中止本轮（退出码 1）而非盲目重试。

### 5. 抢座策略与防御机制

- **优先级**：按 `config.json` 的 `rooms` 顺序逐房间扫描；
- **候选排序**：`seatPreferLow~seatPreferHigh` 偏好区间优先，其余按号升序，`seatExclude` 排除；
- **被抢换座**：提交失败（座位已被占）自动换下一候选，≤8 座；
- **限流退避**：连续提交触发 `"请勿频繁操作"` 时退避 3–5 秒后重解验证码再试；
- **全满蹲守**：无空位时每 45 秒轮询，最长蹲守 20 分钟；
- **幂等**：成功预约后 `state.json` 记录 `lastSuccessDate`，当日重复触发直接跳过；
- **防失控**：单次运行请求硬上限 120；`threading.Timer(720s)` 看门狗到点强制退出（码 3）；
- **全程可诊断**：每轮运行向 `logs/run-YYYY-MM-DD.jsonl` 追加摘要
  （结果/耗时/请求数/重试数/退出码），失败响应落盘 `artifacts/`。

### 6. 定时调度（`--at` 参数，进程内实现）

不注册系统计划任务，启动后进程内 sleep 循环：

- 到点触发一次完整抢座流程；
- **启动时已过点** → 顺延明天（不误触发）；
- **休眠错过触发时刻** → 唤醒后立即补跑一次（幂等检查保证无害）；
- `last_run_date` 保证**每日至多执行一次**（旧版曾出现无限补跑缺陷，已实测修复）；
- 触发前 60 秒预热：提前探活/刷新 token，避免挤在触发时刻（`preWarmSec` 开关）；
- 本轮异常只记日志不退出循环，等下一天；Ctrl+C 停止。

---

## 二、使用方法

### 1. 环境要求

- Windows / Linux / macOS，**Python 3.10+**
- 依赖一键安装：

```powershell
pip install -r requirements.txt
```

（`requests` / `cryptography` / `Pillow` / `numpy` / `ddddocr`，均为 pip wheel，无需手动编译。）

### 2. 快速开始

公开仓库不包含真实账号配置。首次使用先复制示例配置：

```powershell
copy config.example.json config.json
```

然后编辑 `config.json`：

```jsonc
{
  "username": "学号",
  "password": "密码",
  "rooms": [                              // 按优先级排列
    {"roomId": "1661583581389099008", "name": "研学自习位(15层)"},
    {"roomId": "1666357033522270208", "name": "研学自习位(14层)"}
  ],
  "seatPreferLow": 1, "seatPreferHigh": 0, // 偏好座位区间（High=0 表示不限制）
  "seatExclude": [],                       // 排除的座位号
  "maxSeatsTry": 8,                        // 单房间最多尝试座位数
  "sliderRetries": 3,                      // 单座验证码失败重解上限
  "sliderAttempts": 5,                     // 单次滑块破解最大尝试次数
  "pollIntervalSec": 45,                   // 全满时轮询间隔
  "pollMaxMin": 20,                        // 全满蹲守上限（分钟）
  "maxRequests": 120,                      // 单次运行请求硬上限
  "watchdogSec": 720,                      // 看门狗（秒）
  "preWarmSec": 60                         // 触发前预热开关（0 关闭）
}
```

### 3. 命令行子命令

```powershell
python main.py test                   # 只读探活：token 有效性 + 明天各房间空位
python main.py test --day today       # 探查今天的空闲座位
python main.py test --day 2026-09-05  # 探查任意指定日期（系统只开放今天/明天）
python main.py test --slider          # 额外验证滑块破解链路（不提交预约）
python main.py test --save-probe      # 用无效验证码探测提交接口（采集错误样本，不产生预约）
python main.py login                  # 强制重新登录刷新 token（写入 state.json）
python main.py login --manual         # 人工输入验证码（OCR 失效时的兜底）
python main.py status                 # 查看 token 缓存与最近运行摘要
python main.py records                # 查询预约记录与违约记录（不显示手机号）
python main.py run                    # 立即执行一次抢座（默认目标：明天）
python main.py run --day today        # 抢今天的空位（适合当天临时去自习）
python main.py run --at 07:10         # 【日常用法】挂机：每天 07:10 自动抢座（默认明天）
```

> `--day` 支持三种取值：`tomorrow`（默认）、`today`、`YYYY-MM-DD`。
> 系统可预约今天和明天两天内的座位，今天的预约窗口在当天 07:10 开放。
> `--at` 挂机模式下日期在**触发时刻动态解析**，例如 `run --at 07:10 --day today`
> 每天 07:10 触发时抢的正是当天（窗口刚开放）的座位。

### 4. 图形界面

源码方式启动：

```powershell
python gui_app.py
```

本地已有 PyInstaller 打包产物时，可直接运行：

```powershell
dist\LideStudyCenterGUI\LideStudyCenterGUI.exe
```

GUI 的配置和状态默认写入用户目录下的 `LideStudyCenter` 应用数据目录，不依赖仓库内的 `config.json`。

### 5. 日常使用流程

1. 开机后在项目目录执行一次：

   ```powershell
   python main.py run --at 07:10
   ```

2. 保持终端窗口开着即可。脚本会打印下次执行时刻并静默等待；
   每天 07:09 左右自动预热 token，07:10 窗口开启即抢座。
3. 抢到后日志显示 `预约成功: <房间> <座位号>`，`state.json` 记录
   `lastSuccessDate`，当天重复触发自动跳过。
4. 不需要时 `Ctrl+C` 或直接关闭窗口，无任何系统级残留。

### 6. 打包 exe

本项目保留 PyInstaller 配置文件 `lide_study_center_gui.spec`。本地打包：

```powershell
pip install -r requirements.txt
pyinstaller lide_study_center_gui.spec --clean
```

生成目录：

```text
build/                         # PyInstaller 中间产物
dist/LideStudyCenterGUI/        # GUI 可执行程序目录
```

`build/`、`dist/` 和 `*.exe` 已被 `.gitignore` 忽略；GitHub 仓库只建议提交源码。需要分发 exe 时，建议把压缩后的 `dist/LideStudyCenterGUI/` 上传到 GitHub Releases，不要直接提交到 git 历史。

### 7. 退出码

| 码 | 含义 |
|---|---|
| 0 | 预约成功（或幂等跳过） |
| 1 | 满座 / 蹲守超时 / 预约窗口未开放 / 请求硬上限 |
| 2 | 认证失败（账密错误等） |
| 3 | 看门狗超时强退 |
| 4 | 未知异常（堆栈落盘日志） |

### 8. 日志与产物

```text
logs/run-YYYY-MM-DD.jsonl   # 每轮一行 JSON 摘要（结果/耗时/请求数/重试数/退出码）
artifacts/                  # 失败诊断：提交响应、验证码图片、登录快照
state.json                  # 运行时状态：token、签发时间、幂等标记
```

这些文件可能包含账号状态、token、运行轨迹或诊断样本，已被 `.gitignore` 忽略，不应上传到公开仓库。

### 9. 故障排查

| 现象 | 处理 |
|---|---|
| 日志反复出现"验证码错误" | 正常自动重解；若连续超限，`python main.py test --slider` 定位 |
| CAS 验证码 OCR 总失败 | `python main.py login --manual` 人工输入一次（token 可长期缓存） |
| "预约窗口尚未开放" | `--at` 时刻早于 07:10 会白跑一轮；建议就设 07:10 |
| "请勿频繁操作" | 脚本自动退避重试；若频繁出现，调大 `pollIntervalSec` |
| 电脑休眠错过 07:10 | 唤醒后自动补跑一次（幂等保护），无需干预 |
| 想确认上次是否成功 | `python main.py status` |

### 10. 单元测试

```powershell
python -m unittest discover -s tests -v
```

覆盖：AES-CTR/RSA 信封对拍、滑块缺口定位回归（真实抓包样本）、轨迹格式、
调度器不变量（每日至多一次、异常不退出、启动过点顺延）、GUI 服务与 GitHub 打包卫生。

---

## 三、GitHub 上传建议

- 仓库提交源码、测试、README、`requirements.txt`、`config.example.json` 和 `lide_study_center_gui.spec`。
- 不提交 `config.json`、`state.json`、`logs/`、`artifacts/`、`build/`、`dist/`、`*.exe`。
- 如果要公开仓库，上传前再次确认没有真实学号、密码、token、日志或验证码样本。
- exe 建议走 GitHub Releases；源码仓库保持轻量、可审计。

初始化并提交：

```powershell
git init
git add .
git commit -m "chore: prepare project for GitHub"
```

关联远程仓库后推送：

```powershell
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

---

## 四、文件结构

```text
lide_study_center/
├── main.py                      # 入口：run/test/login/status/records 子命令 + --at 调度
├── gui_app.py                   # Tkinter GUI 入口
├── gui_workers.py               # Tkinter 后台任务执行器
├── config.example.json          # 可提交的配置模板
├── config.json                  # 本地真实账号/房间配置（.gitignore）
├── state.json                   # 运行时：token、签发时间、幂等标记（.gitignore）
├── requirements.txt
├── lide_study_center_gui.spec   # PyInstaller GUI 打包配置
├── lib/
│   ├── httpclient.py            # Session 封装：400ms 全局节流、退避重试、手动 302 跟跳
│   ├── encrypt.py               # AES-128-CTR + RSA-1024 信封加密
│   ├── slider.py                # numpy 缺口定位 + 拟人轨迹 + gen/check 全链
│   ├── login.py                 # CAS + ddddocr + OAuth + appletBind + token 缓存
│   ├── book.py                  # 查座、预约提交、预约历史查询
│   ├── gui_service.py           # GUI 服务层：登录、记录、房间、座位、预约
│   ├── runtime_paths.py         # GUI 运行时目录
│   └── scheduler.py             # --at 日循环调度（补跑/顺延/每日至多一次）
├── tests/                       # 单元测试
├── logs/                        # 按日 JSONL 运行摘要（.gitignore）
├── artifacts/                   # 失败诊断产物（.gitignore）
├── build/                       # 本地打包中间产物（.gitignore）
└── dist/                        # 本地 exe 产物（.gitignore）
```
