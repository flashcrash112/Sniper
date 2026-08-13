# Getting started on Windows

You do **not** need a server yet, and you do not need to spend any money to get
through this page. By the end you will have the bot installed, a wallet
created, and it watching real pump.fun launches in practice mode.

Renting a server comes later, when you are ready to actually buy something.

Total time: about 30 minutes, most of it waiting for downloads.

---

## Step 1 — Install WSL (Linux inside Windows)

This bot is built for Linux. Windows has a free, built-in Linux environment
called WSL — it is not a virtual machine you have to manage, it is just a
terminal. Using it now also means your laptop matches the server you will rent
later, so nothing changes when you move over.

Open **PowerShell as Administrator** (press Start, type `powershell`,
right-click → *Run as administrator*) and run:

```powershell
wsl --install
```

**Restart your computer** when it tells you to.

After the restart, an Ubuntu window opens by itself and asks you to create a
username and password. This is a *Linux* username and password, nothing to do
with your Windows login. Pick anything you will remember — you will need the
password for commands starting with `sudo`.

> **The password shows nothing as you type it** — no dots, no stars, no cursor
> movement. That is normal Linux behaviour, not a frozen screen. Type it, press
> Enter, and type it again to confirm.

When it is done you get a prompt like `adria@DESKTOP-1234:~$`. That is Linux,
and you are ready for step 2.

### If you get "virtualization is not enabled"

```
WSL2 is unable to start since virtualization is not enabled on this machine.
Error code: Wsl/InstallDistro/Service/RegisterDistro/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED
```

WSL itself installed fine — it just cannot start its virtual machine yet.
There are two possible causes, and this tells you which:

**Open Task Manager** (Ctrl+Shift+Esc) → **Performance** → **CPU**. Look for
**Virtualization** on the right.

---

**If it says "Virtualization: Enabled"** — you only need one Windows component.
In Administrator PowerShell:

```powershell
wsl --install --no-distribution
```

Restart, then:

```powershell
wsl --install -d Ubuntu
```

---

**If it says "Virtualization: Disabled"** (or the line is missing) — it is
switched off in your computer's firmware. You have to turn it on there; Windows
cannot do it for you.

1. Restart, and as it boots press the setup key repeatedly. It is usually
   **Del** or **F2** — some machines use **F1**, **F10** or **Esc**. The boot
   screen normally says which.
2. Find the setting. It lives under *Advanced*, *CPU Configuration*, *Security*
   or *Overclocking* depending on the maker, and is called one of:
   - **Intel VT-x** / **Intel Virtualization Technology** (Intel)
   - **SVM Mode** / **AMD-V** (AMD)
3. Set it to **Enabled**.
4. Save and exit — usually **F10**.

Then back in Administrator PowerShell:

```powershell
wsl --install --no-distribution
```

restart once more, and:

```powershell
wsl --install -d Ubuntu
```

> **Locked-down or work laptop?** If you cannot get into the firmware, skip WSL
> entirely — see [Running without WSL](#appendix-running-without-wsl) at the
> bottom. It works, it is just less tidy.

> If `wsl --install` says it is not recognised at all, your Windows is too old
> for the one-command install. Update Windows, or follow Microsoft's manual WSL
> install steps, then come back.

From now on, **every command goes in the Ubuntu window**, not PowerShell. To
reopen it later: press Start and type `Ubuntu`.

---

## Steps 2 and 3 — Install everything (one command)

> ### First: make sure you are in Ubuntu, not PowerShell
>
> Look at your prompt. This is the single easiest thing to get wrong.
>
> | Prompt looks like | Which shell | Bot commands work? |
> |---|---|---|
> | `PS C:\Users\you>` | Windows PowerShell | **No** |
> | `you@DESKTOP-1234:~$` | Ubuntu (Linux) | **Yes** |
>
> If you see `PS C:\...`, just type **`wsl`** and press Enter — the prompt
> switches to Linux in the same window. Or open a fresh one: Start → `Ubuntu`.
>
> Pasting Linux commands into PowerShell gives you a wall of red
> `The token '&&' is not a valid statement separator` errors. Nothing is
> broken when that happens — the commands simply did not run. Switch to
> Ubuntu and paste again.

Paste this whole block into the **Ubuntu** window and press Enter. It installs
everything, downloads the bot, and runs the tests to prove it works.

```bash
sudo apt update && sudo apt install -y git && \
cd ~ && \
git clone -b claude/pump-fun-sniper-bot-wunrae https://github.com/flashcrash112/Sniper.git sniper && \
cd sniper && ./scripts/setup.sh
```

It will ask for your Linux password near the start. Then it runs for a few
minutes on its own. You want it to end with **`Setup complete.`**

The script is safe to re-run — if it stops partway, fix whatever it complained
about and run `./scripts/setup.sh` again.

> **If git asks for a username and password:** the repo is private. GitHub no
> longer accepts your account password here — you need a Personal Access Token.
> GitHub → Settings → Developer settings → Personal access tokens → Tokens
> (classic) → *Generate new token*, tick the **repo** box, then paste the token
> where it asks for the password. Your username goes in as normal.

> **Do not move the folder onto your Windows C: drive.** Windows drives are
> mounted without working file permissions, so the wallet file's safety check
> could never pass and the bot would refuse to start. The script checks for
> this and stops you.

### Activating it later

The bot runs inside a virtual environment. **Every time you open a new Ubuntu
window**, run this first:

```bash
cd ~/sniper && source .venv/bin/activate
```

You will see `(.venv)` at the start of your prompt when it is active. If
`sniper` is not found, or you see `No module named sniper`, this is what you
forgot.

Once activated, `sniper` works from any directory — the setup script installs
the package, not just its dependencies, and checks that it runs from outside
its own folder.

---

## Step 4 — Create your wallet

This makes a brand new, empty Solana wallet, encrypted with a password.

```bash
sniper keystore create ./keystore.json --generate
```

It asks for a password, twice. Typing shows nothing on screen — normal.

> Do **not** set `SNIPER_KEYSTORE_PASSWORD` by hand just to skip the prompt.
> Anything you type at the shell is saved to `~/.bash_history` in plain text,
> so your wallet password would sit unencrypted on disk — which defeats having
> an encrypted keystore at all. Let it prompt. (The one place the environment
> variable belongs is a root-owned `0600` file read by systemd on a server,
> which is how [DEPLOY.md](DEPLOY.md) sets it up.)

It prints a **public key** — an address like `C35qGut6EcndFfHcw...`. That is your
new wallet's address. Nothing is in it yet, and that is fine for now.

Two things to know:

- **Save that password somewhere safe.** Lose it and the wallet is gone
  permanently. There is no reset.
- **Use a new wallet, not your existing one.** Only put in money you are
  willing to lose.

---

## Step 5 — Get your Helius key

Go to [helius.dev](https://helius.dev), sign up, and choose the **free** plan.
Find the API key page and copy the key — a long string like
`7f3a1c8e-4b2d-49f1-a6e0-2c9d5b8f1a03`.

Back in Ubuntu:

```bash
export HELIUS_API_KEY=paste-your-key-here
```

---

## Step 6 — Set up the config

```bash
cp config.example.toml config.toml
chmod 600 config.toml
sed -i 's/^backend = .*/backend = "logs"/; s|atlas-mainnet|mainnet|' config.toml
```

That switches the feed from the paid one to the free one. Nothing else needs
changing yet — the target ticker only matters once you are hunting a specific
coin.

To look at or edit the file later: `nano config.toml`. Arrow keys to move,
**Ctrl+O** then **Enter** to save, **Ctrl+X** to quit.

---

## Step 7 — See if it all works

```bash
sniper check
```

This confirms your Helius key works, reads live data from pump.fun, and tells
you whether the feed is delivering launches. Your API key is hidden in the
output, so it is safe to share if you want help reading it.

Then:

```bash
sniper verify-layout
```

This checks the bot's buy code against real pump.fun purchases happening on
Solana right now. You want it to say **PASS**.

Then watch it work, spending nothing:

```bash
sniper run --dry-run
```

You will see live launches streaming past. Press **Ctrl+C** to stop.

---

## Where you are now

Installed, wallet created, connected to Solana, and able to watch launches in
real time — for free, with no server and no money at risk.

**What is still missing before a real buy:**

1. SOL in the wallet.
2. A target — the ticker and Twitter of the coin you are waiting for, set in
   `config.toml`.
3. A server, if you want a realistic chance of winning a contested launch.
   Your laptop can technically do it, but home wifi latency, sleep mode and
   ISP routing will lose you the race.

See [DEPLOY.md](DEPLOY.md) when you get to the server step — everything you
learned here transfers directly, because the server runs the same Ubuntu.

And read the
[Ticker-only matches are dangerous](README.md#ticker-only-matches-are-dangerous)
section of the README before you put real money behind it.

---

## If something breaks

| What you see | What it means |
|---|---|
| `The token '&&' is not a valid statement separator` | You are in PowerShell, not Ubuntu. Type `wsl` and press Enter, then paste again |
| `command not found: python` | The venv is not active. Run `cd ~/sniper && source .venv/bin/activate` |
| `needs Python 3.11 or newer` | See step 2 — install `python3.12` and rebuild the venv |
| `accessible to other users` | The bot is on `/mnt/c`. Move it to `~` (see step 3) |
| `references ${HELIUS_API_KEY} but that environment variable is not set` | Re-run the `export HELIUS_API_KEY=...` line; it resets when you close the window |
| `could not decrypt keystore` | Wrong `SNIPER_KEYSTORE_PASSWORD` |
| `PROBLEM — could not subscribe` | Wrong endpoint or key, or the paid feed on a free plan |

Both `export` lines vanish when you close the Ubuntu window. To make them
permanent:

```bash
echo "export HELIUS_API_KEY=your-key-here" >> ~/.bashrc
```

Do **not** do that with the keystore password on a machine other people use.


---

## Appendix: running without WSL

Only use this if you cannot enable virtualization — a locked work laptop, for
example. Everything works, but you are on a slightly different path from the
Linux server you will eventually deploy to, so commands in the other guides
will need translating.

1. Install Python from [python.org](https://python.org) (3.11 or newer). **Tick
   "Add python.exe to PATH"** on the first screen of the installer.
2. Install [Git for Windows](https://git-scm.com/download/win).
3. Open **PowerShell** (normal, not Administrator) and run:

```powershell
cd $HOME
git clone -b claude/pump-fun-sniper-bot-wunrae https://github.com/flashcrash112/Sniper.git sniper
cd sniper
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q
```

> If PowerShell refuses to run the activate script, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

From there follow steps 4-7 of the main guide, with these differences:

| Main guide (Linux) | Windows equivalent |
|---|---|
| `export NAME=value` | `$env:NAME = "value"` |
| `source .venv/bin/activate` | `.\.venv\Scripts\Activate.ps1` |
| `nano config.toml` | `notepad config.toml` |
| `cp config.example.toml config.toml` | `copy config.example.toml config.toml` |
| `chmod 600 config.toml` | not needed — see below |

**One real difference to understand.** On Linux the bot refuses to load your
wallet file if other users can read it. Windows does not have those permissions,
so that check cannot run and is skipped — you will see a
`keystore_permissions_unchecked` warning at startup. Nothing is broken, but the
protection is not there either, so keep `keystore.json` out of OneDrive,
Dropbox, or any shared folder.
