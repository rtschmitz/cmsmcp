# contrib

Helpers that sit next to cmsmcp: `claude-remote` is standalone, `lxplus/` sets up and runs this checkout on lxplus.

## `claude-remote` — Claude Code on a remote directory

Run Claude Code **on your own machine** against a project that lives on **lxplus (or any SSH host)**, with nothing installed remotely.

```bash
claude-remote user@lxplus.cern.ch:/afs/cern.ch/user/u/user/myproject
```

What it does:

1. Opens **one SSH control master** to the host. You authenticate once (password, Kerberos, 2FA); everything after reuses that connection.
2. **Mounts** the remote path with sshfs at `~/remote/<host>_<path>` over the same connection.
3. Starts `claude` in the mount, with an appended system prompt telling it to **edit files through the mount** and **run every command on the remote** (`ssh -S <socket> <host> 'cd <path> && …'`): builds, tests, git, large greps, anything that needs the remote software stack.
4. When `claude` exits, it **unmounts** and **closes the connection** (only if this run created them, so two sessions on the same host can share one).

Any extra arguments go to `claude` unchanged, e.g. `claude-remote host:/path --continue`.

### Requirements

On your machine (nothing on the remote):

| | Linux | macOS |
| --- | --- | --- |
| `claude` | Claude Code, logged in | Claude Code, logged in |
| `ssh` | preinstalled | preinstalled |
| sshfs + FUSE | `sudo apt install sshfs` (Fedora/RHEL/Alma: `sudo dnf install fuse-sshfs`) | FUSE-T or macFUSE, see below |
| mount tools | `mountpoint`, `fusermount3`/`fusermount` (installed with the above) | `umount`, `diskutil` (built in) |

Windows: use WSL and follow the Linux column.

#### macOS: FUSE and sshfs

macOS has no FUSE of its own and Homebrew core no longer ships sshfs. Pick one:

- **FUSE-T (recommended)**: no kernel extension, so no Recovery-mode or security-settings steps.
  ```bash
  brew install macos-fuse-t/homebrew-cask/fuse-t-sshfs
  ```
- **macFUSE + sshfs**: the classic stack.
  ```bash
  brew install --cask macfuse          # then allow it in System Settings → Privacy & Security
  brew install gromgit/fuse/sshfs-mac  # or the sshfs .pkg from github.com/osxfuse/sshfs/releases
  ```
  Older macFUSE versions use a kernel extension; on Apple Silicon that also means lowering security in Recovery mode once.

The script is written for macOS's own `/bin/bash` (3.2); no Homebrew bash is needed.

> macOS support is new and not yet checked by the maintainer. If something fails, report the error and your FUSE flavour.

### Install

```bash
install -m 755 contrib/claude-remote ~/.local/bin/   # any directory on your PATH
```

Optionally add aliases to `~/.bashrc` / `~/.zshrc`:

```bash
alias claude-lxplus='claude-remote <user>@lxplus.cern.ch:/afs/cern.ch/user/<u>/<user>/<project>'
alias claude-eos='claude-remote <user>@lxplus.cern.ch:/eos/user/<u>/<user>/<project>'
```

### lxplus tips

- `lxplus.cern.ch` is a round-robin alias. The control master pins the session to one node, so the mount and the commands always hit the same machine.
- With a Kerberos ticket (`kinit <user>@CERN.CH`) you skip the password; this `~/.ssh/config` block forwards it so AFS/EOS work in the remote commands:
  ```
  Host lxplus lxplus.cern.ch
      HostName lxplus.cern.ch
      User <user>
      GSSAPIAuthentication yes
      GSSAPIDelegateCredentials yes
  ```
  Then `claude-remote lxplus:/afs/...` works. lxplus may still ask for the 2FA code once, when the master opens.
- The connection lives as long as the session. AFS tokens on lxplus expire (typically after a day); if remote commands start failing with permission errors on a long session, restart `claude-remote`.

### Options

| Variable | Effect |
| --- | --- |
| `CLAUDE_REMOTE_SSHFS_OPTS` | Extra comma-separated sshfs `-o` options, e.g. `noappledouble,volname=lxplus` with macFUSE (stops macOS writing `._*` files to the remote). Options the installed sshfs does not know make the mount fail, so add them one at a time. |

### Things to know

- **Big trees are slow through the mount.** Claude's own search tools read files over sshfs; the appended prompt steers it to run `grep`/`find` remotely, but a nudge ("search on the remote") helps if it forgets.
- **Run git on the remote**, not through the mount: it is much faster there and uses the remote's git config and credentials.
- **Edit alongside Claude**: open the printed mount path in your editor from another terminal.
- **Claude's history is per mount path** (a `~/.claude/projects/` entry named after `~/remote/<host>_<path>`), so `--continue` / `--resume` work across sessions on the same target.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `missing dependency: sshfs` | Install sshfs (see Requirements). |
| `Transport endpoint is not connected` / mount directory unreadable | A session was killed hard and left a dead mount. Linux: `fusermount3 -u ~/remote/<dir>` (or `fusermount -u`). macOS: `umount ~/remote/<dir>` or `diskutil unmount force ~/remote/<dir>`. |
| `ControlSocket … already exists, disabling multiplexing`, or a second password/2FA prompt for the mount | A stale socket from a crashed run: `ssh -S ~/.ssh/claude-remote-<host>.sock -O exit <host>`, or delete the `.sock` file. |
| macOS: mount fails with a FUSE/permission error | macFUSE: allow the extension in System Settings → Privacy & Security and reboot. FUSE-T: reinstall the cask. Also grant your terminal access to network volumes if macOS asks. |
| `unix_listener: path … too long for Unix domain socket` | Your home path plus host name exceeds the socket length limit (104 bytes on macOS). Use a short `~/.ssh/config` alias as HOST. |
| `unknown option` from sshfs | Remove that entry from `CLAUDE_REMOTE_SSHFS_OPTS`. |

## `lxplus/` — cmsmcp on CERN lxplus

`setup` installs cmsmcp from this clone; `cmsmcp-lxplus` runs it. See [CERN lxplus setup](../README.md#cern-lxplus-setup).
