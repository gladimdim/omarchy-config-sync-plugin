# Omarchy Config Sync (`gladimdim.config-sync`)

A status-bar plugin that keeps [Omarchy](https://omarchy.org/) configs in a **private** git repo — the same layout as [`omarchy-config`](https://github.com/gladimdim/omarchy-config) — in sync across machines.

**New here?** Follow the [first-time setup guide](GETTING-STARTED.md): create an empty **private** GitHub repo, paste its URL into the tray icon, review this machine, then **Publish this machine**. Keep that repo private so shortcuts, hooks, and scripts are not public.

Click the cloud-sync tray icon, paste the git URL of your config repo, preview what would land on this machine, then **Apply**. When you add a shortcut or plugin locally, open the panel again and **Publish** to send it to the next machine.

When there is drift, **Review Changes** opens a checklist: incoming vs local **shortcuts** (individual keybindings), **plugins** (whole plugin), the **selected theme**, and other config files. Uncheck anything you do not want. Apply and Publish only touch the checked items. Applying a theme runs `omarchy theme set` on this machine.

## Features

- **Tray icon** in the Omarchy bar, with a badge when this machine and the repo have drifted.
- **Link a repo** by HTTPS, SSH, `owner/repo` shorthand, or a local git path (for example `~/Github/omarchy-config`).
- **Validates** that the clone is really an Omarchy config tree (`hypr/` plus `omarchy/shell.json`, `plugins/`, or `apply.sh`).
- **Preview before apply**: shortcuts from `hypr/bindings.lua`, plugins with versions, bar layout, hooks, helper scripts, and terminal configs.
- **Apply** copies repo → this machine (timestamped backup under `~/.config/omarchy-backup.<timestamp>/`), reloads Hyprland, and rescans the shell. The Config Sync widget itself is preserved in the bar even if the incoming `shell.json` does not list it.
- **Publish** copies this machine → repo, commits, and pushes so the next machine can Apply.
- **Drift detection** on open (and every 10 minutes): local-only edits, incoming remote files, both-changed files, and git merge conflicts.
- **Conflict handling**: per-file Keep local / Take repo for overlapping edits; Keep local / Take incoming for git merge conflicts. Display layout (`hypr/monitors.lua`) stays on this machine unless you opt in.

## Install

```bash
omarchy plugin add https://github.com/gladimdim/omarchy-config-sync-plugin --enable --yes
```

From a local checkout:

```bash
omarchy plugin add /home/$USER/Github/omarchy-config-sync-plugin --enable --yes
```

Place it next to the tray:

```bash
omarchy plugin enable gladimdim.config-sync --section right
```

## First machine vs next machine

| | First machine | Next machine |
| --- | --- | --- |
| GitHub repo | Create **empty + Private** (see [GETTING-STARTED.md](GETTING-STARTED.md)) | Same URL |
| After Connect | Tabs show **this machine** | Tabs show **the repo** |
| Primary button | **Publish this machine** (seed + push) | **Apply** (backup, then copy onto the machine) |

## Daily flow

1. **New machine** — click the icon, paste `https://github.com/<you>/omarchy-config.git` (or the local clone). Review Shortcuts / Plugins / Configs. Press **Apply**.
2. **You changed this machine** — open the panel. A badge and the Changes tab list local edits (new keybinding, plugin, …). Press **Publish** to commit and push.
3. **The other machine published** — a badge appears. Open the panel, review incoming files, press **Apply**.
4. **Both changed the same file** — Changes → Both changed → **Keep local** or **Take repo** per file, then Apply and/or Publish.
5. **Git diverged** (two machines pushed without pulling) — **Pull**, resolve any unmerged paths, then continue.

State lives in `~/.local/share/omarchy-config-sync/` so applying `shell.json` does not unlink the repo. Removing the plugin (`omarchy plugin remove gladimdim.config-sync`) forgets that state: a reinstall starts unlinked. **Unlink** in the panel does the same without deleting the plugin. A clone you pointed at in-place (for example `~/Github/omarchy-config`) is never deleted.

## What is synced

| Repo path | On this machine |
| --- | --- |
| `hypr/*.lua`, `hypr/*.conf` | `~/.config/hypr/` |
| `omarchy/shell.json` | `~/.config/omarchy/shell.json` |
| `omarchy/theme.name` | Selected theme (`omarchy theme set`); custom overlays under `omarchy/themes/<slug>/` (images skipped) |
| `omarchy/{branding,extensions,hooks,agents}/` | same under `~/.config/omarchy/` |
| `plugins/*` | `~/.config/omarchy/plugins/` (skips this plugin and other git-managed checkouts’ `.git`) |
| `bin/*` | `~/.local/bin/` |
| `terminals/alacritty.toml` etc. | matching terminal config files |

`hypr/monitors.lua` is tagged machine-specific and is **not** applied unless you enable **Include display layout**.

## Keyboard

| Key | Action |
| --- | --- |
| Left click | Toggle panel |
| Right click | Refresh + fetch |
| `1`–`5` or `←` `→` | Switch tabs |
| `r` | Refresh |
| `c` | Review Changes (cherry-pick) |
| `a` | Apply selected |
| `p` | Publish selected |
| `Esc` | Close |

## Dependencies

Shipped on Omarchy: `python3`, `git`. SSH or a git credential helper is required to clone/push private GitHub repos. The helper never prompts for a password (that would freeze the bar); it fails with a message instead.

## Development

```bash
python3 -m unittest tests.test_config_sync -v
omarchy plugin validate .
```

Starter guide for end users: [GETTING-STARTED.md](GETTING-STARTED.md).

## License

MIT © 2026 Dmytro Gladkyi
