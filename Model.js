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
    case "empty": return "Empty repo — seed from this laptop"
    case "local-ahead": return "Local changes"
    case "remote-ahead": return "Incoming updates"
    case "diverged": return "Both sides changed"
    case "conflicts": return "Merge conflicts"
    case "invalid": return "Not an Omarchy config"
    case "not-configured": return "Not linked"
    default: return "Config Sync"
  }
}

function stateHint(state, status) {
  var localN = status && status.local_changes ? Number(status.local_changes) : 0
  var repoN = status && status.repo_changes ? Number(status.repo_changes) : 0
  var bothN = status && status.both_changed ? Number(status.both_changed) : 0
  var differs = status && status.unknown_differs ? Number(status.unknown_differs) : 0
  switch (String(state || "")) {
    case "in-sync":
      return "This laptop matches the linked config repo."
    case "empty":
      return "This GitHub repo is empty (or only has a README). The tabs show this laptop. Press Publish this laptop to seed the private repo, then use Apply on your other machines."
    case "ready":
      return "The repo looks like Omarchy config. Review shortcuts, plugins, and files, then Apply to this machine — or Publish if this laptop is the source of truth."
    case "local-ahead":
      return localN === 1
        ? "1 local change is not in the repo yet. Publish to share it with your other laptops."
        : localN + " local changes are not in the repo yet. Publish to share them with your other laptops."
    case "remote-ahead":
      return "The repo has config this laptop has not applied. Review the incoming files, then Apply."
    case "diverged":
      return "This laptop and the repo both moved. Resync from repo to make this machine match git (usual on a second laptop). Or Review Changes and pick item by item."
    case "conflicts":
      return "Git could not merge automatically. Keep the local copy or take the incoming copy for each conflicted file."
    case "invalid":
      return "The linked git repo is missing Hyprland / Omarchy config files."
    default:
      return "Paste the git URL of your omarchy-config repo to get started."
  }
}

function fileStatusLabel(status) {
  switch (String(status || "")) {
    case "local": return "Local only"
    case "added-local": return "New on this laptop"
    case "repo": return "Incoming"
    case "added-repo": return "New in repo"
    case "both": return "Both changed"
    case "differs": return "Different"
    case "identical": return "In sync"
    case "machine": return "This machine"
    default: return String(status || "")
  }
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

function reviewItem(kind, id, label, summary, status, typeLabel, both, changedCount, hidden) {
  return {
    kind: kind,
    itemId: String(id || ""),
    label: String(label || ""),
    summary: String(summary || ""),
    status: status,
    typeLabel: typeLabel || "",
    both: !!both || String(status) === "both",
    changed_count: changedCount || 0,
    hidden: !!hidden
  }
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

function pickedInItems(items, picks) {
  var n = 0
  var list = items || []
  var map = picks || {}
  for (var i = 0; i < list.length; i++) {
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

function appendThemes(out, list, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var t = rows[i]
    var id = t.id || "selected"
    if (isItemHidden("t", id, hiddenMap, t)) continue
    out.push(reviewItem("t", id, t.display || t.slug, t.slug, t.status, "Theme", t.status === "both", 0, false))
  }
}

function appendShortcuts(out, list, summaryField, both, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var s = rows[i]
    if (isItemHidden("s", s.keys, hiddenMap, s)) continue
    var sum = summaryField === "detail" ? (s.detail || s.label || "") : (s.label || "")
    out.push(reviewItem("s", s.keys, s.keys, sum, s.status, "Shortcut", both || s.status === "both", 0, false))
  }
}

function appendBundles(out, list, both, hiddenMap) {
  var rows = list || []
  for (var i = 0; i < rows.length; i++) {
    var b = rows[i]
    if (isItemHidden("g", b.id, hiddenMap, b)) continue
    var typeLabel = b.kind === "plugin" ? "Plugin" : "Folder"
    var n = Number(b.changed_count || (b.files ? b.files.length : 0) || 0)
    var sum = b.summary || (n + (n === 1 ? " file" : " files"))
    out.push(reviewItem("g", b.id, b.name || b.plugin_id || b.id, sum, b.status, typeLabel, both || b.status === "both", n, false))
  }
}

function appendLooseFiles(out, files, both, hiddenMap) {
  var rows = files || []
  for (var i = 0; i < rows.length; i++) {
    var f = rows[i]
    var p = String(f.path || "")
    if (!p || isBundledPath(p)) continue
    if (isItemHidden("f", p, hiddenMap, f)) continue
    out.push(reviewItem("f", p, p, f.summary || "", f.status, "File", both || f.status === "both", 0, false))
  }
}

function appendPluginFilesAsFolders(out, files, both, alreadyBundled, hiddenMap) {
  var covered = alreadyBundled || {}
  var buckets = {}
  var order = []
  var rows = files || []
  for (var i = 0; i < rows.length; i++) {
    var f = rows[i]
    if (f.status === "identical" || f.status === "machine") continue
    var p = String(f.path || "")
    if (p.indexOf("plugins/") !== 0) continue
    var pid = p.split("/")[1] || ""
    if (!pid || covered[pid]) continue
    if (isItemHidden("g", "plugin:" + pid, hiddenMap, f)) continue
    if (isItemHidden("f", p, hiddenMap, f)) continue
    var bid = "plugin:" + pid
    if (!buckets[bid]) {
      buckets[bid] = { id: bid, name: pid, count: 0, status: f.status }
      order.push(bid)
    }
    buckets[bid].count++
  }
  for (var j = 0; j < order.length; j++) {
    var g = buckets[order[j]]
    var n = g.count
    out.push(reviewItem("g", g.id, g.name, n + (n === 1 ? " file" : " files"), g.status, "Plugin", both || g.status === "both", n, false))
  }
}

function bundledPluginIds(bundles) {
  var covered = {}
  var rows = bundles || []
  for (var i = 0; i < rows.length; i++) {
    var b = rows[i]
    if (b.kind === "plugin" && b.plugin_id) covered[b.plugin_id] = true
    var id = String(b.id || "")
    if (id.indexOf("plugin:") === 0) covered[id.substring(7)] = true
  }
  return covered
}

function buildIncomingItems(theme, addedShortcuts, changedShortcuts, bundles, files, allFiles, hiddenMap) {
  var out = []
  appendThemes(out, theme, hiddenMap)
  appendShortcuts(out, addedShortcuts, "label", false, hiddenMap)
  appendShortcuts(out, changedShortcuts, "detail", false, hiddenMap)
  appendBundles(out, bundles, false, hiddenMap)
  appendPluginFilesAsFolders(out, allFiles, false, bundledPluginIds(bundles), hiddenMap)
  appendLooseFiles(out, files, false, hiddenMap)
  return out
}

function buildOutgoingItems(theme, addedShortcuts, changedShortcuts, bundles, files, allFiles, hiddenMap) {
  var out = []
  appendThemes(out, theme, hiddenMap)
  appendShortcuts(out, addedShortcuts, "label", false, hiddenMap)
  appendShortcuts(out, changedShortcuts, "detail", false, hiddenMap)
  appendBundles(out, bundles, false, hiddenMap)
  appendPluginFilesAsFolders(out, allFiles, false, bundledPluginIds(bundles), hiddenMap)
  appendLooseFiles(out, files, false, hiddenMap)
  return out
}

function buildBothItems(theme, shortcuts, bundles, files, allFiles, hiddenMap) {
  var out = []
  appendThemes(out, theme, hiddenMap)
  appendShortcuts(out, shortcuts, "label", true, hiddenMap)
  appendBundles(out, bundles, true, hiddenMap)
  appendPluginFilesAsFolders(out, allFiles, true, bundledPluginIds(bundles), hiddenMap)
  appendLooseFiles(out, files, true, hiddenMap)
  return out
}

function buildHiddenItems(theme, shortcuts, bundles, files, allFiles, hiddenMap) {
  var out = []
  var seen = {}
  function addHidden(kind, id, label, summary, status, typeLabel, both, count) {
    var key = kind + ":" + id
    if (seen[key]) return
    seen[key] = true
    out.push(reviewItem(kind, id, label, summary, status, typeLabel, both, count, true))
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
      addHidden("s", s.keys, s.keys, s.label || s.detail || "", s.status, "Shortcut", s.status === "both", 0)
    }
  }

  var bList = bundles || []
  for (var bi = 0; bi < bList.length; bi++) {
    var b = bList[bi]
    if (isItemHidden("g", b.id, hiddenMap, b)) {
      var typeLabel = b.kind === "plugin" ? "Plugin" : "Folder"
      var n = Number(b.changed_count || (b.files ? b.files.length : 0) || 0)
      var sum = b.summary || (n + (n === 1 ? " file" : " files"))
      addHidden("g", b.id, b.name || b.plugin_id || b.id, sum, b.status, typeLabel, b.status === "both", n)
    }
  }

  var fList = files || []
  for (var fi = 0; fi < fList.length; fi++) {
    var f = fList[fi]
    var p = String(f.path || "")
    if (!p || f.status === "identical" || f.status === "machine") continue
    if (isItemHidden("f", p, hiddenMap, f)) {
      var parentHidden = false
      if (p.indexOf("plugins/") === 0) {
        var pid = p.split("/")[1] || ""
        if (pid && (isItemHidden("g", "plugin:" + pid, hiddenMap, null) || isItemHidden("p", pid, hiddenMap, null)))
          parentHidden = true
      }
      if (!parentHidden) {
        addHidden("f", p, p, f.summary || "", f.status, "File", f.status === "both", 0)
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
