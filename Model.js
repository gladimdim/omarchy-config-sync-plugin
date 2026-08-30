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
    case "ready":
      return "The repo looks like Omarchy config. Review shortcuts, plugins, and files, then Apply to this machine — or Publish if this laptop is the source of truth."
    case "local-ahead":
      return localN === 1
        ? "1 local change is not in the repo yet. Publish to share it with your other laptops."
        : localN + " local changes are not in the repo yet. Publish to share them with your other laptops."
    case "remote-ahead":
      return "The repo has config this laptop has not applied. Review the incoming files, then Apply."
    case "diverged":
      return "This laptop and the repo both moved. Resolve files marked Both, then Apply and/or Publish."
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
