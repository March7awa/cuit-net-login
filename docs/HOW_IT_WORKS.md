# 工作原理

这份文档记录各个认证链路是怎么被还原出来的，以及每一步的真实抓包证据。
如果你要新增适配器，照着这个思路做就行。

---

## 1. 通用思路

校园网门户本质上就是一个网站，登录只是几个 HTTP 请求。所以流程永远是：

```
探测是否断网  ->  发现被门户劫持  ->  按门户自己的方式发一遍登录请求  ->  验证能上网
```

顺序上唯一麻烦的是**第一步**：怎么判断"断网"而不是"网站挂了"。
本程序用的是业界通用的 captive portal 探测法 —— 请求几个「有确定响应」的地址：

| 探测地址 | 在线时的正确响应 |
|---|---|
| `http://connect.rom.miui.com/generate_204` | HTTP **204** |
| `http://www.msftconnecttest.com/connecttest.txt` | 正文 `Microsoft Connect Test` |
| `http://connectivitycheck.gstatic.com/generate_204` | HTTP **204** |
| `http://captive.apple.com/hotspot-detect.html` | 正文 `Success` |

被门户劫持时，这些请求会被 **302 重定向**到认证页（或者返回一个登录页 HTML），
响应就不再符合预期。重定向的 `Location` 里通常还带着会话参数，
这就是我们进入认证流程的入口。

---

## 2. `ruijie_sam_cas`：锐捷 SAM 5.x + CAS 单点登录

> 这条链路是完整逆向出来的，并且在**真实门户**上端到端验证过。
> 下面每一条都能在抓包里对上。

### 2.1 门户长什么样

成都信息工程大学的认证页是 `http://10.254.241.66/portal/entry/pc/authenticate;flowParams=undefined;from=`，
一个 Angular 单页应用。页脚写着"锐捷网络股份有限公司"，配置在 `/portal/assets/config.js`。

### 2.2 完整链路

```
┌─ 步骤 1 ─ 拿到 sessionId ────────────────────────────────────────────┐
│ GET  {portal}/entry?mac={MAC}                                       │
│   302 -> /eportal/identityAuth.jsp?source=gateway&from=client&mac=… │
│   302 -> /portal/portal-main?sessionId=…&userIp=…&nasIp=…           │
│              &customPageId=…&flowKey=…&userMac=…                    │
└─────────────────────────────────────────────────────────────────────┘
┌─ 步骤 2 ─ 推进工作流（部分环境可省）───────────────────────────────┐
│ POST {portal}/eportal/workFlow/getCurrentNode                       │
│      {"sessionId": "…", "flowKey": "identity_portal_then_1x_auth"}  │
└─────────────────────────────────────────────────────────────────────┘
┌─ 步骤 3 ─ 取 CAS 登录页，抠出两个关键值 ────────────────────────────┐
│ GET  {portal}/cas-sso/login?flowSessionId=…&customPageId=…&…        │
│      页面里藏着：                                                   │
│        <p id="login-croypto">…</p>        ← AES 密钥（Base64）      │
│        <p id="login-page-flowkey">…</p>   ← CAS execution 令牌      │
└─────────────────────────────────────────────────────────────────────┘
┌─ 步骤 4 ─ 提交凭据 ────────────────────────────────────────────────┐
│ POST {portal}/cas-sso/login?<和步骤 3 完全相同的查询串>             │
│ Content-Type: application/x-www-form-urlencoded                     │
│                                                                     │
│ username        = 学号                                              │
│ password        = Base64( AES-128-ECB/PKCS7(密码, key) )            │
│ croypto         = <步骤 3 抠出来的 Base64 密钥>                     │
│ captcha_payload = Base64( AES-128-ECB/PKCS7("{}", key) )            │
│ execution       = <步骤 3 抠出来的 flowkey>                         │
│ type=UsernamePassword  _eventId=submit  geolocation=                │
└─────────────────────────────────────────────────────────────────────┘
```

成功时返回 **302**（跳到门户的成功页）；失败时返回 **401** 并在页面里带上错误文案。

### 2.3 密码加密到底怎么算

门户前端的代码是（`/sso-public/cas-login-new/main.js`）：

```js
const key = document.getElementById("login-croypto").innerText;
input.password.value = Bn.aesEncrypt(key, this.password);
input.captcha_payload.value = Bn.aesEncrypt(key, JSON.stringify(this.captcha_payload));

// …
static aesEncrypt(r, t) {
  const o = te.enc.Base64.parse(r);                       // 密钥 = Base64 解码
  return te.AES.encrypt(t, o, {mode: te.mode.ECB,         // AES-128-ECB
                               padding: te.pad.Pkcs7})     // PKCS#7
           .toString();                                    // 输出 Base64
}
```

也就是说：**AES-128-ECB + PKCS#7，密钥是 Base64 解码后的 16 字节，输出再 Base64**。

本仓库用纯 Python 复刻了这个算法（`campusnet/aes.py`），并且用两种方式验证：

1. **FIPS-197 C.1 标准测试向量** —— 确认 AES 本体没错；
2. **一份真实抓包** —— 浏览器把 `zzprobe0001` 加密后发出的密文是
   `Uz/UJgU3BdOiWO9Bfrykrg==`，密钥是 `Qvy2L55c2aTCHUc6uX/Llw==`。
   我们的实现在这两个输入上产出**完全相同的密文**。

```bash
$ python tools/verify_algorithms.py
[PASS] AES-128(key=000102..0f, pt=001122..ff)
[PASS] AES-ECB-PKCS7('zzprobe0001', key=Qvy2L55c2aTCHUc6uX/Llw==)
[PASS] 13 组随机用例全部一致
```

### 2.4 端到端验证

用**假账号**把整条链路跑一遍，如果服务端说"用户不存在或者密码错误"，
就说明除了凭据之外的每一个字节都是对的：

```
$ python campus_login.py --config test.json login
INFO    步骤1 完成: sessionId=e66015b812b8 userIp=10.18.0.100 nasIp=1.1.1.1
INFO    步骤3 完成: croypto=YnHmFS0k… execution=0807e9f8…
INFO    步骤4 提交结果: HTTP 401
失败: 认证被拒绝: 用户不存在或者密码错误!
```

服务端**成功解密了我们的密码密文并做了比对** —— 这正是链路正确的证明。

### 2.5 怎么把它挪到别的学校

这套锐捷 SAM 5.x 在很多学校都在用，差异一般只在：

- `options.mac`：一般 `auto` 就行
- `entry_path` / `cas_prefix`：如果页面结构不同再改

**`options.portal` 和 `options.nasip` 通常不用填。** 程序在没认证（被门户劫持）
的状态下会先碰一个外网地址，从 AC 的 302 里拿到真正的入口：

```
GET http://connect.rom.miui.com/generate_204
 -> 302 Location: http://<portal>/portal/entry/pc/authenticate?...
```

门户地址就是那个 `Location` 的 origin，接入设备地址藏在随后跳转的
`sessionId=...&nasIp=...` 里。这两个值都探测不到时才需要手填。

`tools/verify_discover.py` 会在本机起一个假门户，把这条探测链路完整跑一遍
（包括「`portal` / `nasip` 全空也能探测出来」这个断言），不碰外网。

---

## 3. `srun`：深澜 Srun 门户

深澜的链路是公开的（很多开源项目都实现过），本程序按官方接口实现：

```
GET  {portal}/cgi-bin/get_challenge?callback=cb&username=…&ip=…&_=…
     -> cb({"challenge": "<一次性随机串>", "client_ip": "…"})

计算：
  hmd5   = HMAC-MD5(密码, challenge)
  info   = "{SRBX1}" + 自定义Base64( xEncode(JSON, challenge) )
  chksum = HMAC-MD5(challenge + 用户名 + hmd5 + ac_id + ip + n + type + info,
                    challenge)

GET  {portal}/cgi-bin/srun_portal?action=login&username=…&password={hmd5}
     &ac_id=…&ip=…&chksum={chksum}&info={info}&n=200&type=1&…
```

其中 `xEncode` 是一个 XXTEA 变体，配套的 Base64 用的是**自定义字母表**
（`LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA`）。

这两样东西最容易写错，所以本仓库把它们用**纯 Python** 复刻之后，
又用门户自身的 JavaScript 实现（`tools/srun_reference.js`）做了随机对拍：

```
$ python tools/verify_algorithms.py
[PASS] 13 组随机用例全部一致
```

几个容易踩的坑（都已经处理）：

- JS 的位运算是 **Int32**，`>>>` 是逻辑右移，而 `z >> 5` 在 Python 里要保证是无符号的；
- JS 里 `k[i]` 越界得到 `undefined`，而 `undefined ^ z === z`（等价于按 0 处理）；
- JS 字符串是 **UTF-16 code unit** 序列，不能直接拿 UTF-8 字节去打包
  （账号密码里出现中文时就会不一样）。

---

## 4. `ruijie_eportal`：锐捷 ePortal 经典接口

老版本锐捷（4.x 及更早）用的是很直白的接口：

```
GET  {任意被劫持的 http 地址}       # 拿到 Location，取出 queryString
POST {portal}/eportal/InterFace.do?method=login
     userId, password, service, queryString, operatorPwd,
     operatorUserId, validcode, passwordEncrypt=false
     -> {"result": "success" | "fail", "message": "…"}
```

`queryString` 是门户用来绑定本次会话的，所以必须先制造一次被劫持的请求才能拿到。
本程序会自动去碰那些探测地址来捕获它。

---

## 5. 探测流程为什么这样设计

```
        ┌──────────────┐
        │  探测 4 个地址 │
        └──────┬───────┘
               │
      ┌────────┴────────┐
      │                 │
   全部符合预期        有不符合预期
      │                 │
   判定「在线」      判定「被劫持」
   sleep 后重来      调用 provider.login()
                        │
                   ┌────┴────┐
                   │         │
                 成功      失败
                   │         │
              验证能否上网   指数退避后重试
                             (最长 5 分钟一次)
```

失败退避是为了避免把账号给"试"锁了：连续失败时等待时间会按 2 的幂增长，
最多 5 分钟重试一次。登录成功后会先静置一小会儿再重新探测，
避免网关还没生效就误判。

---

## 6. 抓包/逆向用到的方法（复现指南）

想给新学校做适配器，可以照这个套路：

1. **Chrome/Edge 无头 + NetLog**，抓门户页面加载产生的所有请求：
   ```powershell
   msedge.exe --headless=new --user-data-dir=<临时目录> `
              --log-net-log=netlog.json `
              --net-log-capture-mode=IncludeCookiesAndCredentials <门户地址>
   ```
2. **CDP（DevTools Protocol）驱动真实点击**，抓凭据提交请求。
   两个实践要点：
   - 登录表单常常在 **iframe**（微前端）里，`document.querySelectorAll('input')`
     在顶层文档里什么都找不到，要递归进 iframe 找；
   - 提交按钮可能是 Ant Design 的 `disabled` 状态，得先勾选用户协议再点。
3. **静态分析前端 bundle**：下载 `main.js`，搜 `"login"`、`"execution"`、
   `"aesEncrypt"`、`/api/` 等关键字，能直接定位到加密函数和接口表。
4. **用假账号跑一遍**：只要服务端回的是"密码错误"而不是参数错误，
   就说明请求格式对了。这是最省事的验证手段，也不会真的登进去。

---

## 7. 已知限制

- 需要选运营商 / 二次合规弹窗的门户，本程序只完成"认证"这一步，
  后续步骤可能还需要手工处理；
- 少数学校把门户做成了必须走客户端（PPPoE / 专用客户端），那不是网页认证，
  本程序不适用；
- IPv6-only 环境需要在 `options.check_urls` 里换成 IPv6 可达的探测点。
