.pragma library

function repoName(url) {
  var raw = String(url || "").replace(/\/+$/, "")
  if (!raw) return "Config repo"
  raw = raw.replace(/\.git$/, "")
  var slash = raw.lastIndexOf("/")
  if (slash !== -1) raw = raw.substring(slash + 1)
  var colon = raw.lastIndexOf(":")
  if (colon !== -1 && raw.indexOf("/") === -1) raw = raw.substring(colon + 1)
  return raw || "Config repo"
}

function stateTitle(state) {
  switch (String(state || "")) {
    case "in-sync": return "In sync"
    case "ready": return "Ready to apply"
    case "empty": return "Linked · ready for first push"
    case "local-ahead": return "Local changes"
    case "remote-ahead": return "Incoming updates"
    case "diverged": return "Both sides changed"
    case "conflicts": return "Merge conflicts"
    case "stale": return "Repo copy is behind"
    case "invalid": return "Not an Omarchy config"
    case "not-configured": return "Not linked"
    default: return "Omarchy Config Sync"
  }
}

function stateHint(state, status) {
  var localN = status && status.local_changes ? Number(status.local_changes) : 0
  var repoN = status && status.repo_changes ? Number(status.repo_changes) : 0
  var bothN = status && status.both_changed ? Number(status.both_changed) : 0
  var differs = status && status.unknown_differs ? Number(status.unknown_differs) : 0
  switch (String(state || "")) {
    case "in-sync":
      return "This machine matches the linked config repo."
    case "empty":
      return "Connected. The repo is empty (or only has a README), so one step is left: seed it from this machine. The tabs show what this machine would push."
    case "ready":
      return "The repo looks like Omarchy config. Review shortcuts, plugins, and files, then Apply to this machine — or Publish if this machine is the source of truth."
    case "local-ahead":
      return localN === 1
        ? "1 local change is not in the repo yet. Publish to share it with your other machines."
        : localN + " local changes are not in the repo yet. Publish to share them with your other machines."
    case "remote-ahead":
      return "The repo has config this machine has not applied. Review the incoming files, then Apply."
    case "diverged":
      return "This machine and the repo both moved. Review Changes to pick a side item by item, or Resync from repo to make this machine match git (usual on a second machine)."
    case "conflicts":
      return "Git could not merge automatically. Keep the local copy or take the incoming copy for each conflicted file."
    case "stale":
      return staleHint(status && status.stale_clone)
    case "invalid":
      return "The linked git repo is missing Hyprland / Omarchy config files."
    default:
      return "Paste the git URL of your omarchy-config repo to get started."
  }
}

// The clone is behind origin and was not moved forward. Say why, and name the
// files that block it, so nobody reads already-pushed files as local edits.
function staleHint(stale) {
  var s = stale || {}
  var n = Number(s.behind || 0)
  var text = "The local copy of the repo is " + n + (n === 1 ? " commit" : " commits") + " behind origin"
  if (s.remote_head) text += " (" + s.remote_head + ")"
  text += ". " + String(s.reason || "")
  var dirty = s.dirty_files || []
  if (dirty.length > 0)
    text += " Uncommitted in the clone: " + dirty.join(", ") + ". Commit or discard them there, then Refresh."
  text += " Files that already match origin are not listed as local changes."
  return text
}

function fileStatusLabel(status, removal) {
  if (removal) {
    // The file is gone from one side. Syncing it deletes, it does not copy.
    switch (String(status || "")) {
      case "local":
      case "added-local": return "Removed here"
      case "repo":
      case "added-repo": return "Removed in repo"
    }
  }
  switch (String(status || "")) {
    case "local": return "Local only"
    case "added-local": return "New on this machine"
    case "repo": return "Incoming"
    case "added-repo": return "New in repo"
    case "both": return "Both changed"
    case "differs": return "Different"
    case "identical": return "In sync"
    case "machine": return "This machine"
    default: return String(status || "")
  }
}

// Join a row's status label to its summary without saying the same thing twice.
// A bundle's summary comes from the backend already opening with its own status
// ("Removed here · 11 files"), so prefixing the label again renders as
// "Removed here · Removed here · 11 files". A loose file's summary does not, and
// still wants the prefix.
function statusPrefix(statusLabel, summary) {
  var st = String(statusLabel || "")
  if (!st) return ""
  var sum = String(summary || "")
  // Nothing to separate the label from: return it bare rather than leaving a
  // dangling "Removed here · " on a row whose summary is empty.
  if (!sum) return st
  if (sum === st || sum.indexOf(st + " ") === 0) return ""
  return st + " · "
}

function filesByStatus(files, statuses) {
  var wanted = {}
  for (var i = 0; i < statuses.length; i++) wanted[statuses[i]] = true
  var out = []
  var list = files || []
  for (var j = 0; j < list.length; j++) {
    if (wanted[list[j].status]) out.push(list[j])
  }
  return out
}

function isBundledPath(path) {
  var p = String(path || "")
  return p.indexOf("plugins/") === 0
    || p.indexOf("omarchy/hooks/") === 0
    || p.indexOf("omarchy/agents/") === 0
    || p.indexOf("omarchy/branding/") === 0
    || p.indexOf("omarchy/extensions/") === 0
    || p.indexOf("bin/") === 0
}

function reviewItem(kind, id, label, summary, status, typeLabel, both, changedCount, hidden, changes) {
  return {
    kind: kind,
    itemId: String(id || ""),
    label: String(label || ""),
    summary: String(summary || ""),
    status: status,
    typeLabel: typeLabel || "",
    both: !!both || String(status) === "both",
    changed_count: changedCount || 0,
    hidden: !!hidden,
    removal: false,
    // A file both sides edited that a three-way merge can combine.
    mergeable: false,
    // Rows with an action (Install/Update) are buttons, not Apply/Publish picks.
    pickable: true,
    action: "",
    changes: changes || [],
    // One line: what Apply or Publish changes, and on which machine.
    effect: "",
    // The unified diff (files) or commit log and diff (git plugins).
    preview: "",
    // Bundle rows load their per-file diffs on demand (bundle-diff).
    lazyDiff: false
  }
}

function withDiff(row, src) {
  row.effect = String((src && src.effect) || "")
  row.preview = String((src && src.preview) || "")
  // Both-sides files: what Keep local, Take repo and Merge each change.
  row.choices = (src && src.choices) ? src.choices : null
  return row
}

// The diff text for one both-sides choice: this machine, then the repo.
function choiceDiffText(choices, side) {
  var c = choices && choices[side]
  if (!c) return ""
  var parts = []
  parts.push("── " + c.label + ": this machine")
  parts.push(c.here || "(no change here)")
  parts.push("── " + c.label + ": the repo")
  parts.push(c.repo || "(no change in the repo)")
  var o = c.overlaps || []
  if (o.length > 0)
    parts.push("── Both machines changed " + o.join(", ") + "; Merge keeps this machine's version there")
  return parts.join("\n")
}

function unbundledFiles(files) {
  var out = []
  var list = files || []
  for (var i = 0; i < list.length; i++) {
    if (!isBundledPath(list[i].path)) out.push(list[i])
  }
  return out
}

function itemsOfKind(items, kind) {
  var out = []
  var list = items || []
  for (var i = 0; i < list.length; i++) {
    if (list[i].kind === kind) out.push(list[i])
  }
  return out
}

function categorizePath(path) {
  var p = String(path || "")
  if (p === "hypr/bindings.lua") return "shortcuts"
  if (p === "hypr/monitors.lua") return "displays"
  if (p.indexOf("hypr/") === 0) return "hyprland"
  if (p === "omarchy/theme.name" || p.indexOf("omarchy/themes/") === 0) return "theme"
  if (p.indexOf("plugins/") === 0) return "plugins"
  if (p === "omarchy/shell.json") return "shell"
  if (p.indexOf("terminals/") === 0) return "terminals"
  if (p.indexOf("omarchy/hooks/") === 0) return "hooks"
  if (p.indexOf("bin/") === 0) return "scripts"
  return "other"
}

function itemCategory(item) {
  if (!item) return "other"
  if (item.kind === "s") return "shortcuts"
  if (item.kind === "t") return "theme"
  if (item.kind === "p" || item.kind === "l") return "plugins"
  if (item.kind === "g") {
    var id = String(item.itemId || "")
    if (id.indexOf("plugin:") === 0) return "plugins"
    if (id.indexOf("hooks:") === 0) return "hooks"
    if (id === "bin") return "scripts"
    return "other"
  }
  return categorizePath(item.itemId || item.path)
}

function itemsForCategory(items, cat) {
  var out = []
  var list = items || []
  for (var i = 0; i < list.length; i++) {
    if (itemCategory(list[i]) === cat) out.push(list[i])
  }
  return out
}

function filesForCategory(files, cat) {
  var out = []
  var list = files || []
  for (var i = 0; i < list.length; i++) {
    if (categorizePath(list[i].path) === cat) out.push(list[i])
  }
  return out
}

function pickedInItems(items, picks) {
  var n = 0
  var list = items || []
  var map = picks || {}
  for (var i = 0; i < list.length; i++) {
    if (list[i].pickable === false) continue
    if (map[list[i].kind + ":" + list[i].itemId]) n++
  }
  return n
}

function isItemHidden(kind, id, hiddenMap, item) {
  if (item && item.hidden) return true
  if (!hiddenMap) return false
  var key = kind + ":" + id
  if (hiddenMap[key] || hiddenMap[id]) return true
  if (kind === "f") {
    var p = String(id || "")
    if (p.indexOf("plugins/") === 0) {
      var pid = p.split("/")[1] || ""
      if (pid && (hiddenMap["g:plugin:" + pid] || hiddenMap["p:" + pid] || hiddenMap["plugin:" + pid])) return true
    }
    if (p.indexOf("omarchy/hooks/") === 0) {
      var parts = p.split("/")
      if (parts.length >= 3) {
        var ev = parts[2].replace(".d", "")
        if (hiddenMap["g:hooks:" + ev] || hiddenMap["hooks:" + ev]) return true
      }
    }
    if (p.indexOf("omarchy/agents/") === 0 && (hiddenMap["g:agents"] || hiddenMap["agents"])) return true
    if (p.indexOf("omarchy/branding/") === 0 && (hiddenMap["g:branding"] || hiddenMap["branding"])) return true
    if (p.indexOf("omarchy/extensions/") === 0 && (hiddenMap["g:extensions"] || hiddenMap["extensions"])) return true
    if (p.indexOf("bin/") === 0 && (hiddenMap["g:bin"] || hiddenMap["bin"])) return true
    if ((p === "omarchy/theme.name" || p.indexOf("omarchy/themes/") === 0) && (hiddenMap["t:selected"] || hiddenMap["t:theme"] || hiddenMap["theme"])) return true
  } else if (kind === "g") {
    var raw = String(id || "")
    if (raw.indexOf("plugin:") === 0) {
      var gpid = raw.substring(7)
      if (hiddenMap["p:" + gpid] || hiddenMap[gpid]) return true
    }
  } else if (kind === "p") {
    if (hiddenMap["g:plugin:" + id] || hiddenMap["plugin:" + id]) return true
  }
  return false
}

function hasVisibleShortcutDiffs(shortcuts, hiddenMap) {
  var list = shortcuts || []
  for (var i = 0; i < list.length; i++) {
    if (isItemHidden("s", list[i].keys, hiddenMap, list[i])) continue
    return true
  }
  return false
}

function appendThemes(out, list, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var t = rows[i]
    var id = t.id || "selected"
    if (isItemHidden("t", id, hiddenMap, t)) continue
    out.push(withDiff(reviewItem("t", id, t.display || t.slug, t.semantic_summary || t.slug, t.status, "Theme", t.status === "both", 0, false, t.changes || []), t))
  }
}

function appendShortcuts(out, list, summaryField, both, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var s = rows[i]
    if (isItemHidden("s", s.keys, hiddenMap, s)) continue
    var sum = (summaryField === "detail" || s.portable === false || s.duplicate_of)
      ? (s.detail || s.skip_reason || s.label || "")
      : (s.label || "")
    var row = reviewItem("s", s.keys, s.keys, sum, s.status, "Shortcut", both || s.status === "both", 0, false)
    // Non-portable binds depend on surrounding Lua declarations and cannot be
    // cherry-picked safely. Keep them visible as an explanation without
    // presenting an Include control that the backend would silently ignore.
    row.pickable = s.portable !== false
    row.removal = !!s.removal
    out.push(row)
  }
}

function bundleEffect(b) {
  var n = Number(b.changed_count || 0)
  var files = n + (n === 1 ? " file" : " files")
  var st = String(b.status || "")
  if (b.removal) {
    if (st === "local" || st === "added-local") return "Publish: the repo loses " + files
    return "Apply: this machine loses " + files
  }
  if (st === "repo" || st === "added-repo") return "Apply: this machine gets " + files + " from the repo"
  if (st === "local" || st === "added-local") return "Publish: the repo gets " + files + " from this machine"
  return files + " differ · open the diff to compare"
}

function appendBundles(out, list, both, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var b = rows[i]
    if (isItemHidden("g", b.id, hiddenMap, b)) continue
    var typeLabel = b.kind === "plugin" ? "Plugin" : "Folder"
    var n = Number(b.changed_count || (b.files ? b.files.length : 0) || 0)
    var sum = b.summary || (n + (n === 1 ? " file" : " files"))
    var bundleRow = reviewItem("g", b.id, b.name || b.plugin_id || b.id, sum, b.status, typeLabel, both || b.status === "both", n, false)
    bundleRow.removal = !!b.removal
    bundleRow.lazyDiff = true
    bundleRow.effect = bundleEffect(b)
    out.push(bundleRow)
  }
}

// Git plugins from plugins.json. Outgoing rows publish list entries; incoming
// rows are never copied and only offer Omarchy's Install/Update flow.
function pluginListItem(p, hidden) {
  var row = reviewItem("l", p.id, p.name || p.id, p.summary || "", p.status, "Plugin", false, 0, hidden)
  row.removal = !!p.removal
  row.action = String(p.action || "")
  // A diverged row is a Publish pick (list this machine's commit) and has a
  // Check out button; it never gets Keep local / Take repo file buttons.
  row.both = false
  row.diverged = !!p.diverged
  row.pickable = p.status === "local" || p.status === "added-local" || row.diverged
  return withDiff(row, p)
}

function appendPluginList(out, list, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var p = rows[i]
    if (isItemHidden("l", p.id, hiddenMap, p)) continue
    out.push(pluginListItem(p, false))
  }
}

function appendLooseFiles(out, files, both, hiddenMap) {
  var rows = files || []
  for (var i = 0; i < rows.length; i++) {
    var f = rows[i]
    var p = String(f.path || "")
    if (!p || isBundledPath(p) || p.indexOf("plugins/gladimdim.config-sync") === 0) continue
    if (isItemHidden("f", p, hiddenMap, f)) continue
    var sum = f.semantic_summary || f.summary || ""
    var fileRow = reviewItem("f", p, p, sum, f.status, "File", both || f.status === "both", 0, false, f.changes || [])
    fileRow.removal = !!f.removal
    fileRow.mergeable = !!f.mergeable
    withDiff(fileRow, f)
    // A bindings.lua whose shortcuts already match keeps its choices; the
    // backend leaves it unticked (default_apply/default_publish false).
    fileRow.shortcutsMatch = !!f.shortcuts_match
    out.push(fileRow)
  }
}

function buildIncomingItems(theme, addedShortcuts, changedShortcuts, bundles, files, allFiles, hiddenMap, pluginList) {
  var out = []
  appendThemes(out, theme, hiddenMap)
  appendShortcuts(out, addedShortcuts, "label", false, hiddenMap)
  appendShortcuts(out, changedShortcuts, "detail", false, hiddenMap)
  appendBundles(out, bundles, false, hiddenMap)
  appendPluginList(out, pluginList, hiddenMap)
  appendLooseFiles(out, files, false, hiddenMap)
  return out
}

function buildOutgoingItems(theme, addedShortcuts, changedShortcuts, bundles, files, allFiles, hiddenMap, pluginList) {
  var out = []
  appendThemes(out, theme, hiddenMap)
  appendShortcuts(out, addedShortcuts, "label", false, hiddenMap)
  appendShortcuts(out, changedShortcuts, "detail", false, hiddenMap)
  appendBundles(out, bundles, false, hiddenMap)
  appendPluginList(out, pluginList, hiddenMap)
  appendLooseFiles(out, files, false, hiddenMap)
  return out
}

function buildBothItems(theme, shortcuts, bundles, files, allFiles, hiddenMap, pluginList) {
  var out = []
  appendThemes(out, theme, hiddenMap)
  appendShortcuts(out, shortcuts, "label", true, hiddenMap)
  appendBundles(out, bundles, true, hiddenMap)
  appendPluginList(out, pluginList, hiddenMap)
  appendLooseFiles(out, files, true, hiddenMap)
  return out
}

function buildHiddenItems(theme, shortcuts, bundles, files, allFiles, hiddenMap, pluginList) {
  var out = []
  var seen = {}
  function addHidden(kind, id, label, summary, status, typeLabel, both, count, removal) {
    var key = kind + ":" + id
    if (seen[key]) return
    seen[key] = true
    var row = reviewItem(kind, id, label, summary, status, typeLabel, both, count, true)
    row.removal = !!removal
    out.push(row)
  }

  var tList = theme || []
  for (var ti = 0; ti < tList.length; ti++) {
    var t = tList[ti]
    var tid = t.id || "selected"
    if (isItemHidden("t", tid, hiddenMap, t)) {
      addHidden("t", tid, t.display || t.slug, t.slug, t.status, "Theme", t.status === "both", 0)
    }
  }

  var sList = shortcuts || []
  for (var si = 0; si < sList.length; si++) {
    var s = sList[si]
    if (isItemHidden("s", s.keys, hiddenMap, s)) {
      addHidden("s", s.keys, s.keys, s.label || s.detail || "", s.status, "Shortcut", s.status === "both", 0, s.removal)
    }
  }

  var bList = bundles || []
  for (var bi = 0; bi < bList.length; bi++) {
    var b = bList[bi]
    if (isItemHidden("g", b.id, hiddenMap, b)) {
      var typeLabel = b.kind === "plugin" ? "Plugin" : "Folder"
      var n = Number(b.changed_count || (b.files ? b.files.length : 0) || 0)
      var sum = b.summary || (n + (n === 1 ? " file" : " files"))
      addHidden("g", b.id, b.name || b.plugin_id || b.id, sum, b.status, typeLabel, b.status === "both", n, b.removal)
    }
  }

  var lList = pluginList || []
  for (var li = 0; li < lList.length; li++) {
    var lp = lList[li]
    var lkey = "l:" + lp.id
    if (seen[lkey] || !isItemHidden("l", lp.id, hiddenMap, lp)) continue
    seen[lkey] = true
    out.push(pluginListItem(lp, true))
  }

  var fList = files || []
  for (var fi = 0; fi < fList.length; fi++) {
    var f = fList[fi]
    var p = String(f.path || "")
    if (!p || f.status === "identical" || f.status === "machine") continue
    if (p.indexOf("plugins/gladimdim.config-sync") === 0) continue
    if (isItemHidden("f", p, hiddenMap, f)) {
      var parentHidden = false
      if (p.indexOf("plugins/") === 0) {
        var pid = p.split("/")[1] || ""
        if (pid && (isItemHidden("g", "plugin:" + pid, hiddenMap, null) || isItemHidden("p", pid, hiddenMap, null)))
          parentHidden = true
      }
      if (!parentHidden) {
        addHidden("f", p, p, f.summary || "", f.status, "File", f.status === "both", 0, f.removal)
      }
    }
  }

  return out
}

function countBy(files, statuses) {
  return filesByStatus(files, statuses).length
}

function relativeAgo(iso) {
  if (!iso) return "never"
  var then = Date.parse(iso)
  if (!isFinite(then)) return iso
  var seconds = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (seconds < 60) return "just now"
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago"
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago"
  return Math.floor(seconds / 86400) + "d ago"
}

function pluginActionLabel(action) {
  if (action === "update") return "Update"
  if (action === "checkout") return "Check out"
  if (action === "reinstall") return "Reinstall"
  return "Install"
}

function pluginActionIcon(action) {
  if (action === "update") return "󰚰"
  if (action === "checkout") return "󰘬"
  if (action === "reinstall") return "󰑐"
  return "󰏗"
}

function pluginActionTip(action) {
  if (action === "update") return "Open Omarchy's plugin updater in a terminal"
  if (action === "checkout") return "Check out the repo's commit on a new branch here; the current branch stays"
  if (action === "reinstall") return "Move this plain copy to a backup and install it from git in a terminal"
  return "Open Omarchy's plugin installer in a terminal"
}

// Each diff line with what it is, for coloring. Header lines (---, +++,
// diff --git, index) only count before a file's first @@: after that a
// removed "-- comment" line is content, not a header. A line removed in one
// place and added in another (same text) is "moved", so the real
// differences stand out.
function diffLines(text) {
  var lines = String(text || "").split("\n")
  var out = []
  var inHeader = true
  var section = ""
  var removed = {}, added = {}
  var i, line, kind, body
  for (i = 0; i < lines.length; i++) {
    line = lines[i]
    if (line.indexOf("diff --git") === 0 || line.indexOf("── ") === 0 || line.indexOf("Diff from") === 0) { inHeader = true; section = "diff"; kind = "head" }
    else if (line.indexOf("Commits (") === 0) { inHeader = false; section = "log"; kind = "head" }
    else if (inHeader && (line.indexOf("--- ") === 0 || line.indexOf("+++ ") === 0 || line.indexOf("index ") === 0 || line.indexOf("new file") === 0 || line.indexOf("deleted file") === 0)) kind = "head"
    else if (line.indexOf("@@") === 0) { inHeader = false; section = "diff"; kind = "hunk" }
    else if (section === "log" && line.indexOf("> ") === 0) kind = "add"
    else if (section === "log" && line.indexOf("< ") === 0) kind = "remove"
    else if (!inHeader && line.charAt(0) === "+") kind = "add"
    else if (!inHeader && line.charAt(0) === "-") kind = "remove"
    else kind = "same"
    if (section !== "log" && (kind === "add" || kind === "remove")) {
      body = line.substring(1).trim()
      if (body) {
        var bag = kind === "add" ? added : removed
        bag[body] = (bag[body] || 0) + 1
      }
    }
    out.push({ text: line, kind: kind, section: section })
  }
  for (i = 0; i < out.length; i++) {
    var o = out[i]
    if (o.section === "log" || (o.kind !== "add" && o.kind !== "remove")) continue
    body = o.text.substring(1).trim()
    if (!body) continue
    var other = o.kind === "add" ? removed : added
    if (other[body] > 0) o.kind = "moved"
  }
  return out
}

// What the colors mean for this diff, from its own --- / +++ names. A choice
// preview compares now with after; a plain row compares two machines.
function diffSide(name) {
  var n = String(name || "").trim()
  if (n === "this machine" || n.indexOf("a/") === 0) return "on this machine"
  if (n === "repo") return "in the repo"
  if (n.indexOf("b/") === 0) return "in the listed commit"
  return "in " + n
}

function diffLegend(text) {
  var t = String(text || "")
  var moved = " · ↕ same line, only moved"
  if (t.indexOf("── ") === 0 && t.indexOf(" after ") >= 0)
    return "+ green: after the choice · − red: before it" + moved
  var from = t.match(/^--- (.+)$/m), to = t.match(/^\+\+\+ (.+)$/m)
  if (from && to) return "+ green: only " + diffSide(to[1]) + " · − red: only " + diffSide(from[1]) + moved
  return "+ green: added · − red: removed" + moved
}

// The active theme's colors.toml as { name: "#rrggbb" }. Every stock Omarchy
// theme names a green and a red there; the shell only exposes red (as urgent).
function parsePalette(raw) {
  var out = {}
  var lines = String(raw || "").split("\n")
  for (var i = 0; i < lines.length; i++) {
    var m = lines[i].match(/^\s*([A-Za-z0-9_-]+)\s*=\s*["']?(#[0-9A-Fa-f]{6})/)
    if (m) out[m[1]] = m[2].toLowerCase()
  }
  return out
}

function hexHueSat(hex) {
  var x = parseInt(String(hex).substring(1), 16)
  var r = ((x >> 16) & 255) / 255, g = ((x >> 8) & 255) / 255, b = (x & 255) / 255
  var max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min
  var l = (max + min) / 2
  var sat = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1))
  var hue = 0
  if (d !== 0) {
    if (max === r) hue = 60 * (((g - b) / d) % 6)
    else if (max === g) hue = 60 * ((b - r) / d + 2)
    else hue = 60 * ((r - g) / d + 4)
  }
  return { hue: (hue + 360) % 360, sat: sat }
}

// Diff colors from the theme: its green for added lines, its red for removed.
// Some themes make the two grey or nearly the same hue (hackerman, white,
// lumon); then null, and the panel falls back to its own pair.
function diffPalette(palette) {
  var p = palette || {}
  if (!p.green || !p.red) return null
  var g = hexHueSat(p.green), r = hexHueSat(p.red)
  var apart = Math.abs(g.hue - r.hue)
  apart = Math.min(apart, 360 - apart)
  if (g.sat < 0.15 || r.sat < 0.15 || apart < 60) return null
  return { add: p.green, remove: p.red }
}

// Repo URL field: a local path, HTTPS/SSH URL, or owner/repo never needs more.
var REPO_URL_LIMIT = 1024

// Replaces text[start, end) with insert, dropping control characters and
// keeping the result within limit. Returns the new text and cursor position.
function spliceText(text, start, end, insert, limit) {
  var s = String(text || "")
  var from = Math.max(0, Math.min(Number(start) || 0, Number(end) || 0, s.length))
  var to = Math.min(s.length, Math.max(Number(start) || 0, Number(end) || 0, from))
  var piece = String(insert || "").replace(/[\u0000-\u001f\u007f]/g, "")
  var room = Math.max(0, (Number(limit) || Infinity) - (s.length - (to - from)))
  if (piece.length > room) piece = piece.substring(0, room)
  return { text: s.substring(0, from) + piece + s.substring(to), cursor: from + piece.length }
}
