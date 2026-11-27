# Running cmsmcp on lxplus

cmsmcp runs on lxplus (RHEL 9) without code changes, but a few AFS and lxplus details need configuration. This guide installs everything under `~/private/cmsmcp`. Checked on lxplus9 (RHEL 9.9) in November 2026.

## What is different from a laptop

| Topic | On lxplus | What to do |
| --- | --- | --- |
| Python | The system `python3.12` has no development headers, so the `gssapi` wheel needed by `cmsmcp login` does not build. | Use a uv-managed Python 3.12. |
| Secrets on AFS | AFS ignores Unix mode bits for other users; only directory ACLs protect files. `~/private` has no `system:anyuser` access. | Put the config (secrets) and state (SSO cookies) directories under `~/private`. |
| lxplus SSH tools | The lxplus tools keep an SSH ControlMaster socket in the state directory, and AFS refuses Unix sockets (`Operation not permitted`). You are already on lxplus, so these tools add nothing. | Leave `CMSMCP_LXPLUS_USER` unset. |
| Browser | No local display. Browser sign-in would need X11 forwarding. | Sign in with `cmsmcp login` (Kerberos ticket + 2FA code). Install only Playwright's headless Chromium. |
| Grid proxy | The default `/tmp/x509up_u<uid>` exists only on the node where it was made. | Write the proxy to AFS (`-out ~/x509up_u$(id -u)`) and point cmsmcp at it. |
| `dasgoclient` | Lives in `/cvmfs/cms.cern.ch/common`, which may not be on the `PATH` an MCP client starts the server with. | Add it to `PATH` in the wrapper. |

## Install

About 0.6 GB in your home volume (environment, uv's Python, headless Chromium).

```bash
B=~/private/cmsmcp
mkdir -p "$B" && cd "$B"
git clone https://github.com/rtschmitz/cmsmcp app
python3 -m pip install --user uv          # puts uv in ~/.local/bin

export UV_PYTHON_INSTALL_DIR=$B/uv-python UV_LINK_MODE=copy UV_CACHE_DIR=/tmp/$USER/uv-cache
cd app
~/.local/bin/uv sync --no-dev --extra kerberos --python 3.12 --python-preference only-managed
PLAYWRIGHT_BROWSERS_PATH=$B/pw .venv/bin/playwright install --only-shell chromium
mkdir -p -m 700 "$B/config" "$B/state"
```

`UV_CACHE_DIR` on local `/tmp` keeps uv's download cache off AFS; `UV_LINK_MODE=copy` avoids hard links across filesystems.

## Wrapper

Save as `~/private/cmsmcp/cmsmcp` and `chmod 700` it. Use it both as the MCP server command and for every `cmsmcp` command, so all of them see the same settings.

```bash
#!/bin/bash
# cmsmcp with lxplus/AFS settings
B=$HOME/private/cmsmcp
export CMSMCP_CONFIG_DIR=$B/config        # secrets + cmsmcp.env
export CMSMCP_STATE_DIR=$B/state          # SSO cookies, audit log, downloads
export PLAYWRIGHT_BROWSERS_PATH=$B/pw     # headless Chromium
unset CMSMCP_LXPLUS_USER                  # lxplus SSH tools: no Unix sockets on AFS
export X509_USER_PROXY=$HOME/x509up_u$(id -u)   # grid proxy on AFS
export CMSMCP_GRID_CERT_FILE=$X509_USER_PROXY
export CMSMCP_GRID_KEY_FILE=$X509_USER_PROXY
case ":$PATH:" in *:/cvmfs/cms.cern.ch/common:*) ;; *) PATH=$PATH:/cvmfs/cms.cern.ch/common ;; esac
exec "$B/app/.venv/bin/cmsmcp" "$@"
```

## Register with your MCP client

Use the wrapper's absolute path as the stdio command:

```json
{"mcpServers": {"cmsmcp": {"command": "/afs/cern.ch/user/<u>/<user>/private/cmsmcp/cmsmcp"}}}
```

In Claude Code: `claude mcp add -s user cmsmcp -- ~/private/cmsmcp/cmsmcp`. Client configuration in your AFS home applies on every lxplus node.

## Sign in

```bash
~/private/cmsmcp/cmsmcp login
```

Your lxplus Kerberos ticket replaces the password; you type the 2FA code from your authenticator app. The session is stored under `~/private/cmsmcp/state`, so it works on every lxplus node, and lasts up to 24 hours (`CMSMCP_SESSION_MAX_AGE_SECONDS`). Run the command again when tools answer `AUTH_REQUIRED`.

- **Security key or passkey as default 2FA:** the login switches to "Try Another Way" → "Authenticator Application" by itself. The account still needs an authenticator app registered; terminal login cannot use a security key or passkey.
- **Several authenticator apps registered:** CERN checks the code against one selected app only, so the login lists your apps and asks which one the code comes from. Without a terminal, pass `--device <name>` (or set `CMSMCP_OTP_DEVICE`) together with `--otp <code>`.

## Grid proxy and tokens

```bash
voms-proxy-init -voms cms -valid 192:00 -out ~/x509up_u$(id -u)   # DAS, DQM, Rucio
~/private/cmsmcp/cmsmcp setup gitlab    # internal/private GitLab projects, issues, code search
~/private/cmsmcp/cmsmcp setup indico    # protected Indico events and categories
~/private/cmsmcp/cmsmcp doctor          # live check of every connection and token
```

Tokens are saved in `~/private/cmsmcp/config/secrets/`.

## Update

```bash
B=~/private/cmsmcp
export UV_PYTHON_INSTALL_DIR=$B/uv-python UV_LINK_MODE=copy UV_CACHE_DIR=/tmp/$USER/uv-cache
git -C "$B/app" pull
cd "$B/app" && ~/.local/bin/uv sync --no-dev --extra kerberos --python 3.12 --python-preference only-managed
```

If a Playwright upgrade asks for a newer browser, rerun the `playwright install --only-shell chromium` line from the install step.
