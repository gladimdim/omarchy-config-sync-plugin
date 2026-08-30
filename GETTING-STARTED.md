# First-time setup

This walkthrough takes you from a fresh Omarchy laptop to a **private** GitHub repo that holds your desktop config — shortcuts, bar layout, plugins, hooks, and terminal files — so the next machine can apply the same setup in one click.

You do this **once**. After that, the tray icon is Apply / Publish.

---

## Why the repo must be private

The files this plugin syncs are yours, not a theme pack. They can include:

- personal keybindings and menu customizations
- helper scripts under `~/.local/bin`
- automation hooks that run on boot or after updates
- hostnames, display names, and paths that identify you

A **public** GitHub repo is crawled, forked, and searchable. Keep the config repo **Private**. You can still clone it on every laptop you own. Starting private is the safe default; you can open it later if you really want to.

This plugin’s own source (`omarchy-config-sync-plugin`) can stay public. That is not the same repo as your configs.

---

## What you will create

| Repo | Visibility | Purpose |
| --- | --- | --- |
| `omarchy-config` (your name is fine) | **Private** | Your Hyprland + Omarchy files |
| This plugin | Public is fine | The tray app that talks to that private repo |

An empty private repo is enough. You do **not** need to copy files by hand. The plugin seeds it from this laptop.

---

## Step 1 — Create an empty private GitHub repo

1. Sign in to GitHub and open **[github.com/new](https://github.com/new)**.
2. **Repository name:** `omarchy-config` (or any name you will remember).
3. **Visibility:** click **Private**. Do not leave it on Public.
4. **Initialize this repository:** leave README, .gitignore, and license **unchecked**. Empty is ideal. A README-only repo also works.
5. Click **Create repository**.
6. Copy the clone URL from the next page:
   - HTTPS: `https://github.com/<you>/omarchy-config.git`
   - SSH: `git@github.com:<you>/omarchy-config.git`

Keep that URL. You will paste it into the plugin.

---

## Step 2 — Let this laptop talk to GitHub (no password prompts)

The plugin never pops a username/password box. A prompt would freeze the status bar, so Git is told not to ask. Set up credentials **in a terminal first**.

**Easiest (HTTPS):**

```bash
gh auth login
```

Choose GitHub.com → HTTPS → login with a browser. That stores a token git can use.

**Or SSH:** generate a key, add it under GitHub → Settings → SSH and GPG keys, and use the `git@github.com:…` URL.

**Check it works:**

```bash
git ls-remote https://github.com/<you>/omarchy-config.git
```

An empty repo prints nothing and exits 0. An auth error means fix Step 2 before opening the panel.

---

## Step 3 — Install Config Sync (if it is not already on the bar)

```bash
omarchy plugin add https://github.com/gladimdim/omarchy-config-sync-plugin --enable --yes
```

You should see a cloud-sync icon on the right of the Omarchy bar. If it is missing:

```bash
omarchy plugin enable gladimdim.config-sync --section right
omarchy-shell shell rescanPlugins
```

---

## Step 4 — Link the empty repo

1. Click the **cloud-sync** icon.
2. You will see a short starter: *create a private repo, then paste its URL*.
3. Optional: **Open GitHub** opens `github.com/new` if you skipped Step 1.
4. Paste the URL from Step 1 into the field.
5. Click **Connect repo**.

The plugin clones the repo, notices it is empty, and **does not apply anything yet**. The Shortcuts, Plugins, and Configs tabs show **this laptop** — that is what will be uploaded.

If clone fails with an authentication message, go back to Step 2.

---

## Step 5 — Review, then seed with Publish

This is the last “are you sure?” before your desktop layout lives on GitHub.

- **Shortcuts** — bindings from `hypr/bindings.lua`
- **Plugins** — everything under `~/.config/omarchy/plugins/` except this plugin itself
- **Configs** — Hyprland files, `shell.json`, hooks, terminals

**Display layout** (`hypr/monitors.lua`) stays on this machine. A second laptop usually has different screens. Turn on **Include display layout** on the Changes tab only if you mean it.

When it looks right, click **Publish this laptop**. Confirm. The plugin copies those files into the private repo, commits, and pushes.

Your GitHub repo should now contain `hypr/`, `omarchy/`, `plugins/`, and so on — still **private**.

---

## Step 6 — The next laptop

On the new Omarchy machine:

1. Repeat Step 2 (GitHub auth) and Step 3 (install the plugin).
2. Click the icon, paste the **same** private repo URL, Connect.
3. This time the repo is **not** empty. Review incoming shortcuts and plugins.
4. Click **Apply** (not Publish). A timestamped backup is written under `~/.config/omarchy-backup.*` first.

After that, daily life is:

When the badge lights up, **Review Changes** (or press `c`) opens a checklist. You can apply or publish only some shortcuts, only certain plugins, the selected **theme**, or only some config files. Unchecked items stay as they are on that machine.

The selected Omarchy theme (`omarchy theme current`) is part of that list. Stock themes only need the name. If you customized a theme under `~/.config/omarchy/themes/<slug>/`, those overlay files sync too (wallpapers and preview images are skipped so the repo stays small). Apply runs `omarchy theme set` on the other laptop.

Both laptops need the **same Config Sync plugin version** (Overview shows `Plugin: config-sync 1.2.2`). Incoming/outgoing groups, Include checkboxes, and Resync live in that version. Update with `omarchy plugin update gladimdim.config-sync --yes`, or copy `~/.config/omarchy/plugins/gladimdim.config-sync/` from the laptop that already has 1.2.2, then `omarchy restart shell`. Removing the plugin also forgets the linked repo, so a reinstall starts at Connect.

| You did this | Open the icon | Press |
| --- | --- | --- |
| Added a shortcut / plugin on this laptop | Badge: local changes | **Publish** |
| Published from the other laptop | Badge: incoming updates | **Apply** |
| Both machines edited the same file | Changes → Both | Keep local or Take repo, then Apply/Publish |

---

## If something goes wrong

**“Git could not authenticate”**  
The bar will not ask for a password. Run `gh auth login` or fix SSH, then click refresh (right-click the icon, or `r` in the panel).

**The repo was created Public by accident**  
GitHub → the repo → Settings → Danger Zone → Change repository visibility → Private. Do this before the first Publish if you can.

**Connect says it is not an Omarchy config repo**  
You pointed at the wrong git URL (this plugin repo, a random project, a non-empty unrelated tree). Create a **new empty private** repo and paste that URL instead.

**I already have `~/Github/omarchy-config`**  
Use **Use this laptop’s clone** on the setup screen instead of creating a second GitHub repo. The folder must already be a git checkout of your config.

**Secrets**  
Do not put API tokens, `.env` files, or private keys in the synced tree. If a hook needs a secret, read it from a file that lives only on the machine, outside the repo.

---

## Keyboard while you set up

| Key | Action |
| --- | --- |
| Enter in the URL field | Connect |
| `r` | Refresh |
| `p` | Publish (seed or later updates) |
| `a` | Apply (second laptop) |
| `Esc` | Close the panel |
