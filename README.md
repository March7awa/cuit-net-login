# campus-net-login · 校园网自动登录

> 开机自动登录校园网，断线自动重连，再也不用每天手输密码。
>
> 纯 Python 标准库实现，**零第三方依赖**，Windows / Linux / macOS 都能跑。

[English](README.en.md) · [工作原理](docs/HOW_IT_WORKS.md) · [排错指南](docs/TROUBLESHOOTING.md)

---

## 它能解决什么

- 每次开机、每次从睡眠唤醒，都要打开浏览器手动输一遍账号密码；
- 校园网时不时自己掉线，等到发现时已经断了几分钟；
- 想挂着下载 / 跑远程任务，一断线就全断了。

这个程序会常驻后台，每隔十几秒探测一次网络：**一旦发现被门户劫持，就用你保存的凭据自动登录**，同时把过程写进日志。

```
[2026-09-18 12:37:49] INFO    看门狗启动: 每 20s 检查一次网络
[2026-09-18 13:02:11] WARNING 检测到断网（第 1 次）: 被门户劫持: http://connect.rom.miui.com/generate_204 -> http://10.254.241.66/portal/...
[2026-09-18 13:02:11] INFO    步骤1 完成: sessionId=e66015b812b8 userIp=10.18.0.100 nasIp=1.1.1.1
[2026-09-18 13:02:12] INFO    步骤3 完成: croypto=YnHmFS0k… execution=0807e9f8…
[2026-09-18 13:02:12] INFO    步骤4 提交结果: HTTP 302
[2026-09-18 13:02:12] INFO    登录成功: 认证成功（已验证可上网）
```

---

## 30 秒上手

需要 **Python 3.8+**（Windows 上安装时记得勾 *Add Python to PATH*）。

### Windows 用户：双击一个程序就完事

双击 **`校园网自动登录.pyw`**（或桌面上的快捷方式），会打开一个窗口：

```
┌─ 校园网自动登录 ────────────────────────────────────────┐
│  1.选择认证方式 › 2.认证服务器 › 3.账号密码 › 4.测试登录 › 5.完成 │
├─────────────────────────────────────────────────────────┤
│  你们学校的校园网是怎么认证的？                          │
│                                                         │
│  我的学校  [（不确定／让程序自己检测）▾ ]  ← 有就选，没有就算了 │
│                                                         │
│  认证方式                                               │
│   ◉ 锐捷 SAM/ePortal 5.x + CAS 单点登录                 │
│   ○ 锐捷 ePortal 经典网页认证 (InterFace.do)            │
│   ○ 深澜 Srun 门户认证 (srun_portal)                    │
│   ○ 通用 HTML 表单 POST                                 │
│                                                         │
│  [自动检测认证方式]        ← 认不出学校就点这个          │
├─────────────────────────────────────────────────────────┤
│  [上一步]                                    [下一步]    │
└─────────────────────────────────────────────────────────┘
```

第 2 页是这样，**不懂就整页跳过，直接点「下一步」**：

```
┌─ 校园网自动登录 ────────────────────────────────────────┐
│  1.选择认证方式 › 2.认证服务器 › 3.账号密码 › 4.测试登录 › 5.完成 │
├─────────────────────────────────────────────────────────┤
│  认证服务器                                             │
│                                                         │
│  这一页可以整页跳过，直接点「下一步」。                   │
│  断开校园网时程序会自己去碰一下网络，从门户的跳转里把      │
│  服务器地址和接入设备地址探测出来，不需要你知道这些。      │
│                                                         │
│  [自动检测（推荐先点这个）]  检测到：服务器 http://10...  │
│                                                         │
│  服务器   [                        ]  ← 留空即可          │
│  接入设备 [                        ]  ← 留空即可          │
│  运营商   [ 自动（用列表第一项）  ▾ ]  ← 移动/电信/联通   │
├─────────────────────────────────────────────────────────┤
│  [上一步]                                    [下一步]    │
└─────────────────────────────────────────────────────────┘
```

服务器和接入设备是**学校内网的地址，普通用户没理由知道**，所以留空就行 ——
程序会在断网时自己探测，学到之后还会记住。运营商那一栏按你办宽带时选的那家选
（中国移动 / 中国电信 / 中国联通），不确定就先留「自动」，登录日志里会打印门户
实际给了哪些选项。

五步走完，账号密码存好、开机自启也顺手装上了。之后同一个窗口会变成状态面板：

- **● 在线 / ● 离线** 一眼看到
- `立即登录` `刷新状态` `修改密码` `打开配置文件`
- `重新配置` `打开日志`，以及开机自启的开关（随时可以关掉）
- 底部实时滚动运行日志

面板上直接写着配置文件在哪一行，点 `打开配置文件` 就能用记事本改：
除了密码（Windows 上是 DPAPI 密文，看不懂是正常的）以外的字段手改都有效。
**改密码不用重跑向导**，点 `修改密码` 输一遍新密码就行。

图形界面用的是 Python 自带的 tkinter，不需要额外安装任何东西。

### 命令行方式（Linux / macOS / 喜欢终端的人）

**不用 git 也行**：在 GitHub 页面点绿色的 `Code` → `Download ZIP`，解压后直接跳到
下面的 `python campus_login.py init`。

```bash
git clone https://github.com/March7awa/cuit-net-login.git
cd cuit-net-login

python campus_login.py init        # 交互式配置：选认证方式、填账号密码
python campus_login.py login       # 立刻试一次
python campus_login.py watch       # 前台常驻，看看日志对不对
```

> 国内直连 github.com 时通时不通。clone 不动就换成镜像地址：
> ```
> git clone https://gh-proxy.com/https://github.com/March7awa/cuit-net-login.git
> ```
> 或者干脆用上面的 Download ZIP。

跑通之后，装成开机自启：

```powershell
# Windows（管理员不需要，普通 PowerShell 即可）
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

```bash
# Linux
./scripts/install-linux.sh
sudo loginctl enable-linger "$USER"     # 可选：不登录也自动跑

# macOS
./scripts/install-macos.sh
```

卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
```
```bash
./scripts/install-linux.sh --uninstall   # 或 ./scripts/install-macos.sh --uninstall
```

---

## 支持哪些学校 / 认证系统

| provider | 认证系统 | 状态 |
|---|---|---|
| `ruijie_sam_cas` | 锐捷 SAM / ePortal 5.x + CAS 单点登录 | ✅ **已在真实门户上端到端验证**（成都信息工程大学） |
| `srun` | 深澜 Srun 门户（`srun_portal`） | ✅ 加密算法与门户自身 JS **逐字节对拍通过**；HTTP 链路按官方接口实现 |
| `ruijie_eportal` | 锐捷 ePortal 经典接口 `InterFace.do` | ⚠️ 按公开接口实现，尚未在真实环境验证 |
| `generic_form` | 任意普通 HTML 表单 | ⚠️ 需要你自己填表单字段（见下） |

> 「验证」是什么意思：`ruijie_sam_cas` 这条链路是用真实门户跑通的 ——
> 会话建立、工作流、CAS 页面解析、AES 加密、表单编码全部正确，
> 服务端返回的是「用户不存在或者密码错误」而不是任何参数错误。
> 详见 [工作原理](docs/HOW_IT_WORKS.md)。

### 怎么知道自己是哪一种？

断网时随便打开一个 http 网站，看浏览器跳到哪里：

| 现象 | 用哪个 provider |
|---|---|
| 跳到 `http://10.x.x.x/portal/entry/pc/authenticate` 这种 Angular 页面 | `ruijie_sam_cas` |
| 跳到 `http://x.x.x.x/eportal/InterFace.do?method=pageInfo...` | `ruijie_eportal` |
| 跳到 `http://x.x.x.x/srun_portal_pc?ac_id=1&...` 或 `cgi-bin/srun_portal` | `srun` |
| 其它普通登录页面 | `generic_form` |

不确定就跑 `python campus_login.py doctor`，它会把探测到的重定向地址打出来。
图形向导第 1 页的「自动检测认证方式」按钮做的就是这件事。

> **你不需要知道认证服务器和接入设备的地址。** 这两项在向导第 2 页可以直接留空
> （整页跳过），程序会在断网时自己去碰网络、从门户跳转里读出来。手填只是给
> 高级用户留的后路。

### 没有适配器怎么办：`generic_form`

按 F12 → Network 打开认证页、登一次，把登录请求照抄进配置：

```json
{
  "provider": "generic_form",
  "username": "2025000000",
  "options": {
    "login_url": "http://10.0.0.1/doLogin",
    "method": "POST",
    "content_type": "form",
    "fields": {
      "userName": "{username}",
      "pwd":      "{password}",
      "domain":   "default"
    },
    "success_contains": ["success", "认证成功"]
  }
}
```

`{username}` / `{password}` 会被自动替换。如果门户的密码是前端加密的，
用 `ruijie_sam_cas` 的 AES 实现思路做个小适配器（欢迎 PR！）。

---

## 配置说明

`config.json` 默认放在：

| 系统 | 位置 |
|---|---|
| Windows | `%APPDATA%\campus-net-login\config.json` |
| Linux | `~/.config/campus-net-login/config.json` |
| macOS | `~/Library/Application Support/campus-net-login/config.json` |

程序也会优先使用**当前目录**下的 `config.json`，方便便携使用。
也可以用 `-c` 显式指定：`python campus_login.py -c D:\my.json watch`。

完整示例见 [`config.example.json`](config.example.json)。

| 字段 | 含义 |
|---|---|
| `provider` | 用哪个适配器 |
| `username` / `password` | 账号；密码由 `set-password` 或界面上的「修改密码」写入（Windows 上是 DPAPI 密文，<br>手写成明文也认，程序会在下次保存时加密回去） |
| `options.portal` | 认证服务器地址。**可以留空** —— 断网时程序会从门户跳转里自己探测 |
| `options.nasip` | 接入设备（AC/NAS）地址。**可以留空** —— 同样自动探测，学到之后会写回配置 |
| `options.mac` | `auto` 自动取默认网卡的 MAC；取错了就手填 12 位十六进制 |
| `watch.interval` | 每隔多少秒探测一次网络（默认 20） |
| `watch.check_min_ok` | 有几个探测点通了就算在线（网络很烂时可以调成 2 更保守） |
| `logging.level` | `DEBUG` 能看到每一步请求细节 |

> **`portal` 和 `nasip` 不懂就留空。** 这两个是学校内网的地址，普通用户没理由知道。
> 只要在**没认证（被门户拦着）**的状态下运行，程序就会自己去碰一下网络，从校园网
> 设备的重定向里把这两个地址读出来；`nasip` 学到后还会自动写回 `config.json`，
> 下次直接复用。图形向导第 2 页有个「自动检测」按钮，点一下就会填好；
> 什么都不点、直接「下一步」也能用。

---

## 常用命令

```bash
python campus_login.py init             # 交互式生成配置（--preset cuit 可直接套用预设）
python campus_login.py set-password     # 保存 / 修改密码
python campus_login.py login            # 登录一次（--force 强制重登）
python campus_login.py watch            # 常驻看门狗（--once 只检查一次，给计划任务用）
python campus_login.py status           # 看当前网络和配置
python campus_login.py doctor           # 诊断：探测点、重定向、配置校验
python campus_login.py selftest         # 算法自检
python campus_login.py providers        # 列出适配器
```

---

## 安全说明

- **密码怎么存的**：Windows 上用 DPAPI（`CryptProtectData`）加密后写进 `config.json`。
  密文只能被**同一台机器的同一个用户**解开，拷到别的电脑或别的账号上都解不开。
  Linux / macOS 上会退化成明文，但配置文件权限会被设成 `0600`。
- **密码会明文过网吗**：`ruijie_sam_cas` 和 `srun` 都会按门户自身的方式做前端加密，
  和浏览器行为一致。**但校园网门户本身多是 HTTP**，中间人理论上仍可看到加密前的
  交互。这属于学校侧的设计，不是本程序能改变的。
- **不要提交 `config.json`**：`.gitignore` 已经排除了它。真要分享配置，请用
  `config.example.json`。
- 本程序只做「用你自己的账号登录你自己的网络」，不修改任何系统文件、不装驱动、
  不劫持流量。

---

## 项目结构

```
campus-net-login/
├── 校园网自动登录.pyw           # 双击入口（Windows）
├── campus_login.py              # CLI 入口（Linux / macOS / 终端用户）
├── campusnet/
│   ├── gui.py                   # 图形化向导 + 主面板（tkinter）
│   ├── presets.py               # 学校预设（CLI 和 GUI 共用）
│   ├── aes.py                   # 纯 Python AES-128-ECB（CryptoJS 兼容）
│   ├── httpx.py                 # urllib 封装：Cookie、重定向控制、URL 净化
│   ├── netutil.py               # 在线探测 / 门户劫持检测 / 本机 IP 与 MAC
│   ├── secret.py                # 密码存储（Windows DPAPI）
│   ├── config.py                # 配置读写与路径发现
│   ├── runner.py                # 看门狗、日志、单实例锁
│   └── providers/               # 各认证系统适配器
│       ├── ruijie_sam_cas.py    #   锐捷 SAM 5.x + CAS   ← 已验证
│       ├── srun.py              #   深澜 Srun            ← 算法对拍通过
│       ├── ruijie_eportal.py    #   锐捷 ePortal 经典
│       └── generic_form.py      #   通用表单
├── assets/                      # 图标（icon.ico / icon.png / 原图）
├── scripts/                     # 开机自启（Windows 计划任务 / systemd / launchd）
├── tools/
│   ├── make_icon.py             # 用自己的校徽重新生成图标
│   ├── srun_reference.js        # 深澜算法的 JS 参照实现（对拍用）
│   ├── verify_algorithms.py     # 一键跑全部算法自检
│   ├── verify_discover.py       # 本地假门户，验证「服务器/接入设备」能自动探测
│   ├── verify_reconnect.py      # 拔网线再插回去，验证几秒内自动重连
│   └── verify_broken_permissions.py  # 把配置目录的权限搞坏，验证程序能自救
└── docs/
```

### 换成你自己学校的图标

程序的图标就是一张普通图片转出来的。把你的校徽丢进去重跑一次即可
（这一步需要 Pillow，只有打包/改图标时才用，跑程序本身不需要）：

```bash
pip install Pillow
python tools/make_icon.py assets/你的校徽.jpg
```

会生成 `assets/icon.ico`（含 16～256 七个尺寸）和 `assets/icon.png`，
窗口标题栏和桌面快捷方式都会自动用上。

默认会把纯色背景抠成透明。判断依据是**饱和度**而不是「接近白色」——
背景和它下面的灰色投影都是中性色，而校徽通常是彩色的，这样抠不会有
灰色残留。抠图只从图像四边泛洪，所以被图形包住的白色（比如吉祥物的
眼白、徽章内圈）会原样保留。想保留原背景加 `--bg keep`。

> **换了图标但桌面没变？** Windows 有图标缓存。按 F5 刷新桌面，或者在
> 任务管理器里重启「Windows 资源管理器」。脚本跑完也会提示。

### 自己验证算法

```bash
python tools/verify_algorithms.py
```

会检查 AES 是否命中 FIPS-197 标准向量、CryptoJS 兼容模式是否能复现一份
**真实门户抓包密文**，以及深澜 xEncode 是否与门户自身的 JavaScript 完全一致。

### 自己验证「拔网线再插回去，几秒内重连」

```bash
python tools/verify_reconnect.py
```

把网卡链路和网络探测换成可控的桩，量「链路恢复 → 发起登录」的延迟，
并断言拔线期间不会反复去敲门户。不碰真网卡。

### 自己验证「不填服务器地址也能用」

```bash
python tools/verify_discover.py
```

这个会在本机起一个**假的校园网门户**，模拟「被 AC 劫持 → 跳到门户 → 门户给出
会话和真实接入设备地址」这条链路，然后断言在 `portal` 和 `nasip` **都留空**的
情况下程序确实能把两个地址探测出来、并且不会写到你的真实配置里。全程不碰外网。

---

## 常见问题

**Q：别人电脑上提示 `[Errno 13] Permission denied: ...\campus-net-login\config.json`？**
这是**早期版本**留下的坑：那时候保存完配置会跑一句
`icacls /inheritance:r /grant:r "%USERNAME%":F`，想把权限收紧。但在域账号、
微软账号、中文用户名上，`%USERNAME%` 解析出来的主体未必就是正在运行的那个账号 ——
继承来的权限先被删掉、新的授权又落到别人头上，**属主就被自己的配置文件锁在门外了**。

现在的版本：

- 不再用 `/inheritance:r`，只**追加**当前用户 + SYSTEM + 管理员的授权，不可能自锁；
- 读配置失败时会**先试着把权限修回来**（属主天然有权改自己的 DACL），
  修好就继续用原来那份，配置一点不丢；
- 实在修不动就换到 `%LOCALAPPDATA%\campus-net-login\config.json` 继续存，
  下次启动也会自己去那儿找 —— 不会再卡在「用不了」。

已经在旧版本上锁死了、又不想重装的话，在**命令提示符**里跑一行就好
（路径换成报错里的那个目录）：

```
icacls "%APPDATA%\campus-net-login" /grant "%USERNAME%":(OI)(CI)F /T
```

**Q：密码改了 / 打错了，怎么重新设置？**
图形界面主面板点 `修改密码`，输两遍就完事，**不用重跑向导**。
命令行用 `python campus_login.py set-password`。
也可以直接点 `打开配置文件`，把 `password` 改成明文密码
（`"password": "你的密码"`，手写的明文程序照样认），下次保存时它会自动加密回去。

**Q：换了台电脑，配置文件拷过去说「密码解不开」？**
正常的。Windows 上用 DPAPI 加密，密文只能被**同一台机器的同一个用户**解开 ——
这正是它比明文安全的地方。在新机器上点一次 `修改密码` 重新输一遍即可。

**Q：提示「取不到 MAC」/ MAC 不对怎么办？**
`python campus_login.py status` 会打印它取到的 MAC。如果不对（比如取到了 VMware 虚拟网卡），
在配置里手工指定：

```json
"options": { "mac": "001122334455" }
```

**Q：`login` 说成功，但还是上不了网？**
有些学校登录后还要选运营商 / 服务，或者有合规弹窗。先手动登一次看有没有额外步骤，
然后跑 `python campus_login.py doctor` 把输出发到 Issue。

**Q：拔了网线再插回去，多久会自动重连？**
几秒内。看门狗每 20 秒检查一次，但**拔插网线是本地就能看出来的**（读网卡的
链路状态，一个包都不发），所以链路一恢复就立刻重新认证，不会傻等下一轮。
以前这里有个坑：拔线期间每个探测点都要等满超时，一轮要 20 秒，
插回线时如果正卡在这一轮里就得白等十几秒 —— 这段时间里用户往往自己开了
浏览器，于是误以为「只有校园网登录窗口弹出来它才会连」。

**Q：常驻看门狗和 5 分钟一次的兜底任务会不会同时重登？**
不会。`watch` 用文件锁 + 心跳保证只有一个在重登；兜底任务发现常驻进程还活着
就跳过，只有常驻进程真的卡死（心跳超过 3 分钟没动）时才接管。

**Q：开机时网络还没起来，会失败吗？**
不会。看门狗是循环探测的，网一通就会自动登录。任务也设了失败重启。

**Q：两个进程会不会打架？**
不会。`watch` 用文件锁保证只有一个常驻实例；计划任务里的 `--once` 是独立兜底，
它只在发现断网时才动作。

**Q：怎么彻底停掉？**
Windows：`... install-windows.ps1 -Uninstall`；或者任务管理器结束 `pythonw.exe`。

更多见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

---

## 免责声明

本项目仅供**个人在自己的设备上自动化自己的网络登录**使用。
请遵守你所在学校的网络使用规定。作者不对任何滥用或由此产生的后果负责。

---

## 贡献

欢迎 PR，尤其是**新的认证系统适配器**和**学校预设**：

1. Fork 后新建 `campusnet/providers/你的名字.py`，继承 `Provider`，
   参考 `ruijie_sam_cas.py` 的写法；
2. 在 `campusnet/providers/__init__.py` 的 `_BUILTINS` 里注册；
3. 在 `campusnet/presets.py` 的 `PRESETS` 里加一条学校预设（可选 —— 只有在地址
   真的必须手填时才加，见上面「留空即可」的说明）；
4. 跑一遍 `python tools/verify_algorithms.py`、`python tools/verify_discover.py`
   和 `python campus_login.py selftest`。

**请勿在 PR 里提交任何真实账号、密码或内网地址。**

## License

[MIT](LICENSE)
