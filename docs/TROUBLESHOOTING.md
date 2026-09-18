# 排错指南

先跑这个，八成问题它就能直接指出来：

```bash
python campus_login.py doctor
```

它会打印：Python 版本、配置文件路径、本机 IP/MAC、四个探测地址各自的真实响应、
以及配置校验结果。贴 Issue 时请附上这个输出（**记得先删掉账号密码**）。

---

## 一、配置 / 安装

### `找不到配置文件`
还没初始化。跑 `python campus_login.py init`。

### `无法解密已保存的密码`
密码是用 Windows DPAPI 加密的，**只能被保存它的那台机器的那个用户解开**。
换了电脑、换了用户、重装了系统都会这样。重新跑：

```bash
python campus_login.py set-password
```

### `未知的 provider: 'xxx'`
跑 `python campus_login.py providers` 看可用列表。

### 计划任务建好了但没反应
1. `Get-ScheduledTask -TaskName CampusNetLogin | Get-ScheduledTaskInfo` 看上次运行结果；
2. 注意**密码是 DPAPI 加密的，所以任务必须跑在你的用户下（登录触发），不能选 SYSTEM**；
3. 手工跑一下任务，看日志：
   ```powershell
   Start-ScheduledTask -TaskName CampusNetLogin
   Get-Content "$env:APPDATA\campus-net-login\campus-login.log" -Tail 40
   ```

---

## 二、网络 / MAC

### `取不到 MAC` 或者 MAC 取错了
```bash
python campus_login.py status     # 看它取到的 MAC
```
如果取到了 VMware / VirtualBox 的虚拟网卡（`00:50:56:`、`00:0C:29:`、`08:00:27:` 开头），
或蓝牙网卡，就在配置里手工写死真实网卡的 MAC：

```json
"options": { "mac": "001122334455" }
```

Windows 上查真实网卡 MAC：

```powershell
Get-NetAdapter | Where-Object Status -eq Up |
    Select-Object Name, InterfaceDescription, MacAddress
```

挑那个"以太网 / WLAN"的物理网卡。

### `门户没有返回 sessionId`
说明 `/entry?mac=…` 那条链路和你学校的不一样。用 `doctor` 看真实的重定向地址：

- 如果跳的是 `InterFace.do?method=pageInfo…` → 改用 `ruijie_eportal`
- 如果跳的是 `srun_portal…` → 改用 `srun`
- 如果结构完全不同 → 用 `generic_form`，或提 Issue 附上抓包

### 探测点全都不通
有些学校封了 `connect.rom.miui.com` 之类的外网探测点。换成你们学校内肯定通、
且**断网时会被劫持**的地址：

```json
"options": {
  "check_urls": ["http://www.baidu.com", "http://connect.rom.miui.com/generate_204"]
}
```

注意：`check_urls` 里的地址会被当作"期望 HTTP 204"来判断。

---

## 三、登录

### `认证被拒绝: 用户不存在或者密码错误!`
密码不对。重新 `set-password`。
注意有些学校第一次登录需要先改初始密码 / 激活账号。

### `认证被拒绝` 但密码明明是对的
- 账号可能被别处占用（同一账号在另一台设备在线，且学校限制单设备）；
- 有些学校要求先"下线"再登录，手工打开认证页登一次试试；
- 有验证码 / 二次验证的学校，`UsernamePassword` 这条路走不通。

### `HTTP 200 且无失败标志`（generic_form）
说明成功词没配好。用 `-v` 看响应正文，然后调整 `success_contains` / `failure_contains`。

### 提示成功但还是上不了网
```bash
python campus_login.py -v login
```
看最后几行。常见原因：
1. 登录后还要**选运营商 / 选套餐** —— 手工登一次看有没有额外页面；
2. 有**合规 / 实名弹窗**没处理；
3. 探测点本身不通（换个 `check_urls`）。

---

## 四、其他

### 想看每一步的请求细节
```bash
python campus_login.py -v watch
```
或者把配置里 `logging.level` 改成 `"DEBUG"`。
日志文件在 `%APPDATA%\campus-net-login\campus-login.log`（Linux/macOS 见 README 的路径表）。

### 想确认没在重复运行
`watch` 有文件锁，第二个实例会直接退出。看日志里有没有
`已经有一个 campus_login watch 在运行了`。

### 彻底清理
```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
Remove-Item -Recurse "$env:APPDATA\campus-net-login"      # 配置 + 日志
```
```bash
# Linux
./scripts/install-linux.sh --uninstall
rm -rf ~/.config/campus-net-login
```

---

## 还是不行？

提 Issue 时请附上：

1. `python campus_login.py doctor` 的完整输出（**删掉账号密码**）；
2. `python campus_login.py -v login` 的输出；
3. 学校名称 + 认证系统类型（不确定就描述一下断网时浏览器跳转到的页面）；
4. 如果方便，F12 → Network 里那次**成功手工登录**的请求（URL + 表单字段 + 响应），
   敏感信息打码即可。

有这些信息，加一个新适配器通常很快。
