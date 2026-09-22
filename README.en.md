# campus-net-login · Automatic campus network login

> Log in to the campus network automatically at boot, reconnect when the link drops — no more typing your password every day.
>
> Pure Python standard library, **zero third-party dependencies**, runs on Windows / Linux / macOS.

[中文](README.md) · [How it works](docs/HOW_IT_WORKS.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

---

## What it solves

- Every boot, every wake from sleep, you open a browser and type your credentials again;
- The campus network drops on its own now and then, and you only notice minutes later;
- You want to leave a download running or a remote job going, and one drop kills it all.

This program stays resident in the background and probes the network every few seconds: **as soon as it detects a captive portal hijack, it logs in automatically with your saved credentials**, and writes the whole process to a log.

```
[2026-09-18 12:37:49] INFO    看门狗启动: 每 20s 检查一次网络   (watchdog started: checking the network every 20s)
[2026-09-18 13:02:11] WARNING 检测到断网（第 1 次）: 被门户劫持: http://connect.rom.miui.com/generate_204 -> http://10.254.241.66/portal/...   (offline detected (attempt 1): hijacked by portal)
[2026-09-18 13:02:11] INFO    步骤1 完成: sessionId=e66015b812b8 userIp=10.18.0.100 nasIp=1.1.1.1   (step 1 done)
[2026-09-18 13:02:12] INFO    步骤3 完成: croypto=YnHmFS0k… execution=0807e9f8…   (step 3 done)
[2026-09-18 13:02:12] INFO    步骤4 提交结果: HTTP 302   (step 4 submission result: HTTP 302)
[2026-09-18 13:02:12] INFO    登录成功: 认证成功（已验证可上网）   (login succeeded: authentication accepted, internet access verified)
```

---

## Up and running in 30 seconds

Requires **Python 3.8+** (on Windows, remember to tick *Add Python to PATH* during installation).

```bash
git clone https://github.com/<you>/campus-net-login.git
cd campus-net-login

python campus_login.py init        # interactive setup: pick an auth method, enter credentials
python campus_login.py login       # try one login right now
python campus_login.py watch       # run in the foreground and check the log looks right
```

Once that works, install it to start at login:

```powershell
# Windows (no admin needed, an ordinary PowerShell window is fine)
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

```bash
# Linux
./scripts/install-linux.sh
sudo loginctl enable-linger "$USER"     # optional: keep running even when you are not logged in

# macOS
./scripts/install-macos.sh
```

Uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
```
```bash
./scripts/install-linux.sh --uninstall   # or ./scripts/install-macos.sh --uninstall
```

---

## Supported campuses / authentication systems

| provider | Authentication system | Status |
|---|---|---|
| `ruijie_sam_cas` | Ruijie SAM / ePortal 5.x + CAS single sign-on | ✅ **verified end-to-end against a real portal** (Chengdu University of Information Technology) |
| `srun` | Srun portal (`srun_portal`) | ✅ crypto is **byte-for-byte cross-checked against the portal's own JavaScript**; the HTTP flow follows the official interface |
| `ruijie_eportal` | Ruijie ePortal classic `InterFace.do` interface | ⚠️ implemented from the public interface, not yet verified against a real deployment |
| `generic_form` | Any plain HTML form | ⚠️ you must fill in the form fields yourself (see below) |

> What "verified" means here: the `ruijie_sam_cas` path was driven against a real portal —
> session setup, workflow, CAS page parsing, AES encryption and form encoding are all correct,
> and the server replied "user does not exist or wrong password" rather than any parameter error.
> See [How it works](docs/HOW_IT_WORKS.md) for details.

### Which one am I?

With the network down, open any http site and see where the browser lands:

| What you see | Which provider |
|---|---|
| Redirected to an Angular page like `http://10.x.x.x/portal/entry/pc/authenticate` | `ruijie_sam_cas` |
| Redirected to `http://x.x.x.x/eportal/InterFace.do?method=pageInfo...` | `ruijie_eportal` |
| Redirected to `http://x.x.x.x/srun_portal_pc?ac_id=1&...` or `cgi-bin/srun_portal` | `srun` |
| Some other ordinary login page | `generic_form` |

If you are not sure, run `python campus_login.py doctor` — it prints the redirect target it detected.

### No adapter for your campus? Use `generic_form`

Hit F12 → Network, open the login page, log in once, and copy the login request into your config:

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

`{username}` / `{password}` are substituted automatically. If the portal encrypts the password client-side, write a small adapter following the AES approach in `ruijie_sam_cas` (PRs welcome!).

---

## Configuration

`config.json` lives in these locations by default:

| System | Location |
|---|---|
| Windows | `%APPDATA%\campus-net-login\config.json` |
| Linux | `~/.config/campus-net-login/config.json` |
| macOS | `~/Library/Application Support/campus-net-login/config.json` |

The program also prefers a `config.json` in the **current directory**, which makes portable use easy.
You can also point at one explicitly with `-c`: `python campus_login.py -c D:\my.json watch`.

See [`config.example.json`](config.example.json) for a complete example.

| Field | Meaning |
|---|---|
| `provider` | Which adapter to use |
| `username` / `password` | Account; the password is written by `set-password` or the "Change password" button (stored as DPAPI ciphertext on Windows — a hand-written plaintext value works too and is re-encrypted on the next save) |
| `options.portal` | Authentication server address. **May be left empty** — it is auto-discovered from the portal redirect while you are offline |
| `options.nasip` | NAS/AC (access device) address. **May be left empty** — also auto-discovered, then written back to the config |
| `options.mac` | `auto` takes the MAC of the default NIC; if it picks the wrong one, set 12 hex digits by hand |
| `watch.interval` | Seconds between network probes (default 20) |
| `watch.check_min_ok` | How many probe targets must respond to count as online (raise to 2 for a more conservative check on flaky networks) |
| `logging.level` | `DEBUG` shows the details of every request |

> **Don't know `portal` / `nasip`? Leave them empty.** They are internal campus
> addresses that ordinary users have no reason to know. Run the program while you
> are **not authenticated** (i.e. the portal is hijacking you) and it will probe
> the network, read both addresses out of the campus equipment's redirect, and
> remember `nasip` for next time. Wizard page 2 has an "auto-detect" button, and
> skipping the whole page with "Next" works just as well.

---

## Common commands

```bash
python campus_login.py init             # generate a config interactively (--preset cuit applies a preset directly)
python campus_login.py set-password     # save / change the password
python campus_login.py login            # log in once (--force to re-authenticate)
python campus_login.py watch            # resident watchdog (--once probes a single time, for scheduled tasks)
python campus_login.py status           # show current network and config
python campus_login.py doctor           # diagnostics: probe targets, redirects, config validation
python campus_login.py selftest         # algorithm self-test
python campus_login.py providers        # list adapters
```

---

## Security notes

- **How the password is stored**: on Windows it is encrypted with DPAPI (`CryptProtectData`) before being written to `config.json`.
  The ciphertext can only be decrypted by **the same user on the same machine**; copy it to another computer or another account and it will not decrypt.
  On Linux / macOS it falls back to plaintext, but the config file permissions are set to `0600`.
- **Does the password cross the network in the clear?** Both `ruijie_sam_cas` and `srun` perform the same client-side encryption the portal itself uses,
  matching browser behaviour. **But campus portals are mostly plain HTTP**, so a man-in-the-middle can in theory still observe the
  interaction before encryption. That is a design decision on the campus side, not something this program can change.
- **Never commit `config.json`**: `.gitignore` already excludes it. If you really want to share a config, use
  `config.example.json`.
- This program only "logs your own account into your own network". It does not modify any system files, install drivers,
  or hijack traffic.

---

## Project layout

```
campus-net-login/
├── campus_login.py              # CLI entry point
├── campusnet/
│   ├── aes.py                   # pure-Python AES-128-ECB (CryptoJS-compatible)
│   ├── httpx.py                 # urllib wrapper: cookies, redirect control, URL sanitising
│   ├── netutil.py               # online probes / captive-portal detection / local IP and MAC
│   ├── secret.py                # password storage (Windows DPAPI)
│   ├── config.py                # config read/write and path discovery
│   ├── runner.py                # watchdog, logging, single-instance lock
│   └── providers/               # adapters for each authentication system
│       ├── ruijie_sam_cas.py    #   Ruijie SAM 5.x + CAS   ← verified
│       ├── srun.py              #   Srun                  ← crypto cross-checked
│       ├── ruijie_eportal.py    #   Ruijie ePortal classic
│       └── generic_form.py      #   generic form
├── scripts/                     # autostart (Windows scheduled task / systemd / launchd)
├── tools/
│   ├── srun_reference.js        # JS reference implementation of the Srun algorithm (for cross-checking)
│   ├── verify_algorithms.py     # run every algorithm self-test in one go
│   ├── verify_discover.py       # local fake portal: proves portal/nasip auto-discovery works
│   ├── verify_reconnect.py      # unplug/replug simulation: proves reconnect takes seconds
│   └── verify_broken_permissions.py  # break the config ACL, prove the program recovers
└── docs/
```

### Verify the algorithms yourself

```bash
python tools/verify_algorithms.py
```

It checks that AES matches the FIPS-197 standard vectors, that the CryptoJS-compatible mode can reproduce a
**ciphertext captured from a real portal**, and that Srun's xEncode matches the portal's own JavaScript exactly.

### Verify that you really don't need to fill in the server address

```bash
python tools/verify_discover.py
```

This spins up a **fake campus portal on localhost**, replays the real chain
(hijacked by the AC → redirected to the portal → portal hands back a session and the real access-device
address), and then asserts that with `portal` and `nasip` **both empty** the program does discover both
addresses and never writes anything into your real config. No external network involved.

---

## FAQ

**Q: Another machine says `[Errno 13] Permission denied: ...\campus-net-login\config.json`.**
That is a trap left by an early build: after saving it ran
`icacls /inheritance:r /grant:r "%USERNAME%":F` to tighten permissions. On domain accounts,
Microsoft accounts or non-ASCII usernames, `%USERNAME%` does not always resolve to the account
that is actually running — the inherited ACEs are removed first, the new grant lands on somebody
else, and **the owner ends up locked out of their own config**.

Current builds:

- never use `/inheritance:r`; they only *add* grants for the current user + SYSTEM + Administrators,
  so self-lockout is impossible;
- if reading the config fails they **repair the ACL first** (the owner always has WRITE_DAC), so the
  original file and its settings survive;
- if the repair is impossible they move to `%LOCALAPPDATA%\campus-net-login\config.json` and will
  find it again on the next start — it never just gives up.

If a machine is already locked by an old build and you would rather not reinstall, one line in a
Command Prompt fixes it (replace the path with the one from the error):

```
icacls "%APPDATA%\campus-net-login" /grant "%USERNAME%":(OI)(CI)F /T
```

**Q: I changed my password / typed it wrong. How do I set it again?**
Click **Change password** on the main panel and type it twice — no need to redo the wizard.
On the command line use `python campus_login.py set-password`.
You can also click **Open config file** and put a plaintext password in `"password": "..."`;
the program accepts it and re-encrypts it the next time it saves.

**Q: I copied the config to another PC and it says the password can't be decrypted.**
That is expected. On Windows the password is DPAPI-sealed, so the ciphertext can only be decrypted by
**the same user on the same machine** — that is exactly what makes it safer than plaintext.
On the new machine just click **Change password** once.

**Q: It says "cannot get MAC", or the MAC is wrong. What now?**
`python campus_login.py status` prints the MAC it picked up. If it is wrong (a VMware virtual adapter, for example),
set it manually in the config:

```json
"options": { "mac": "001122334455" }
```

**Q: `login` reports success, but I still have no internet.**
Some campuses require you to pick an ISP / service after logging in, or show a compliance popup. Log in by hand once to see whether there are extra steps,
then run `python campus_login.py doctor` and attach the output to an issue.

**Q: How long after unplugging and replugging the cable does it reconnect?**
A few seconds. The watchdog wakes every 20 s, but the **link state is checked locally**
(reading the NIC's OperStatus — not a single packet), so it re-authenticates the moment the
link returns instead of waiting out the current cycle. It used to probe every endpoint with a
full timeout while the cable was out — a 20 s dead window in which people would open a browser
and conclude that "it only connects when the campus login page pops up".

**Q: Can the resident watchdog and the 5-minute fallback task log in at the same time?**
No. `watch` uses a file lock plus a heartbeat, so only one of them ever re-authenticates; the
fallback skips while the resident process is alive and only takes over once its heartbeat has
been stale for 3 minutes.

**Q: The network is not up yet at boot — will it fail?**
No. The watchdog probes in a loop and logs in as soon as the link comes up. The scheduled task is also configured to restart on failure.

**Q: Will two processes fight each other?**
No. `watch` uses a file lock to guarantee a single resident instance; the `--once` call in the scheduled task is an independent fallback that
only acts when it detects the network is down.

**Q: How do I stop it completely?**
Windows: `... install-windows.ps1 -Uninstall`; or end `pythonw.exe` from Task Manager.

More in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

---

## Disclaimer

This project is intended only for **individuals automating their own network login on their own devices**.
Please follow your campus's network usage rules. The author accepts no responsibility for any misuse or its consequences.

---

## Contributing

PRs are welcome, especially **new authentication adapters** and **campus presets**:

1. Fork, then create `campusnet/providers/your_name.py`, subclass `Provider`,
   and follow the style of `ruijie_sam_cas.py`;
2. Register it in `_BUILTINS` in `campusnet/providers/__init__.py`;
3. Add a campus preset to `PRESETS` in `campusnet/presets.py` (optional — and only if
   the addresses really are needed, see the note above about leaving them empty);
4. Run `python tools/verify_algorithms.py`, `python tools/verify_discover.py`
   and `python campus_login.py selftest`.

**Never include real account names, passwords or internal addresses in a PR.**

## License

[MIT](LICENSE)
