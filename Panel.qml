import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "gladimdim.config-sync"
  ipcTarget: "gladimdim.config-sync"
  manageIpc: false

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property color muted: Color.muted
  readonly property color dim: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.65)
  readonly property color cardBg: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.05)
  readonly property color cardBorder: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.12)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string scriptPath: String(Qt.resolvedUrl("scripts/config_sync.py")).replace(/^file:\/\//, "")

  property bool busy: false
  property string lastError: ""
  property string lastMessage: ""
  property string pendingAction: ""
  property var pendingArgs: []
  property string pendingStdin: ""
  property string repoUrlInput: ""
  property int activeTab: 0
  property bool includeMachine: false
  property bool editingRepo: false
  property string confirmKind: ""
  property var bothPicks: ({})
  property var picks: ({})
  property var status: ({})
  property var inspect: null
  property var diffFiles: []
  property var shortcutDiffs: []
  property var pluginDiffs: []
  property var bundleDiffs: []
  property var pluginListDiffs: []
  property var themeDiff: null
  property bool openOnChanges: false
  property bool showingHidden: false

  readonly property bool configured: !!(status && status.configured)
  readonly property string reportedSyncState: String((status && status.sync_state) || (configured ? "in-sync" : "not-configured"))
  readonly property string syncState: {
    var raw = reportedSyncState
    // File-level "incoming" with nothing to review (comment-only bindings.lua,
    // hidden rows) must not keep the header on Incoming updates.
    if ((raw === "remote-ahead" || raw === "local-ahead") && !hasReviewable) {
      var ahead = Number((status && status.ahead) || 0)
      var behind = Number((status && status.behind) || 0)
      if (ahead === 0 && behind === 0)
        return "in-sync"
    }
    return raw
  }
  readonly property bool alarming: syncState === "conflicts" || syncState === "diverged" || syncState === "invalid"
  readonly property bool pending: syncState === "ready" || syncState === "empty" || syncState === "remote-ahead" || syncState === "local-ahead" || alarming
  readonly property color stateColor: alarming ? urgent : (pending ? accent : foreground)
  readonly property var tabs: [
    { name: "Overview", icon: "󰘿" },
    { name: "Changes", icon: "󰦓" },
    { name: "Configs", icon: "󰒓" }
  ]
  readonly property var allReviewItems: incomingItems.concat(outgoingItems).concat(bothItems)
  // Deletions are opt-in, so they are only ever a surprise if the confirm step stays silent.
  function selectedRemovals(direction) {
    var _ = root.picks
    var items = direction === "apply" ? incomingItems : outgoingItems
    var out = []
    for (var i = 0; i < items.length; i++) {
      var it = items[i]
      if (it.removal && root.isPicked(it.kind, it.itemId)) out.push(it.label)
    }
    return out
  }

  readonly property var hiddenMap: {
    var map = {}
    var list = (status && status.hidden) ? status.hidden : []
    for (var i = 0; i < list.length; i++) {
      map[list[i]] = true
    }
    return map
  }

  readonly property var incomingFiles: Model.filesByStatus(otherFiles, ["repo", "added-repo"])
  readonly property var localFiles: Model.filesByStatus(otherFiles, ["local", "added-local"])
  readonly property var bothFiles: Model.filesByStatus(otherFiles, ["both"])
  readonly property var differsFiles: Model.filesByStatus(otherFiles, ["differs"])
  readonly property var conflictFiles: (status && status.conflicts) ? status.conflicts : []
  readonly property var otherFiles: {
    var out = []
    for (var i = 0; i < diffFiles.length; i++) {
      var f = diffFiles[i]
      if (f.status === "identical" || f.status === "machine") continue
      if (!includeMachine && !f.portable) continue
      if (Model.isBundledPath(f.path)) continue
      if (f.group === "theme" && f.path !== "omarchy/theme.name") continue
      // Per-shortcut rows replace the whole bindings.lua file. If the parser
      // found no bind-level drift (comments, or unbind-then-bind that used to
      // collapse), keep the file so Incoming/Outgoing is not an empty header.
      if (f.path === "hypr/bindings.lua" && Model.hasVisibleShortcutDiffs(shortcutDiffs, hiddenMap)) continue
      out.push(f)
    }
    return out
  }
  readonly property var incomingShortcuts: Model.filesByStatus(shortcutDiffs, ["repo", "added-repo"])
  readonly property var incomingAddedShortcuts: Model.filesByStatus(shortcutDiffs, ["added-repo"])
  readonly property var incomingChangedShortcuts: Model.filesByStatus(shortcutDiffs, ["repo"])
  readonly property var localShortcuts: Model.filesByStatus(shortcutDiffs, ["local", "added-local"])
  readonly property var localAddedShortcuts: Model.filesByStatus(shortcutDiffs, ["added-local"])
  readonly property var localChangedShortcuts: Model.filesByStatus(shortcutDiffs, ["local"])
  readonly property var bothShortcuts: Model.filesByStatus(shortcutDiffs, ["both"])
  readonly property var incomingPlugins: Model.filesByStatus(pluginDiffs, ["repo", "added-repo"])
  readonly property var localPlugins: Model.filesByStatus(pluginDiffs, ["local", "added-local"])
  readonly property var bothPlugins: Model.filesByStatus(pluginDiffs, ["both"])
  readonly property var differsPlugins: Model.filesByStatus(pluginDiffs, ["differs"])
  readonly property var incomingBundles: Model.filesByStatus(bundleDiffs, ["repo", "added-repo", "differs"])
  readonly property var localBundles: Model.filesByStatus(bundleDiffs, ["local", "added-local"])
  readonly property var bothBundles: Model.filesByStatus(bundleDiffs, ["both"])
  readonly property var incomingPluginList: Model.filesByStatus(pluginListDiffs, ["repo", "added-repo"])
  readonly property var outgoingPluginList: Model.filesByStatus(pluginListDiffs, ["local", "added-local"])
  readonly property var incomingTheme: {
    if (!themeDiff) return []
    var st = String(themeDiff.status)
    if (st === "repo" || st === "added-repo" || st === "differs") return [themeDiff]
    return []
  }
  readonly property var outgoingTheme: {
    if (!themeDiff) return []
    var st = String(themeDiff.status)
    if (st === "local" || st === "added-local") return [themeDiff]
    return []
  }
  readonly property var bothTheme: {
    if (!themeDiff) return []
    if (String(themeDiff.status) === "both") return [themeDiff]
    return []
  }
  readonly property var incomingItems: Model.buildIncomingItems(incomingTheme, incomingAddedShortcuts, incomingChangedShortcuts, incomingBundles, incomingFiles.concat(differsFiles), diffFiles, hiddenMap, incomingPluginList)
  readonly property var outgoingItems: Model.buildOutgoingItems(outgoingTheme, localAddedShortcuts, localChangedShortcuts, localBundles, localFiles, diffFiles, hiddenMap, outgoingPluginList)
  readonly property var bothItems: Model.buildBothItems(bothTheme, bothShortcuts, bothBundles, bothFiles, diffFiles, hiddenMap)
  readonly property var hiddenItems: Model.buildHiddenItems((themeDiff ? [themeDiff] : []), shortcutDiffs, bundleDiffs, diffFiles, diffFiles, hiddenMap, pluginListDiffs)
  readonly property int incomingCount: incomingItems.length
  readonly property int outgoingCount: outgoingItems.length
  readonly property int bothCount: bothItems.length
  readonly property int hiddenCount: hiddenItems.length
  readonly property int incomingPicked: {
    var _ = picks
    return Model.pickedInItems(incomingItems, picks)
  }
  readonly property int outgoingPicked: {
    var _ = picks
    return Model.pickedInItems(outgoingItems, picks)
  }
  readonly property int bothPicked: {
    var _ = picks
    return Model.pickedInItems(bothItems, picks)
  }
  readonly property bool hasReviewable: incomingCount + outgoingCount + bothCount + conflictFiles.length > 0
  readonly property int unresolvedBoth: {
    var n = 0
    var i
    for (i = 0; i < bothFiles.length; i++) {
      if (isPicked("f", bothFiles[i].path) && !bothPicks[bothFiles[i].path]) n++
    }
    for (i = 0; i < bothShortcuts.length; i++) {
      if (isPicked("s", bothShortcuts[i].keys) && !bothPicks["s:" + bothShortcuts[i].keys]) n++
    }
    for (i = 0; i < bothPlugins.length; i++) {
      // A plugin is only ever rendered as its plugin:<id> bundle row, so that
      // row's Keep local / Take repo pick is the one the user can actually
      // make. Gating on the p: key alone leaves Apply permanently blocked.
      if (isPicked("p", bothPlugins[i].id)
          && !bothPicks["p:" + bothPlugins[i].id]
          && !bothPicks["g:plugin:" + bothPlugins[i].id]) n++
    }
    for (i = 0; i < bothBundles.length; i++) {
      if (isPicked("g", bothBundles[i].id) && !bothPicks["g:" + bothBundles[i].id]) n++
    }
    if (themeDiff && themeDiff.status === "both" && isPicked("t", "selected") && !bothPicks["t:selected"]) n++
    return n
  }

  function hideItem(kind, id) {
    var key = pickId(kind, id)
    run(["hide", key])
  }

  function unhideItem(kind, id) {
    var key = pickId(kind, id)
    run(["unhide", key])
  }

  function unhideAll() {
    run(["unhide", "--all"])
  }

  function refresh(fetch) {
    run(["snapshot"].concat(fetch ? ["--fetch"] : []))
  }

  function connectRepo() {
    var url = String(repoUrlInput || "").trim()
    if (!url) {
      lastError = "Paste a git URL or a local path to your omarchy-config repo."
      return
    }
    lastError = ""
    run(["connect", "--stdin"], url)
  }

  function cloneMap(obj) {
    var next = {}
    var keys = Object.keys(obj || {})
    for (var i = 0; i < keys.length; i++) next[keys[i]] = obj[keys[i]]
    return next
  }

  function pickId(kind, id) { return kind + ":" + id }

  function isPicked(kind, id) { return !!picks[pickId(kind, id)] }

  function togglePick(kind, id) {
    var key = pickId(kind, id)
    var next = cloneMap(picks)
    next[key] = !next[key]
    picks = next
  }


  // ---- No-scroll layout ----
  // The panel sizes itself to its content and never scrolls, so long lists
  // are laid out in columns (FitGrid) sized to the height left on screen.
  readonly property Item bodyItem: mainCol
  // Vertical first: the panel is one narrow column until a list runs out of
  // screen height; then that list adds columns and reports the width it needs.
  readonly property real baseWidth: Style.space(620)
  // Configs: category rail on the left, the open category's lists beside it.
  readonly property real railWidth: Style.space(190)
  readonly property real railGap: Style.space(12)
  property var widthNeeds: ({})
  property int widthSeq: 0
  function reportWidth(key, w) {
    if (!key) return
    var cur = widthNeeds[key] || 0
    if (Math.abs(cur - w) < 1) return
    var next = cloneMap(widthNeeds)
    if (w > 0) next[key] = w
    else delete next[key]
    widthNeeds = next
  }
  readonly property real neededWidth: {
    var m = baseWidth
    for (var k in widthNeeds) m = Math.max(m, widthNeeds[k])
    return m
  }
  readonly property real screenBudget: panel.availableCardHeight > 0
    ? panel.availableCardHeight - (panel.verticalContentInset || 0)
    : Style.space(900)
  // Bumped whenever content above a list can move, so grids re-measure.
  property int layoutTick: 0
  onActiveTabChanged: layoutTick++
  onOpenSectionChanged: layoutTick++
  onOpenCategoryChanged: layoutTick++
  onShowingHiddenChanged: layoutTick++
  onLastErrorChanged: layoutTick++
  onLastMessageChanged: layoutTick++
  onSyncStateChanged: layoutTick++

  property string openSection: ""
  property string openChip: ""
  property string openCategory: ""

  readonly property var changeSections: {
    var out = []
    if (incomingItems.length) out.push({ key: "in", title: "Incoming", subtitle: "From the repo · Apply", items: incomingItems, bulk: true })
    if (outgoingItems.length) out.push({ key: "out", title: "Outgoing", subtitle: "This machine · Publish", items: outgoingItems, bulk: true })
    if (bothItems.length) out.push({ key: "both", title: "Both sides", subtitle: "Keep local or Take repo on each row", items: bothItems, bulk: false })
    return out
  }
  readonly property var activeSection: {
    for (var i = 0; i < changeSections.length; i++)
      if (changeSections[i].key === openSection) return changeSections[i]
    // Default: the first-push list on an empty repo, otherwise the first group.
    for (var j = 0; j < changeSections.length; j++)
      if (syncState === "empty" && changeSections[j].key === "out") return changeSections[j]
    return changeSections.length ? changeSections[0] : null
  }
  // Category chips inside the open group. "All" is offered while the whole
  // group still fits in a readable grid.
  readonly property var activeChips: {
    if (!activeSection) return []
    var items = activeSection.items
    var order = ["Theme", "Shortcut", "Plugin", "Folder", "File"]
    var counts = {}
    for (var i = 0; i < items.length; i++) {
      var t = String(items[i].typeLabel || "File")
      counts[t] = (counts[t] || 0) + 1
    }
    var chips = []
    if (items.length <= 48) chips.push({ label: "All", count: items.length })
    for (var k = 0; k < order.length; k++)
      if (counts[order[k]]) chips.push({ label: order[k], count: counts[order[k]] })
    return chips.length > 2 || items.length > 48 ? chips : []
  }
  readonly property string activeChip: {
    for (var i = 0; i < activeChips.length; i++)
      if (activeChips[i].label === openChip) return openChip
    return activeChips.length ? activeChips[0].label : "All"
  }
  readonly property var shownChangeItems: {
    if (!activeSection) return []
    if (activeChip === "All") return activeSection.items
    return activeSection.items.filter(function(it) { return String(it.typeLabel || "File") === activeChip })
  }

  readonly property var configCategories: {
    var defs = [
      { id: "shortcuts", icon: "󰌌", title: "Shortcuts", subtitle: "Keyboard shortcuts (hypr/bindings.lua)", inspect: "shortcuts", extra: "shortcuts" },
      { id: "theme", icon: "󰏘", title: "Theme", subtitle: "Selected theme & custom theme styles (omarchy/theme.name)", inspect: "", extra: "" },
      { id: "plugins", icon: "󰐱", title: "Plugins", subtitle: "Shell plugins, widgets & bar layout (plugins/)", inspect: "plugins", extra: "plugins" },
      { id: "displays", icon: "󰍹", title: "Displays", subtitle: "Display layout & monitor rules (hypr/monitors.lua)", inspect: "", extra: "" },
      { id: "hyprland", icon: "󰒓", title: "Hyprland", subtitle: "Gaps, animations, window rules, input, autostart (hypr/)", inspect: "", extra: "" },
      { id: "shell", icon: "󰘿", title: "Shell", subtitle: "Bar layout, widgets, and idle settings (omarchy/shell.json)", inspect: "", extra: "" },
      { id: "terminals", icon: "󰞷", title: "Terminals", subtitle: "Alacritty, Foot, Ghostty, and Kitty configs (terminals/)", inspect: "", extra: "" },
      { id: "hooks", icon: "󰓢", title: "Hooks", subtitle: "Event automation scripts (omarchy/hooks/)", inspect: "hooks", extra: "hooks" },
      { id: "scripts", icon: "󰲋", title: "Scripts", subtitle: "Custom scripts in ~/.local/bin/ (bin/)", inspect: "bins", extra: "bins" },
      { id: "other", icon: "󰉋", title: "Other", subtitle: "Additional tracked configuration files", inspect: "", extra: "" }
    ]
    var out = []
    for (var i = 0; i < defs.length; i++) {
      var d = defs[i]
      d.changeItems = Model.itemsForCategory(allReviewItems, d.id)
      d.files = Model.filesForCategory(diffFiles, d.id)
      var inspected = d.inspect && inspect && inspect[d.inspect] ? inspect[d.inspect].length : 0
      d.total = d.changeItems.length + d.files.length + inspected
      if (d.total > 0) out.push(d)
    }
    return out
  }
  readonly property var activeCategory: {
    for (var i = 0; i < configCategories.length; i++)
      if (configCategories[i].id === openCategory) return configCategories[i]
    // Default: the first category with pending changes, else the first one.
    for (var j = 0; j < configCategories.length; j++)
      if (configCategories[j].changeItems.length > 0) return configCategories[j]
    return configCategories.length ? configCategories[0] : null
  }

  function setPicked(kind, id, on) {
    var next = cloneMap(picks)
    next[pickId(kind, id)] = on
    picks = next
  }

  function seedPicks() {
    var next = {}
    var i, key, item
    for (i = 0; i < otherFiles.length; i++) {
      item = otherFiles[i]
      key = pickId("f", item.path)
      next[key] = (key in picks) ? picks[key] : !!(item.default_apply || item.default_publish || item.status === "differs")
    }
    for (i = 0; i < shortcutDiffs.length; i++) {
      item = shortcutDiffs[i]
      key = pickId("s", item.keys)
      next[key] = (key in picks) ? picks[key] : !!(item.default_apply || item.default_publish)
    }
    for (i = 0; i < pluginDiffs.length; i++) {
      item = pluginDiffs[i]
      key = pickId("p", item.id)
      // Plugins run code: never default-check an incoming one, only ever an outgoing publish.
      next[key] = (key in picks) ? picks[key] : !!item.default_publish
    }
    for (i = 0; i < bundleDiffs.length; i++) {
      item = bundleDiffs[i]
      key = pickId("g", item.id)
      // Hooks/agents/branding/extensions/bin run code or steer an agent: same rule as plugins.
      next[key] = (key in picks) ? picks[key] : !!item.default_publish
    }
    for (i = 0; i < outgoingPluginList.length; i++) {
      item = outgoingPluginList[i]
      key = pickId("l", item.id)
      next[key] = (key in picks) ? picks[key] : !!item.default_publish
    }
    if (themeDiff) {
      key = pickId("t", "selected")
      next[key] = (key in picks) ? picks[key] : !!(themeDiff.default_apply || themeDiff.default_publish || themeDiff.status === "differs")
    }
    function seedReview(list) {
      var rows = list || []
      var ri, row, rkey, st
      for (ri = 0; ri < rows.length; ri++) {
        row = rows[ri]
        rkey = pickId(row.kind, row.itemId)
        if (rkey in next || row.pickable === false) continue
        st = String(row.status || "")
        next[rkey] = st === "repo" || st === "added-repo" || st === "differs" || st === "local" || st === "added-local" || st === "both"
      }
    }
    seedReview(incomingItems)
    seedReview(outgoingItems)
    seedReview(bothItems)
    picks = next
  }

  function reviewChanges() { activeTab = 1 }

  function shortcutDiffFor(keys) {
    var k = String(keys || "")
    for (var i = 0; i < shortcutDiffs.length; i++)
      if (String(shortcutDiffs[i].keys) === k) return shortcutDiffs[i]
    return null
  }

  function pluginDiffFor(id) {
    var k = String(id || "")
    for (var i = 0; i < pluginDiffs.length; i++)
      if (String(pluginDiffs[i].id) === k) return pluginDiffs[i]
    return null
  }

  function fileDiffFor(path) {
    var k = String(path || "")
    for (var i = 0; i < otherFiles.length; i++)
      if (String(otherFiles[i].path) === k) return otherFiles[i]
    return null
  }

  // Tick or untick every pickable row of one Change section.
  function pickItems(items, on) {
    var next = cloneMap(picks)
    for (var i = 0; i < (items || []).length; i++) {
      var row = items[i]
      if (row.pickable === false) continue
      next[pickId(row.kind, row.itemId)] = on
    }
    picks = next
  }

  function bulkPick(mode) {
    var next = cloneMap(picks)
    var keys = Object.keys(next)
    var i, k, on
    function sideOf(key) {
      if (key.indexOf("s:") === 0) {
        for (i = 0; i < shortcutDiffs.length; i++)
          if (pickId("s", shortcutDiffs[i].keys) === key)
            return shortcutDiffs[i].status
      } else if (key.indexOf("p:") === 0) {
        for (i = 0; i < pluginDiffs.length; i++)
          if (pickId("p", pluginDiffs[i].id) === key)
            return pluginDiffs[i].status
      } else if (key.indexOf("f:") === 0) {
        for (i = 0; i < otherFiles.length; i++)
          if (pickId("f", otherFiles[i].path) === key)
            return otherFiles[i].status
      } else if (key.indexOf("g:") === 0) {
        for (i = 0; i < bundleDiffs.length; i++)
          if (pickId("g", bundleDiffs[i].id) === key)
            return bundleDiffs[i].status
      } else if (key.indexOf("l:") === 0) {
        for (i = 0; i < outgoingPluginList.length; i++)
          if (pickId("l", outgoingPluginList[i].id) === key)
            return outgoingPluginList[i].status
      } else if (key === pickId("t", "selected") && themeDiff) {
        return themeDiff.status
      }
      return ""
    }
    for (var ki = 0; ki < keys.length; ki++) {
      k = keys[ki]
      var st = sideOf(k)
      if (mode === "all") on = true
      else if (mode === "none") on = false
      else if (mode === "in") on = st === "repo" || st === "added-repo" || st === "differs" || st === "both"
      else on = st === "local" || st === "added-local" || st === "differs" || st === "both"
      next[k] = on
    }
    picks = next
  }

  function selectedApplyFiles() {
    var out = []
    var i, f
    for (i = 0; i < otherFiles.length; i++) {
      f = otherFiles[i]
      if (!isPicked("f", f.path)) continue
      if (f.status === "both") {
        if (bothPicks[f.path] === "repo") out.push(f.path)
        continue
      }
      if (f.status === "repo" || f.status === "added-repo" || f.status === "differs") out.push(f.path)
    }
    return out
  }

  function selectedPublishFiles() {
    var out = []
    var i, f
    for (i = 0; i < otherFiles.length; i++) {
      f = otherFiles[i]
      if (!isPicked("f", f.path)) continue
      if (f.status === "both") {
        if (bothPicks[f.path] === "local") out.push(f.path)
        continue
      }
      if (f.status === "local" || f.status === "added-local" || f.status === "differs") out.push(f.path)
    }
    return out
  }

  function shortcutPortableFor(s, direction) {
    if (!s) return true
    if (s.status === "both")
      return direction === "apply" ? s.repo_portable !== false : s.local_portable !== false
    return s.portable !== false
  }

  function selectedApplyShortcuts() {
    var out = []
    var i, s
    for (i = 0; i < shortcutDiffs.length; i++) {
      s = shortcutDiffs[i]
      if (!isPicked("s", s.keys)) continue
      if (!shortcutPortableFor(s, "apply")) continue
      if (s.status === "both") {
        if (bothPicks["s:" + s.keys] === "repo") out.push(s.keys)
        continue
      }
      if (s.status === "added-repo" || s.status === "repo" || s.status === "differs") out.push(s.keys)
    }
    return out
  }

  function selectedPublishShortcuts() {
    var out = []
    var i, s
    for (i = 0; i < shortcutDiffs.length; i++) {
      s = shortcutDiffs[i]
      if (!isPicked("s", s.keys)) continue
      if (!shortcutPortableFor(s, "publish")) continue
      if (s.status === "both") {
        if (bothPicks["s:" + s.keys] === "local") out.push(s.keys)
        continue
      }
      if (s.status === "added-local" || s.status === "local") out.push(s.keys)
    }
    return out
  }

  function pluginIdFromBundle(id) {
    var raw = String(id || "")
    if (raw.indexOf("plugin:") === 0) return raw.substring(7)
    return raw
  }

  function selectedApplyPlugins() {
    var out = []
    var seen = {}
    var i, b, pid
    for (i = 0; i < bundleDiffs.length; i++) {
      b = bundleDiffs[i]
      if (b.kind !== "plugin" || !isPicked("g", b.id)) continue
      if (b.status === "both") {
        if (bothPicks["g:" + b.id] !== "repo") continue
      } else if (!(b.status === "added-repo" || b.status === "repo" || b.status === "differs")) {
        continue
      }
      pid = b.plugin_id || pluginIdFromBundle(b.id)
      if (pid && !seen[pid]) { seen[pid] = true; out.push(pid) }
    }
    for (i = 0; i < incomingItems.length; i++) {
      b = incomingItems[i]
      if (b.kind !== "g" || b.typeLabel !== "Plugin" || !isPicked("g", b.itemId)) continue
      pid = pluginIdFromBundle(b.itemId)
      if (pid && !seen[pid]) { seen[pid] = true; out.push(pid) }
    }
    return out
  }

  function selectedBundleFiles(direction) {
    var out = []
    var i, b, j
    for (i = 0; i < bundleDiffs.length; i++) {
      b = bundleDiffs[i]
      if (b.kind === "plugin") continue
      if (!isPicked("g", b.id)) continue
      if (b.status === "both") {
        if (direction === "apply" && bothPicks["g:" + b.id] !== "repo") continue
        if (direction === "publish" && bothPicks["g:" + b.id] !== "local") continue
      } else if (direction === "apply") {
        if (!(b.status === "added-repo" || b.status === "repo" || b.status === "differs")) continue
      } else if (!(b.status === "added-local" || b.status === "local" || b.status === "differs")) continue
      var list = b.files || []
      for (j = 0; j < list.length; j++) out.push(list[j])
    }
    return out
  }

  function selectedApplyTheme() {
    if (!themeDiff || !isPicked("t", "selected")) return false
    if (themeDiff.status === "both") return bothPicks["t:selected"] === "repo"
    return themeDiff.status === "added-repo" || themeDiff.status === "repo" || themeDiff.status === "differs"
  }

  function selectedPublishTheme() {
    if (!themeDiff || !isPicked("t", "selected")) return false
    if (themeDiff.status === "both") return bothPicks["t:selected"] === "local"
    return themeDiff.status === "added-local" || themeDiff.status === "local" || themeDiff.status === "differs"
  }

  function selectedPublishPlugins() {
    var out = []
    var seen = {}
    var i, b, pid
    for (i = 0; i < bundleDiffs.length; i++) {
      b = bundleDiffs[i]
      if (b.kind !== "plugin" || !isPicked("g", b.id)) continue
      if (b.status === "both") {
        if (bothPicks["g:" + b.id] !== "local") continue
      } else if (!(b.status === "added-local" || b.status === "local" || b.status === "differs")) {
        continue
      }
      pid = b.plugin_id || pluginIdFromBundle(b.id)
      if (pid && !seen[pid]) { seen[pid] = true; out.push(pid) }
    }
    for (i = 0; i < outgoingItems.length; i++) {
      b = outgoingItems[i]
      if (b.kind !== "g" || b.typeLabel !== "Plugin" || !isPicked("g", b.itemId)) continue
      pid = pluginIdFromBundle(b.itemId)
      if (pid && !seen[pid]) { seen[pid] = true; out.push(pid) }
    }
    return out
  }

  // Only outgoing rows are picks; installs and updates go through Omarchy.
  function selectedPublishListPlugins() {
    var out = []
    for (var i = 0; i < outgoingItems.length; i++) {
      var row = outgoingItems[i]
      if (row.kind === "l" && row.pickable !== false && isPicked("l", row.itemId)) out.push(row.itemId)
    }
    return out
  }

  function runPluginAction(action, id) {
    if (action === "install") run(["install-plugin", id])
    else if (action === "update") run(["update-plugin", id])
  }

  function selectSide(kind, id, side) {
    setPick(kind === "f" ? id : (kind + ":" + id), side)
    setPicked(kind, id, true)
  }

  function requestApply() {
    if (unresolvedBoth > 0) {
      lastError = "Pick Keep local or Take repo for each file that changed on both sides."
      activeTab = 1
      return
    }
    if (conflictFiles.length > 0) {
      lastError = "Resolve git merge conflicts before applying."
      activeTab = 1
      return
    }
    if (selectedApplyFiles().length + selectedApplyShortcuts().length + selectedApplyPlugins().length + selectedBundleFiles("apply").length === 0 && !selectedApplyTheme()) {
      lastError = "Check the incoming shortcuts, plugins, or files you want to apply."
      activeTab = 1
      return
    }
    confirmKind = "apply"
  }

  function requestPublish() {
    if (unresolvedBoth > 0) {
      lastError = "Pick Keep local or Take repo for each file that changed on both sides."
      activeTab = 1
      return
    }
    if (conflictFiles.length > 0) {
      lastError = "Resolve git merge conflicts before publishing."
      activeTab = 1
      return
    }
    if (selectedPublishFiles().length + selectedPublishShortcuts().length + selectedPublishPlugins().length + selectedPublishListPlugins().length + selectedBundleFiles("publish").length === 0 && !selectedPublishTheme() && Number(status.ahead || 0) === 0) {
      lastError = "Check the local shortcuts, plugins, or files you want to publish."
      activeTab = 1
      return
    }
    confirmKind = "publish"
  }

  function confirmCurrent() {
    var kind = confirmKind
    confirmKind = ""
    if (kind === "apply") {
      var files = selectedApplyFiles().concat(selectedBundleFiles("apply"))
      var args = ["apply", "--explicit", "--files", files.join(",")]
      if (includeMachine) args.push("--include-machine")
      var ashort = selectedApplyShortcuts()
      var aplugs = selectedApplyPlugins()
      var ai
      for (ai = 0; ai < ashort.length; ai++) args.push("--shortcut", ashort[ai])
      for (ai = 0; ai < aplugs.length; ai++) args.push("--plugin", aplugs[ai])
      if (selectedApplyTheme()) args.push("--theme")
      run(args)
    } else if (kind === "publish") {
      var pub = selectedPublishFiles().concat(selectedBundleFiles("publish"))
      var pargs = ["publish", "--push", "--explicit", "--files", pub.join(",")]
      if (includeMachine) pargs.push("--include-machine")
      var pshort = selectedPublishShortcuts()
      var pplugs = selectedPublishPlugins()
      var pi
      for (pi = 0; pi < pshort.length; pi++) pargs.push("--shortcut", pshort[pi])
      for (pi = 0; pi < pplugs.length; pi++) pargs.push("--plugin", pplugs[pi])
      var plist = selectedPublishListPlugins()
      for (pi = 0; pi < plist.length; pi++) pargs.push("--list-plugin", plist[pi])
      if (selectedPublishTheme()) pargs.push("--theme")
      run(pargs)
    } else if (kind === "disconnect") {
      run(["disconnect"])
      repoUrlInput = ""
      editingRepo = false
      bothPicks = ({})
    } else if (kind === "switch-repo") {
      editingRepo = false
      lastError = ""
      var targetUrl = String(repoUrlInput || "").trim()
      run(["connect", "--stdin"], targetUrl)
    } else if (kind === "resync-repo") {
      run(["resync", "--side", "repo"])
    } else if (kind === "resync-local") {
      run(["resync", "--side", "local"])
    } else if (kind === "mirror-local") {
      run(["resync", "--side", "local", "--mirror"])
    } else if (kind === "mirror-repo") {
      run(["resync", "--side", "repo", "--mirror"])
    }
  }

  function startEditRepo() {
    repoUrlInput = String((status && status.repo_url) || repoUrlInput || "")
    editingRepo = true
    activeTab = 0
    lastError = ""
  }

  function cancelEditRepo() {
    editingRepo = false
    repoUrlInput = String((status && status.repo_url) || "")
  }

  function saveEditRepo() {
    var url = String(repoUrlInput || "").trim()
    if (!url) {
      lastError = "Paste a git URL or a local path to the config repo."
      return
    }
    var current = String((status && status.repo_url) || "").replace(/\/+$/, "").replace(/\.git$/, "")
    var next = url.replace(/\/+$/, "").replace(/\.git$/, "")
    if (current && (next === current || next === current + ".git" || current === next + ".git")) {
      editingRepo = false
      lastMessage = "Already linked to that repo."
      return
    }
    confirmKind = "switch-repo"
  }

  function pullRemote() {
    run(["pull"])
  }

  function setPick(path, side) {
    var next = {}
    var keys = Object.keys(bothPicks)
    for (var i = 0; i < keys.length; i++) next[keys[i]] = bothPicks[keys[i]]
    next[path] = side
    bothPicks = next
  }

  function resolveConflict(path, side) {
    run(["resolve", path, "--side", side])
  }

  function openFile(path, localPath, repoPath) {
    var target = String(localPath || "").trim()
    if (!target) {
      target = String(repoPath || "").trim()
    }
    if (!target) {
      target = String(path || "").trim()
    }
    if (!target) return
    Quickshell.execDetached(["python3", root.scriptPath, "open", target])
  }

  function openTerminal(path, localPath, repoPath) {
    var target = String(localPath || "").trim()
    if (!target) {
      target = String(repoPath || "").trim()
    }
    if (!target) {
      target = String(path || "").trim()
    }
    if (!target) return
    Quickshell.execDetached(["python3", root.scriptPath, "terminal", target])
  }

  function applySnapshot(data) {
    status = data.status || {}
    inspect = data.inspect || null
    diffFiles = (data.diff && data.diff.files) ? data.diff.files : []
    shortcutDiffs = (data.diff && data.diff.shortcuts) ? data.diff.shortcuts : []
    pluginDiffs = (data.diff && data.diff.plugins) ? data.diff.plugins : []
    bundleDiffs = (data.diff && data.diff.bundles) ? data.diff.bundles : []
    pluginListDiffs = (data.diff && data.diff.plugin_list) ? data.diff.plugin_list : []
    themeDiff = (data.diff && data.diff.theme) ? data.diff.theme : null
    if (data.sync_state && status)
      status = Object.assign({}, status, { sync_state: data.sync_state })
    if (!editingRepo && status.repo_url)
      repoUrlInput = String(status.repo_url)
    Qt.callLater(function() {
      root.seedPicks()
      // An empty repo opens on Overview: the first-push card is the guide there.
      if (root.openOnChanges && root.syncState === "empty") {
        root.activeTab = 0
        root.openOnChanges = false
      } else if (root.openOnChanges && root.hasReviewable) {
        root.activeTab = 1
        root.openOnChanges = false
      } else {
        root.openOnChanges = false
      }
    })
  }

  function run(args, stdinData) {
    if (syncProc.running) {
      pendingArgs = args
      pendingStdin = String(stdinData || "")
      return
    }
    busy = true
    lastError = ""
    pendingAction = args[0] || ""
    syncProc.command = ["python3", "-u", root.scriptPath].concat(args)
    syncProc.running = true
    // The helper reads a line from stdin and we never close the pipe, so a
    // --stdin command must always get its newline — otherwise it blocks on a
    // read that never returns and the panel stays busy forever.
    if (stdinData || args.indexOf("--stdin") >= 0) {
      syncProc.write(String(stdinData || "") + "\n")
    }
  }

  function handleOutput(text) {
    busy = false
    var raw = String(text || "").trim()
    if (!raw) {
      lastError = "The sync helper returned no output."
      return
    }
    if (raw.length > 5 * 1024 * 1024) {
      lastError = "Sync response exceeded maximum buffer size limit (5MB)."
      return
    }
    var data
    try {
      data = JSON.parse(raw)
    } catch (e) {
      lastError = "Could not parse sync helper output."
      return
    }
    if (!data.ok) {
      lastError = String(data.error || "Sync failed.")
      if (data.both) activeTab = 1
      if (data.conflicts && data.conflicts.length > 0) {
        status = Object.assign({}, status, { conflicts: data.conflicts, sync_state: "conflicts", configured: true })
        activeTab = 1
      }
      return
    }
    lastMessage = String(data.message || "")
    if (data.connected)
      editingRepo = false
    if (data.status || data.configured === false || data.disconnected)
      applySnapshot(data)
    if (data.push_error)
      lastError = String(data.push_error)
    if (data.disconnected) {
      status = { configured: false, sync_state: "not-configured", plugin_version: (status && status.plugin_version) || "" }
      inspect = null
      diffFiles = []
      bothPicks = ({})
      activeTab = 0
    }
  }

  onOpenedChanged: {
    if (opened) {
      confirmKind = ""
      lastError = ""
      openOnChanges = true
      refresh(true)
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
    }
  }

  Component.onCompleted: refresh(true)

  Timer {
    interval: 10 * 60 * 1000
    running: true
    repeat: true
    onTriggered: if (!root.busy) root.refresh(true)
  }

  Process {
    id: syncProc
    stdinEnabled: true
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.handleOutput(text)
        if (root.pendingArgs.length > 0) {
          var next = root.pendingArgs
          var nextStdin = root.pendingStdin
          root.pendingArgs = []
          root.pendingStdin = ""
          root.run(next, nextStdin)
        }
      }
    }
    stderr: StdioCollector { waitForEnd: true }
  }

  IpcHandler {
    target: "gladimdim.config-sync"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(true); return "ok" }
    function setTab(tab: int): void { root.activeTab = tab }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰘿"
    tooltipText: configured
      ? ("Config Sync — " + Model.stateTitle(root.syncState))
      : "Config Sync — link your omarchy-config repo"
    onPressed: function(b) {
      if (b === Qt.RightButton) root.refresh(true)
      else root.toggle()
    }
  }

  Rectangle {
    visible: root.pending && !root.opened
    width: 7
    height: 7
    radius: 4
    color: root.stateColor
    border.width: 1
    border.color: root.bar ? root.bar.background : Color.background
    anchors.right: parent.right
    anchors.top: parent.top
    anchors.rightMargin: 1
    anchors.topMargin: 3
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(root.neededWidth)
    contentHeight: panel.fittedContentHeight(mainCol.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: urlField.activeFocus || root.editingRepo || root.confirmKind !== ""

      onCloseRequested: {
        if (root.confirmKind !== "") root.confirmKind = ""
        else root.close()
      }
      onMoveRequested: function(dx, dy) {
        if (!root.configured) return
        if (dx !== 0) {
          var n = root.tabs.length
          root.activeTab = (root.activeTab + dx + n) % n
        }
      }
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh(true)
        else if (t === "a" || t === "A") root.requestApply()
        else if (t === "p" || t === "P") root.requestPublish()
        else if (t === "c" || t === "C") root.reviewChanges()
        else if (t >= "1" && t <= String(root.tabs.length)) root.activeTab = parseInt(t) - 1
      }

      Column {
        id: mainCol
        anchors.fill: parent
        spacing: Style.space(10)

        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroInfo.implicitHeight, heroActions.implicitHeight)

          Text {
            id: heroIcon
            textFormat: Text.PlainText
            text: root.busy ? "󰦖" : "󰘿"
            color: root.stateColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroInfo
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(12)
            anchors.right: heroActions.left
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Row {
              width: parent.width
              spacing: Style.space(6)

              Text {
                id: heroTitle
                textFormat: Text.PlainText
                text: root.configured ? Model.repoName(root.status.repo_url) : "Config Sync"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
                width: Math.min(implicitWidth, parent.width - heroVersion.implicitWidth - parent.spacing)
              }

              Text {
                id: heroVersion
                textFormat: Text.PlainText
                text: root.status && root.status.plugin_version ? "v" + root.status.plugin_version : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                anchors.baseline: heroTitle.baseline
              }
            }

            Text {
              textFormat: Text.PlainText
              text: root.busy
                ? (root.pendingAction === "connect" ? "Fetching and checking the repo…" : "Working…")
                : Model.stateTitle(root.syncState)
              color: root.stateColor
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              width: parent.width
            }
          }

          Row {
            id: heroActions
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)
            height: Math.max(btnRefresh.implicitHeight, btnEdit.implicitHeight, btnClose.implicitHeight)

            Button {
              id: btnRefresh
              iconText: "󰑐"
              tooltipText: "Refresh (r)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              anchors.verticalCenter: parent.verticalCenter
              height: parent.height
              enabled: !root.busy
              onClicked: root.refresh(true)
            }

            Button {
              id: btnEdit
              visible: root.configured
              text: "Edit"
              tooltipText: "Use a different git repo"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              anchors.verticalCenter: parent.verticalCenter
              height: parent.height
              enabled: !root.busy
              onClicked: root.startEditRepo()
            }

            Button {
              id: btnClose
              visible: root.configured
              iconText: "󰅖"
              tooltipText: "Unlink this repo"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              anchors.verticalCenter: parent.verticalCenter
              height: parent.height
              enabled: !root.busy
              onClicked: root.confirmKind = "disconnect"
            }
          }
        }

        Text {
          visible: root.lastError !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: root.lastError
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        Text {
          visible: root.lastError === "" && root.lastMessage !== ""
          width: parent.width
          textFormat: Text.PlainText
          text: root.lastMessage
          color: root.accent
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        // ---------------- SETUP ----------------
        Item {
          visible: !root.configured
          width: parent.width
          implicitHeight: setupCol.implicitHeight
          height: implicitHeight

          Column {
            id: setupCol
            width: parent.width
            spacing: Style.space(12)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: "First time: create a private GitHub repo for your Omarchy configs, then paste its URL here. The plugin will not make that repo public."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            GuideStep {
              step: "1"
              title: "Create a private GitHub repo"
              body: "github.com/new → name it omarchy-config → visibility Private → leave README / .gitignore / license unchecked → Create repository. Private keeps shortcuts, hooks, and scripts off the public internet."
            }

            Row {
              spacing: Style.space(8)
              Button {
                text: "Open GitHub"
                iconText: "󰊤"
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                onClicked: Quickshell.execDetached(["xdg-open", "https://github.com/new"])
              }
              Button {
                text: "Copy gh auth login"
                tooltipText: "The plugin cannot ask for a GitHub password (that would freeze the bar). Paste this in a terminal, finish the browser login, then Connect."
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: {
                  Quickshell.execDetached(["wl-copy", "gh auth login"])
                  root.lastMessage = "Copied gh auth login — run it in a terminal, finish the browser login, then come back and Connect."
                }
              }
            }

            GuideStep {
              step: "2"
              title: "Paste the repo URL"
              body: "HTTPS (https://github.com/you/omarchy-config.git), SSH, or owner/repo. An empty private repo is what you want on the first machine. On the next machine, paste this same URL and Apply."
            }

            TextField {
              id: urlField
              width: parent.width
              placeholderText: "https://github.com/you/omarchy-config.git"
              text: root.repoUrlInput
              foreground: root.foreground
              font.family: root.fontFamily
              enabled: !root.busy
              onTextChanged: root.repoUrlInput = text
              onAccepted: root.connectRepo()
              Keys.onPressed: function(event) {
                if (event.key === Qt.Key_Escape) {
                  keyCatcher.forceActiveFocus()
                  event.accepted = true
                }
              }
            }

            Row {
              spacing: Style.space(8)
              Button {
                text: root.busy ? "Connecting…" : "Connect repo"
                iconText: "󰓦"
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !root.busy && String(root.repoUrlInput).trim() !== ""
                bordered: true
                onClicked: root.connectRepo()
              }
              Button {
                text: "Use this machine's clone"
                tooltipText: "If you already keep configs in ~/Github/omarchy-config"
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !root.busy
                onClicked: {
                  root.repoUrlInput = Quickshell.env("HOME") + "/Github/omarchy-config"
                  urlField.text = root.repoUrlInput
                }
              }
            }

            GuideStep {
              step: "3"
              title: "Review, then Seed repo"
              body: "Empty repo: the tabs show this machine. Seed repo pushes it to GitHub (still private). Next machine: Connect the same URL and press Apply. Display layout is skipped unless you opt in."
            }
          }
        }

        // ---------------- CONFIGURED TABS ----------------
        Row {
          id: tabRow
          visible: root.configured
          width: parent.width
          spacing: Style.space(4)
          readonly property real tabWidth: (width - spacing * (root.tabs.length - 1)) / root.tabs.length

          Repeater {
            model: root.tabs
            Button {
              required property var modelData
              required property int index
              width: tabRow.tabWidth
              iconText: modelData.icon
              text: modelData.name
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              selected: root.activeTab === index
              bordered: true
              horizontalPadding: Style.space(2)
              verticalPadding: Style.space(5)
              onClicked: root.activeTab = index
            }
          }
        }

        PanelSeparator {
          visible: root.configured
          foreground: root.foreground
        }

        Item {
          id: scrollArea
          visible: root.configured
          width: parent.width
          implicitHeight: loader.implicitHeight
          height: implicitHeight

          Loader {
            id: loader
            width: parent.width
            sourceComponent: {
              if (root.activeTab === 1) return tabChangesComp
              if (root.activeTab === 2) return tabConfigsComp
              return tabOverviewComp
            }
          }
        }
      }

      // Confirm overlay
      Rectangle {
        anchors.fill: parent
        visible: root.confirmKind !== ""
        color: Qt.rgba(0, 0, 0, 0.45)

        MouseArea { anchors.fill: parent; onClicked: root.confirmKind = "" }

        BorderSurface {
          anchors.centerIn: parent
          width: Math.min(parent.width - Style.space(24), Style.space(360))
          implicitHeight: confirmCol.implicitHeight + Style.space(28)
          color: Color.popups.background
          borderSpec: Border.flat(root.accent, Style.normalBorderWidth)
          radius: Style.cornerRadius
          padding: Style.space(16)

          MouseArea { anchors.fill: parent; onClicked: {} }

          Column {
            id: confirmCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.space(16)
            anchors.rightMargin: Style.space(16)
            spacing: Style.space(12)

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: root.confirmKind === "apply"
                ? "Apply the checked incoming shortcuts, plugins, and files onto this machine? A timestamped backup is written first."
                : root.confirmKind === "publish"
                  ? (root.syncState === "empty"
                    ? "Seed this private GitHub repo with the checked items from this machine, then push? Keep the repo private so shortcuts, hooks, and scripts are not public."
                    : "Copy the checked local shortcuts, plugins, and files into the repo, commit, and push?")
                  : root.confirmKind === "switch-repo"
                    ? "Point this machine at a different git repo? Local files are not deleted. The new repo is cloned and checked before anything is applied."
                    : root.confirmKind === "resync-repo"
                      ? "Make this machine match the git repo? Incoming plugins, shortcuts, theme, and configs overwrite local copies. A timestamped backup is written first. Extra files that exist only on this machine are left in place."
                      : root.confirmKind === "resync-local"
                        ? "Overwrite the git repo with this machine's config, then push?"
                        : root.confirmKind === "mirror-local"
                          ? "Mirror this machine into the repo, then push? Everything goes up: bindings.lua as a whole file, every plugin, hook and bin tool, the theme, and machine-local files such as the display layout. Keep the repo private."
                          : root.confirmKind === "mirror-repo"
                            ? "Make this machine an exact copy of the repo? Everything is applied: bindings.lua as a whole file, every plugin, hook and bin tool, the theme, and machine-local files such as the display layout. A backup is written first, then Omarchy's installer opens for listed plugins. Files that exist only here are left alone."
                            : "Unlink the config repo on this machine? Local files are left as they are."
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              textFormat: Text.PlainText
              visible: root.confirmKind === "apply" || root.confirmKind === "publish"
              text: {
                var names = root.selectedRemovals(root.confirmKind)
                if (names.length === 0) return ""
                var where = root.confirmKind === "apply" ? "deleted from this machine" : "deleted from the repo"
                var head = names.length + (names.length === 1 ? " item" : " items") + " will be " + where + ":\n"
                return head + names.slice(0, 8).join(", ") + (names.length > 8 ? ", and " + (names.length - 8) + " more" : "")
              }
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Row {
              spacing: Style.space(8)
              layoutDirection: Qt.RightToLeft
              width: parent.width

              Button {
                text: {
                  switch (root.confirmKind) {
                    case "disconnect": return "Unlink"
                    case "switch-repo": return "Switch repo"
                    case "resync-repo": return "Take repo"
                    case "resync-local": return "Take this machine"
                    case "mirror-local": return "Mirror & push"
                    case "mirror-repo": return "Mirror onto this machine"
                    case "publish": return root.syncState === "empty" ? "Seed & push" : "Publish"
                    default: return "Apply"
                  }
                }
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                onClicked: root.confirmCurrent()
              }
              Button {
                text: "Cancel"
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.confirmKind = ""
              }
            }
          }
        }
      }
    }
  }

  Component {
    id: tabOverviewComp
    // Status, Mirror and repo details stack top to bottom in one column and
    // only flow into a second column when the screen is too short for them.
    Flow {
      id: overviewRow
      flow: Flow.TopToBottom
      width: parent.width
      spacing: Style.space(12)
      readonly property real colW: Math.min(root.baseWidth, width)
      property real topY: 0
      readonly property real stackHeight: {
        var h = 0
        var n = 0
        for (var i = 0; i < children.length; i++) {
          var c = children[i]
          if (!c.visible || c.implicitHeight <= 0) continue
          h += c.implicitHeight
          n++
        }
        return h + Math.max(0, n - 1) * spacing
      }
      height: Math.min(stackHeight, Math.max(Style.space(200), root.screenBudget - topY - Style.space(8)))
      function measure() {
        if (!root.bodyItem) return
        var y = overviewRow.mapToItem(root.bodyItem, 0, 0).y
        if (Math.abs(y - topY) > 1) topY = y
      }
      onChildrenRectChanged: if (root) root.reportWidth("overview", childrenRect.width)
      Component.onCompleted: Qt.callLater(measure)
      Component.onDestruction: if (root) root.reportWidth("overview", 0)
      Timer {
        interval: 200
        repeat: true
        running: root.opened
        onTriggered: overviewRow.measure()
      }

      Column {
        width: overviewRow.colW
        spacing: Style.space(12)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: Model.stateHint(root.syncState, root.status)
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Row {
            width: parent.width
            spacing: Style.space(8)
            readonly property real pillW: (width - spacing * 3) / 4

            QuickPill {
              width: parent.pillW
              icon: "󰌌"
              label: "Shortcuts"
              value: root.inspect && root.inspect.shortcuts ? String(root.inspect.shortcuts.length) : "—"
            }
            QuickPill {
              width: parent.pillW
              icon: "󰐱"
              label: "Plugins"
              value: root.inspect && root.inspect.plugins ? String(root.inspect.plugins.length) : "—"
            }
            QuickPill {
              width: parent.pillW
              icon: "󰅧"
              label: "Incoming"
              value: String(root.incomingCount)
              highlightColor: root.incomingCount > 0 ? root.accent : root.foreground
            }
            QuickPill {
              width: parent.pillW
              icon: "󰈸"
              label: "Outgoing"
              value: String(root.outgoingCount)
              highlightColor: root.outgoingCount > 0 ? root.accent : root.foreground
            }
          }

          // First push. The generic action row below is hidden in this state so
          // there is exactly one obvious thing to press.
          CardBox {
            visible: root.syncState === "empty"
            border.width: 2
            border.color: root.accent

            GuideStep {
              step: "✓"
              title: "Repo linked"
              body: Model.repoName(root.status && root.status.repo_url) + " is connected and empty. Nothing has been pushed yet."
            }
            GuideStep {
              step: "2"
              title: "Check what goes up"
              body: root.outgoingPicked + " of " + root.outgoingCount + " items from this machine are ticked. Review list shows them. Display layout stays local unless you opt in."
            }
            GuideStep {
              step: "3"
              title: "Seed the repo"
              body: "Pushes the ticked items as the first commit. The repo stays private. On your next machine: Connect the same URL, then Apply."
            }
            Row {
              spacing: Style.space(8)
              Button {
                text: "Seed repo (" + root.outgoingPicked + " items)"
                iconText: "󰓂"
                tooltipText: "Push this machine's ticked items as the repo's first commit (p)"
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                selected: true
                enabled: !root.busy
                onClicked: root.requestPublish()
              }
              Button {
                text: "Review list"
                iconText: "󰦓"
                tooltipText: "Tick or untick items before seeding (c)"
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.reviewChanges()
              }
            }
            Button {
              text: "Seed everything (exact mirror)"
              iconText: "󰆏"
              tooltipText: "Everything, ticked or not: whole bindings.lua, plugins, hooks, bin, theme, display layout"
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.busy
              onClicked: root.confirmKind = "mirror-local"
            }
          }

          Row {
            visible: root.syncState !== "empty"
            spacing: Style.space(8)

            Button {
              visible: root.syncState === "diverged" || root.syncState === "conflicts" || (root.incomingFiles.length + root.incomingBundles.length + root.incomingAddedShortcuts.length + root.incomingChangedShortcuts.length > 0 && root.localFiles.length + root.localBundles.length + root.localAddedShortcuts.length + root.localChangedShortcuts.length > 0)
              text: "Resync from repo"
              iconText: "󰁨"
              tooltipText: "Make this machine match the git repo. A backup is written first."
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: !root.busy
              onClicked: root.confirmKind = "resync-repo"
            }
            Button {
              visible: root.hasReviewable
              text: "Review Changes"
              iconText: "󰦓"
              tooltipText: "Cherry-pick shortcuts, plugins, and files (c)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.reviewChanges()
            }
            Button {
              visible: root.syncState !== "empty"
              text: "Apply"
              iconText: "󰁨"
              tooltipText: "Apply checked incoming items (a)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: !root.busy
              onClicked: root.requestApply()
            }
            Button {
              text: "Publish"
              iconText: "󰓂"
              tooltipText: "Publish checked local items (p)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: !root.busy
              onClicked: root.requestPublish()
            }
            Button {
              visible: root.status && Number(root.status.behind || 0) > 0
              text: "Pull"
              iconText: "󰁅"
              tooltipText: "Merge the commits origin has into the local clone"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: !root.busy
              onClicked: root.pullRemote()
            }
          }

          Text {
            visible: !!(root.status && root.status.fetch_error)
            width: parent.width
            textFormat: Text.PlainText
            text: "Fetch: " + root.status.fetch_error
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

      }

      Column {
        width: overviewRow.colW
        spacing: Style.space(12)

          // Recreate a machine exactly: everything from one side, one press.
          CardBox {
            visible: root.syncState !== "empty"

            GuideStep {
              step: "󰆏"
              title: "Mirror (recreate exactly)"
              body: "Everything, not just the ticked items: bindings.lua as a whole file, every plugin, hook and bin tool, the theme, and machine-local files like the display layout. Files that exist only on the receiving side are left alone."
            }
            Flow {
              width: parent.width
              spacing: Style.space(8)
              Button {
                text: "Mirror onto this machine"
                iconText: "󰁨"
                tooltipText: "Make this machine an exact copy of the repo. A backup is written first."
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                enabled: !root.busy
                onClicked: root.confirmKind = "mirror-repo"
              }
              Button {
                text: "Mirror this machine to repo"
                iconText: "󰓂"
                tooltipText: "Push everything on this machine into the repo"
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                enabled: !root.busy
                onClicked: root.confirmKind = "mirror-local"
              }
            }
          }

      }

      Column {
        width: overviewRow.colW
        spacing: Style.space(12)

          CardBox {
            Column {
              width: parent.width
              spacing: Style.space(6)

              Item {
                width: parent.width
                implicitHeight: Math.max(remoteLabel.implicitHeight, remoteVal.implicitHeight, editRepoBtn.implicitHeight)
                visible: !root.editingRepo

                Text {
                  id: remoteLabel
                  textFormat: Text.PlainText
                  text: "Remote"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  anchors.left: parent.left
                  anchors.verticalCenter: parent.verticalCenter
                  width: Math.min(Style.space(140), parent.width * 0.28)
                }
                Text {
                  id: remoteVal
                  textFormat: Text.PlainText
                  text: String(root.status.repo_url || "—")
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideMiddle
                  anchors.left: remoteLabel.right
                  anchors.leftMargin: Style.space(8)
                  anchors.right: editRepoBtn.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                }
                Button {
                  id: editRepoBtn
                  text: "Edit"
                  tooltipText: "Use a different git repo"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  fontSize: Style.font.caption
                  enabled: !root.busy
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  onClicked: root.startEditRepo()
                }
              }

              Column {
                visible: root.editingRepo
                width: parent.width
                spacing: Style.space(8)
                onVisibleChanged: if (visible) repoEditField.forceActiveFocus()

                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: "Git repo"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
                TextField {
                  id: repoEditField
                  width: parent.width
                  placeholderText: "https://github.com/you/omarchy-config.git"
                  text: root.repoUrlInput
                  foreground: root.foreground
                  font.family: root.fontFamily
                  enabled: !root.busy
                  onTextChanged: root.repoUrlInput = text
                  onAccepted: root.saveEditRepo()
                }
                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: "HTTPS, SSH, owner/repo, or a local path. Empty private repos can be seeded from this machine."
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
                Row {
                  spacing: Style.space(8)
                  Button {
                    text: root.busy ? "Switching…" : "Save"
                    bordered: true
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                    enabled: !root.busy && String(root.repoUrlInput).trim() !== ""
                    onClicked: root.saveEditRepo()
                  }
                  Button {
                    text: "Cancel"
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                    enabled: !root.busy
                    onClicked: root.cancelEditRepo()
                  }
                }
              }
            }
            TablePair { label: "Branch"; value: String((root.status.branch || "—") + (root.status.head ? " @ " + root.status.head : "")) }
            TablePair { label: "Ahead / behind"; value: String(root.status.ahead || 0) + " / " + String(root.status.behind || 0) }
            TablePair { label: "Last apply"; value: Model.relativeAgo(root.status.last_apply_at) }
            TablePair { label: "Last publish"; value: Model.relativeAgo(root.status.last_publish_at) }
            TablePair { label: "Plugin"; value: "config-sync " + String((root.status && root.status.plugin_version) || "") }
            TablePair {
              label: "Theme"
              value: {
                if (!root.inspect || !root.inspect.theme) return "—"
                var t = root.inspect.theme
                var name = t.display || t.slug || "—"
                return t.custom ? (name + " (custom overlay)") : name
              }
            }
            TablePair { label: "Bar position"; value: root.inspect && root.inspect.bar ? String(root.inspect.bar.position || "—") : "—" }
            TablePair {
              label: "Idle lock"
              value: root.inspect && root.inspect.idle && root.inspect.idle.lock
                ? (Number(root.inspect.idle.lock) / 60) + " min"
                : "—"
            }
          }

      }
    }
  }

  Component {
    id: tabChangesComp
    Column {
      width: parent.width
      spacing: Style.space(10)

      // One toolbar row: bulk picks, the two actions, and machine-local.
      Flow {
        width: parent.width
        spacing: Style.space(6)
        visible: !root.showingHidden

        Button {
          text: "Select incoming"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.bulkPick("in")
        }
        Button {
          text: "Select local"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.bulkPick("out")
        }
        Button {
          text: "Select all"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.bulkPick("all")
        }
        Button {
          text: "Clear"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.bulkPick("none")
        }
        Button {
          text: "Machine-local " + (root.includeMachine ? "on" : "off")
          iconText: "󰍹"
          tooltipText: "Display layout (hypr/monitors.lua) and machine_local paths in .omarchy-config.json stay on this machine unless this is on."
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          selected: root.includeMachine
          onClicked: {
            root.includeMachine = !root.includeMachine
            Qt.callLater(function() { root.seedPicks() })
          }
        }
        Button {
          text: "Hidden (" + root.hiddenCount + ")"
          iconText: "󰈉"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.showingHidden = true
        }
        Button {
          visible: root.syncState !== "empty"
          text: "Apply selected"
          iconText: "󰁨"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          enabled: !root.busy
          onClicked: root.requestApply()
        }
        Button {
          text: root.syncState === "empty" ? ("Seed repo (" + root.outgoingPicked + " items)") : "Publish selected"
          iconText: "󰓂"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          selected: root.syncState === "empty"
          enabled: !root.busy
          onClicked: root.requestPublish()
        }
      }

      Text {
        visible: !root.showingHidden && root.syncState === "empty"
        width: parent.width
        textFormat: Text.PlainText
        text: "First push: every ticked item under Outgoing becomes the repo's first commit. Untick anything you do not want on GitHub, then Seed repo."
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Text {
        visible: !root.showingHidden && root.unresolvedBoth > 0
        width: parent.width
        textFormat: Text.PlainText
        text: "Checked items that changed on both sides still need Keep local or Take repo."
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      PanelSectionHeader {
        visible: !root.showingHidden && root.conflictFiles.length > 0
        text: "GIT CONFLICTS (" + root.conflictFiles.length + ")"
        foreground: root.foreground
        fontFamily: root.fontFamily
      }
      FitGrid {
        id: conflictGrid
        visible: !root.showingHidden && root.conflictFiles.length > 0
        width: parent.width
        count: root.conflictFiles.length
        share: 0.35
        minCellWidth: Style.space(520)
        Repeater {
          model: root.showingHidden ? [] : root.conflictFiles
          FileRow {
            required property var modelData
            width: conflictGrid.cellWidth
            height: conflictGrid.cellHeight
            pathLabel: String(modelData)
            summary: "Unmerged path"
            statusLabel: "Conflict"
            extra: conflictButtons
            property Component conflictButtons: Row {
              spacing: Style.space(4)
              Button {
                text: "Keep local"
                fontSize: Style.font.caption
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.resolveConflict(String(modelData), "ours")
              }
              Button {
                text: "Take incoming"
                fontSize: Style.font.caption
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: root.resolveConflict(String(modelData), "theirs")
              }
            }
          }
        }
      }

      // Incoming / Outgoing / Both sit side by side; one is open below.
      Row {
        id: sectionTabs
        visible: !root.showingHidden && root.changeSections.length > 0
        width: parent.width
        spacing: Style.space(8)
        Repeater {
          model: root.changeSections
          SectionTab {
            required property var modelData
            width: (sectionTabs.width - sectionTabs.spacing * (root.changeSections.length - 1)) / Math.max(1, root.changeSections.length)
            section: modelData
          }
        }
      }

      Flow {
        visible: !root.showingHidden && root.activeSection !== null
        width: parent.width
        spacing: Style.space(6)
        Repeater {
          model: root.activeChips
          Button {
            required property var modelData
            text: modelData.label + " " + modelData.count
            fontSize: Style.font.caption
            foreground: root.foreground
            fontFamily: root.fontFamily
            bordered: true
            selected: root.activeChip === modelData.label
            onClicked: root.openChip = modelData.label
          }
        }
        Item { width: Style.space(12); height: 1; visible: root.activeSection !== null && root.activeSection.bulk }
        Button {
          visible: root.activeSection !== null && root.activeSection.bulk
          text: "Select all (" + (root.activeSection ? root.activeSection.items.length : 0) + ")"
          iconText: "󰒆"
          tooltipText: "Tick every item in this group"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.pickItems(root.activeSection.items, true)
        }
        Button {
          visible: root.activeSection !== null && root.activeSection.bulk
          text: "Select none"
          iconText: "󰒇"
          tooltipText: "Untick every item in this group"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.pickItems(root.activeSection.items, false)
        }
      }

      FitGrid {
        id: changeGrid
        visible: !root.showingHidden && root.activeSection !== null
        width: parent.width
        count: root.shownChangeItems.length
        minCellWidth: root.activeSection && root.activeSection.key === "both" ? Style.space(520) : Style.space(340)
        Repeater {
          model: root.showingHidden ? [] : root.shownChangeItems
          ChangeRow {
            required property var modelData
            width: changeGrid.cellWidth
            height: changeGrid.cellHeight
            item: modelData
          }
        }
      }

      Column {
        visible: !root.showingHidden && !root.hasReviewable
        width: parent.width
        spacing: Style.space(8)
        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: root.hiddenCount > 0
            ? ("No active config differences (" + root.hiddenCount + " ignored).")
            : "No portable config differences. This machine matches the repo."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          horizontalAlignment: Text.AlignHCenter
        }
      }

      // Hidden changes
      Row {
        visible: root.showingHidden
        width: parent.width
        spacing: Style.space(8)
        Text {
          width: parent.width - unhideAllBtn.width - backBtn.width - parent.spacing * 2
          textFormat: Text.PlainText
          text: "HIDDEN SYNCS (" + root.hiddenCount + ") · ignored, will not sync. Unhide any item to restore it."
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          elide: Text.ElideRight
          anchors.verticalCenter: parent.verticalCenter
        }
        Button {
          id: unhideAllBtn
          text: "Unhide all"
          iconText: "󰈈"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          enabled: !root.busy && root.hiddenCount > 0
          bordered: true
          onClicked: root.unhideAll()
        }
        Button {
          id: backBtn
          text: "Active changes"
          iconText: "󰦓"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.showingHidden = false
        }
      }

      Text {
        visible: root.showingHidden && root.hiddenCount === 0
        width: parent.width
        textFormat: Text.PlainText
        text: "No hidden changes. Press Hide on any incoming or outgoing change to ignore it."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        horizontalAlignment: Text.AlignHCenter
      }

      FitGrid {
        id: hiddenGrid
        visible: root.showingHidden && root.hiddenCount > 0
        width: parent.width
        count: root.hiddenItems.length
        Repeater {
          model: root.showingHidden ? root.hiddenItems : []
          FileRow {
            required property var modelData
            width: hiddenGrid.cellWidth
            height: hiddenGrid.cellHeight
            clickable: false
            pathLabel: (modelData.typeLabel ? modelData.typeLabel + " · " : "") + modelData.label
            summary: Model.statusPrefix(Model.fileStatusLabel(modelData.status, modelData.removal), String(modelData.summary || "")) + String(modelData.summary || "")
            extra: unhideBtn
            property Component unhideBtn: Button {
              text: "Unhide"
              iconText: "󰈈"
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: !root.busy
              onClicked: root.unhideItem(modelData.kind, modelData.itemId)
            }
          }
        }
      }
    }
  }

  Component {
    id: shortcutsExtraComp
    Column {
      width: parent.width
      spacing: Style.space(6)
      Row {
        spacing: Style.space(8)
        PanelSectionHeader { text: "KEYBOARD BINDINGS (" + (root.inspect && root.inspect.shortcuts ? root.inspect.shortcuts.length : 0) + ")"; foreground: root.foreground; fontFamily: root.fontFamily }
        Button {
          text: "bindings.lua"
          iconText: "󰉋"
          tooltipText: "Open hypr/bindings.lua"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.openFile("hypr/bindings.lua", "", "")
        }
        Button {
          iconText: "󰞷"
          tooltipText: "Open hypr/bindings.lua in a terminal"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.openTerminal("hypr/bindings.lua", "", "")
        }
      }
      Text {
        visible: !root.inspect || !root.inspect.shortcuts || root.inspect.shortcuts.length === 0
        textFormat: Text.PlainText
        text: "No o.bind() shortcuts found in hypr/bindings.lua."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      FitGrid {
        id: shortcutGrid
        besideWidth: root.railWidth + root.railGap
        width: parent.width
        count: root.inspect && root.inspect.shortcuts ? root.inspect.shortcuts.length : 0
        cellHeight: Style.space(36)
        minCellWidth: Style.space(220)
        Repeater {
          model: root.inspect && root.inspect.shortcuts ? root.inspect.shortcuts : []
          FileRow {
            required property var modelData
            width: shortcutGrid.cellWidth
            height: shortcutGrid.cellHeight
            clickable: false
            clickToOpen: true
            openPath: "hypr/bindings.lua"
            pathLabel: String(modelData.keys || "")
            summary: String(modelData.label || "")
          }
        }
      }
    }
  }

  Component {
    id: pluginsExtraComp
    Column {
      width: parent.width
      spacing: Style.space(6)
      PanelSectionHeader { text: "INSTALLED PLUGINS (" + (root.inspect && root.inspect.plugins ? root.inspect.plugins.length : 0) + ")"; foreground: root.foreground; fontFamily: root.fontFamily }
      Text {
        visible: !root.inspect || !root.inspect.plugins || root.inspect.plugins.length === 0
        textFormat: Text.PlainText
        text: "No extra plugins in plugins/."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      FitGrid {
        id: pluginGrid
        besideWidth: root.railWidth + root.railGap
        width: parent.width
        count: root.inspect && root.inspect.plugins ? root.inspect.plugins.length : 0
        cellHeight: Style.space(36)
        minCellWidth: Style.space(220)
        reserve: barLayoutCol.implicitHeight + Style.space(12)
        Repeater {
          model: root.inspect && root.inspect.plugins ? root.inspect.plugins : []
          FileRow {
            required property var modelData
            width: pluginGrid.cellWidth
            height: pluginGrid.cellHeight
            clickable: false
            clickToOpen: true
            pathLabel: String(modelData.name || modelData.id)
            openPath: "plugins/" + modelData.id
            summary: (modelData.version ? modelData.version + " · " : "") + String(modelData.description || modelData.id)
              + (modelData.git && modelData.source ? " · git: " + modelData.source : "")
              + (modelData.git && !modelData.installed ? " · not installed here" : "")
            extra: (!!modelData.git && !modelData.installed && String(modelData.source || "") !== "") ? pluginInstallBtn : null
            property Component pluginInstallBtn: Button {
              text: "Install"
              iconText: "󰏗"
              tooltipText: "Open Omarchy's plugin installer in a terminal"
              bordered: true
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.busy
              onClicked: root.runPluginAction("install", String(modelData.id))
            }
          }
        }
      }

      Column {
        id: barLayoutCol
        width: parent.width
        spacing: Style.space(4)
        PanelSectionHeader {
          visible: !!(root.inspect && root.inspect.bar)
          text: "BAR LAYOUT"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }
        Repeater {
          model: ["left", "center", "right"]
          Text {
            required property var modelData
            width: barLayoutCol.width
            visible: !!(root.inspect && root.inspect.bar && root.inspect.bar.widgets && (root.inspect.bar.widgets[modelData] || []).length > 0)
            textFormat: Text.PlainText
            text: modelData.toUpperCase() + "  " + (root.inspect && root.inspect.bar && root.inspect.bar.widgets ? (root.inspect.bar.widgets[modelData] || []).join("  ·  ") : "")
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }

  Component {
    id: hooksExtraComp
    Column {
      width: parent.width
      spacing: Style.space(6)
      PanelSectionHeader { text: "HOOKS (" + (root.inspect && root.inspect.hooks ? root.inspect.hooks.length : 0) + ")"; foreground: root.foreground; fontFamily: root.fontFamily }
      Text {
        visible: !root.inspect || !root.inspect.hooks || root.inspect.hooks.length === 0
        textFormat: Text.PlainText
        text: "No event hooks in omarchy/hooks/."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      FitGrid {
        id: hookGrid
        besideWidth: root.railWidth + root.railGap
        width: parent.width
        count: root.inspect && root.inspect.hooks ? root.inspect.hooks.length : 0
        Repeater {
          model: root.inspect && root.inspect.hooks ? root.inspect.hooks : []
          FileRow {
            required property var modelData
            width: hookGrid.cellWidth
            height: hookGrid.cellHeight
            pathLabel: modelData.event + "/" + modelData.name
            localPath: "~/.config/omarchy/hooks/" + modelData.event + ".d/" + modelData.name
            summary: modelData.sample ? "Sample hook script" : "Active hook script"
            statusLabel: modelData.sample ? "Sample" : "Hook"
          }
        }
      }
    }
  }

  Component {
    id: binsExtraComp
    Column {
      width: parent.width
      spacing: Style.space(6)
      PanelSectionHeader { text: "HELPER SCRIPTS (" + (root.inspect && root.inspect.bins ? root.inspect.bins.length : 0) + ")"; foreground: root.foreground; fontFamily: root.fontFamily }
      Text {
        visible: !root.inspect || !root.inspect.bins || root.inspect.bins.length === 0
        textFormat: Text.PlainText
        text: "No scripts in bin/."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      FitGrid {
        id: binGrid
        besideWidth: root.railWidth + root.railGap
        width: parent.width
        count: root.inspect && root.inspect.bins ? root.inspect.bins.length : 0
        Repeater {
          model: root.inspect && root.inspect.bins ? root.inspect.bins : []
          FileRow {
            required property var modelData
            width: binGrid.cellWidth
            height: binGrid.cellHeight
            pathLabel: "bin/" + String(modelData)
            localPath: "~/.local/bin/" + String(modelData)
            summary: "Custom script in ~/.local/bin/"
            statusLabel: "Script"
          }
        }
      }
    }
  }

  Component {
    id: tabConfigsComp
    Row {
      width: parent.width
      spacing: root.railGap

      // Categories stacked top to bottom; pressing one opens it beside.
      Column {
        id: rail
        width: root.railWidth
        spacing: Style.space(6)
        Repeater {
          model: root.configCategories
          CategoryTile {
            required property var modelData
            width: rail.width
            category: modelData
          }
        }
      }

      Column {
        width: parent.width - root.railWidth - root.railGap
        spacing: Style.space(10)

        Text {
          visible: root.activeCategory !== null
          width: parent.width
          textFormat: Text.PlainText
          text: root.activeCategory ? (root.activeCategory.title + " · " + root.activeCategory.subtitle) : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        PanelSectionHeader {
          visible: root.activeCategory !== null && root.activeCategory.changeItems.length > 0
          text: "PENDING CHANGES (" + (root.activeCategory ? root.activeCategory.changeItems.length : 0) + ")"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }
        FitGrid {
          id: catChangeGrid
          besideWidth: root.railWidth + root.railGap
          visible: root.activeCategory !== null && root.activeCategory.changeItems.length > 0
          width: parent.width
          count: root.activeCategory ? root.activeCategory.changeItems.length : 0
          // Leave room for the tracked-files or settings list below.
          share: root.activeCategory && (root.activeCategory.extra || root.activeCategory.files.length > 0) ? 0.4 : 1
          Repeater {
            model: root.activeCategory ? root.activeCategory.changeItems : []
            ChangeRow {
              required property var modelData
              width: catChangeGrid.cellWidth
              height: catChangeGrid.cellHeight
              item: modelData
            }
          }
        }

        Loader {
          visible: root.activeCategory !== null && !!root.activeCategory.extra
          width: parent.width
          sourceComponent: root.activeCategory && root.activeCategory.extra === "shortcuts" ? shortcutsExtraComp
            : root.activeCategory && root.activeCategory.extra === "plugins" ? pluginsExtraComp
            : root.activeCategory && root.activeCategory.extra === "hooks" ? hooksExtraComp
            : root.activeCategory && root.activeCategory.extra === "bins" ? binsExtraComp
            : null
        }

        PanelSectionHeader {
          visible: root.activeCategory !== null && !root.activeCategory.extra && root.activeCategory.files.length > 0
          text: "TRACKED FILES (" + (root.activeCategory ? root.activeCategory.files.length : 0) + ")"
          foreground: root.foreground
          fontFamily: root.fontFamily
        }
        FitGrid {
          id: trackedGrid
          besideWidth: root.railWidth + root.railGap
          visible: root.activeCategory !== null && !root.activeCategory.extra && root.activeCategory.files.length > 0
          width: parent.width
          count: root.activeCategory && !root.activeCategory.extra ? root.activeCategory.files.length : 0
          Repeater {
            model: root.activeCategory && !root.activeCategory.extra ? root.activeCategory.files : []
            FileRow {
              required property var modelData
              width: trackedGrid.cellWidth
              height: trackedGrid.cellHeight
              pathLabel: modelData.path
              localPath: modelData.local_path || ""
              repoPath: modelData.repo_path || ""
              summary: modelData.summary
              statusLabel: Model.fileStatusLabel(modelData.status, modelData.removal) + (modelData.portable ? "" : " · Machine-specific")
            }
          }
        }
      }
    }
  }
  component GuideStep: Row {
    property string step: ""
    property string title: ""
    property string body: ""
    width: parent ? parent.width : 100
    spacing: Style.space(10)

    Rectangle {
      width: Style.space(22)
      height: Style.space(22)
      radius: width / 2
      color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
      anchors.top: parent.top
      Text {
        textFormat: Text.PlainText
        anchors.centerIn: parent
        text: step
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: true
      }
    }

    Column {
      width: parent.width - Style.space(32)
      spacing: Style.space(3)
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: title
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        wrapMode: Text.WordWrap
      }
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: body
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }

  // Lays cells out in as many columns as it takes for every one to fit the
  // screen below this grid's top edge. The panel never scrolls: a long list
  // gets wider, not taller.
  // Fills top to bottom, and adds a column only when the next row would not
  // fit on screen below this grid. The panel never scrolls: a list that runs
  // out of height gets wider, and the panel widens with it.
  component FitGrid: Grid {
    id: fitGrid
    property int count: 0
    property real cellHeight: Style.space(48)
    property real minCellWidth: Style.space(340)
    // Fraction of the remaining height this grid may use (a second list below
    // it gets the rest), pixels to keep free after it, and width beside it
    // (a sidebar) that the panel must also hold.
    property real share: 1
    property real reserve: 0
    property real besideWidth: 0
    property real topY: 0
    property string needKey: ""
    readonly property real budget: Math.max(cellHeight, (root.screenBudget - topY - reserve - Style.space(8)) * share)
    readonly property int fitRows: Math.max(1, Math.floor((budget + rowSpacing) / (cellHeight + rowSpacing)))
    // Only rows is set: a top-to-bottom Grid derives its columns from it.
    readonly property int cols: Math.max(1, Math.ceil(count / fitRows))
    readonly property real naturalWidth: cols * minCellWidth + (cols - 1) * columnSpacing
    readonly property real cellWidth: (width - columnSpacing * (cols - 1)) / cols
    flow: Grid.TopToBottom
    rows: Math.max(1, Math.ceil(count / cols))
    rowSpacing: Style.space(6)
    columnSpacing: Style.space(6)

    function measure() {
      if (!fitGrid.visible || !root.bodyItem) return
      var y = fitGrid.mapToItem(root.bodyItem, 0, 0).y
      if (Math.abs(y - topY) > 1) topY = y
    }
    function report() {
      root.reportWidth(needKey, fitGrid.visible && count > 0 ? naturalWidth + besideWidth : 0)
    }
    onYChanged: Qt.callLater(measure)
    onVisibleChanged: { Qt.callLater(measure); report() }
    onCountChanged: Qt.callLater(measure)
    onNaturalWidthChanged: report()
    Component.onCompleted: {
      root.widthSeq += 1
      needKey = "grid" + root.widthSeq
      report()
      Qt.callLater(measure)
    }
    Component.onDestruction: if (root) root.reportWidth(needKey, 0)
    Connections {
      target: root
      function onLayoutTickChanged() { Qt.callLater(fitGrid.measure) }
    }
    // Loaders and wrapping text above settle a frame or two later than this
    // grid; keep measuring while it is on screen (cheap, and only rebinds on change).
    Timer {
      interval: 200
      repeat: true
      running: fitGrid.visible && root.opened
      onTriggered: fitGrid.measure()
    }
  }

  // One change, one fixed-height cell: tick box, name, status line, actions.
  component ChangeRow: Rectangle {
    id: cr
    property var item: ({})
    readonly property string rowKind: String(item.kind || "f")
    readonly property string rowId: String(item.itemId || "")
    readonly property bool rowBoth: !!item.both
    readonly property bool included: !!(root.picks[root.pickId(rowKind, rowId)])
    readonly property string bothKey: rowKind === "f" ? rowId : (rowKind + ":" + rowId)
    readonly property bool pickable: item.pickable !== false
    readonly property string rowAction: String(item.action || "")

    radius: Style.cornerRadius
    color: included ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12) : root.cardBg
    border.width: included ? 2 : 1
    border.color: included ? root.accent : root.cardBorder

    MouseArea {
      anchors.fill: parent
      enabled: cr.pickable
      cursorShape: Qt.PointingHandCursor
      onClicked: root.togglePick(cr.rowKind, cr.rowId)
    }

    Rectangle {
      id: crBox
      visible: cr.pickable
      width: Style.space(20)
      height: Style.space(20)
      radius: 4
      anchors.left: parent.left
      anchors.leftMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      color: cr.included ? root.accent : Color.background
      border.width: 2
      border.color: cr.included ? root.accent : root.foreground
      Text {
        textFormat: Text.PlainText
        anchors.centerIn: parent
        text: cr.included ? "✓" : ""
        color: Color.background
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }
    }

    Column {
      anchors.left: cr.pickable ? crBox.right : parent.left
      anchors.leftMargin: Style.space(8)
      anchors.right: crActions.left
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: 1
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: (cr.item.typeLabel ? cr.item.typeLabel + " · " : "") + String(cr.item.label || "")
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideMiddle
      }
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: {
          var st = Model.fileStatusLabel(cr.item.status, cr.item.removal)
          var sum = String(cr.item.summary || "")
          var changes = cr.item.changes && cr.item.changes.length ? " · " + cr.item.changes.join(" · ") : ""
          var tail = !cr.pickable ? "" : (cr.included ? (cr.item.removal ? " · will delete" : " · will sync") : " · skipped")
          return Model.statusPrefix(st, sum) + sum + changes + tail
        }
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    Row {
      id: crActions
      anchors.right: parent.right
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(4)
      Button {
        visible: cr.rowBoth
        text: "Keep local"
        fontSize: Style.font.caption
        foreground: root.foreground
        fontFamily: root.fontFamily
        selected: root.bothPicks[cr.bothKey] === "local"
        bordered: true
        onClicked: root.selectSide(cr.rowKind, cr.rowId, "local")
      }
      Button {
        visible: cr.rowBoth
        text: "Take repo"
        fontSize: Style.font.caption
        foreground: root.foreground
        fontFamily: root.fontFamily
        selected: root.bothPicks[cr.bothKey] === "repo"
        bordered: true
        onClicked: root.selectSide(cr.rowKind, cr.rowId, "repo")
      }
      Button {
        visible: cr.rowAction !== ""
        text: cr.rowAction === "update" ? "Update" : "Install"
        iconText: cr.rowAction === "update" ? "󰚰" : "󰏗"
        tooltipText: cr.rowAction === "update"
          ? "Open Omarchy's plugin updater in a terminal"
          : "Open Omarchy's plugin installer in a terminal"
        fontSize: Style.font.caption
        foreground: root.foreground
        fontFamily: root.fontFamily
        bordered: true
        enabled: !root.busy
        onClicked: root.runPluginAction(cr.rowAction, cr.rowId)
      }
      Button {
        iconText: "󰈉"
        tooltipText: "Hide this change so it doesn't bother you"
        fontSize: Style.font.caption
        foreground: root.foreground
        fontFamily: root.fontFamily
        enabled: !root.busy
        onClicked: root.hideItem(cr.rowKind, cr.rowId)
      }
    }
  }

  // Header card for Incoming / Outgoing / Both; the selected one is open.
  component SectionTab: Rectangle {
    id: st
    property var section: ({})
    readonly property bool open: root.activeSection !== null && root.activeSection.key === section.key
    readonly property int picked: {
      var _ = root.picks
      return Model.pickedInItems(section.items || [], root.picks)
    }
    implicitHeight: stCol.implicitHeight + Style.space(14)
    radius: Style.cornerRadius
    color: stMa.containsMouse || open ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12) : root.cardBg
    border.width: open ? 2 : 1
    border.color: open ? root.accent : root.cardBorder

    Column {
      id: stCol
      anchors.left: parent.left
      anchors.right: stCount.left
      anchors.leftMargin: Style.space(12)
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      spacing: 2
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: String(st.section.title || "")
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        elide: Text.ElideRight
      }
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: String(st.section.subtitle || "") + (st.section.bulk ? " · " + st.picked + " included" : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }
    Text {
      id: stCount
      anchors.right: parent.right
      anchors.rightMargin: Style.space(12)
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: String((st.section.items || []).length)
      color: root.accent
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
      font.bold: true
    }
    MouseArea {
      id: stMa
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: {
        root.openSection = st.section.key
        root.openChip = ""
      }
    }
  }

  // One Configs category; pressing it opens its detail below the tiles.
  component CategoryTile: Rectangle {
    id: ct
    property var category: ({})
    readonly property bool open: root.activeCategory !== null && root.activeCategory.id === category.id
    readonly property int changes: (category.changeItems || []).length
    implicitHeight: ctRow.implicitHeight + Style.space(14)
    radius: Style.cornerRadius
    color: ctMa.containsMouse || open ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12) : root.cardBg
    border.width: open ? 2 : 1
    border.color: open ? root.accent : root.cardBorder

    Row {
      id: ctRow
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(8)
      Text {
        textFormat: Text.PlainText
        text: String(ct.category.icon || "")
        color: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        anchors.verticalCenter: parent.verticalCenter
      }
      Column {
        width: parent.width - Style.space(28)
        spacing: 1
        anchors.verticalCenter: parent.verticalCenter
        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: String(ct.category.title || "")
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          elide: Text.ElideRight
        }
        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: ct.changes > 0
            ? (ct.changes + (ct.changes === 1 ? " change" : " changes"))
            : (ct.category.total + (ct.category.total === 1 ? " item" : " items") + " · in sync")
          color: ct.changes > 0 ? root.accent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }
    }
    MouseArea {
      id: ctMa
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.openCategory = ct.category.id
    }
  }

  component FileRow: Rectangle {
    id: fileRowRoot
    property string pathLabel: ""
    property string openPath: pathLabel
    property string localPath: ""
    property string repoPath: ""
    property string summary: ""
    property string statusLabel: ""
    property Component extra: Item { width: 0; height: 1 }
    property bool clickable: true
    // Dense grids: the whole cell opens the file, no per-row buttons.
    property bool clickToOpen: false
    width: parent ? parent.width : 100
    implicitHeight: Math.max(fileCol.implicitHeight, extraLoader.implicitHeight) + Style.space(12)
    radius: Style.cornerRadius
    color: root.cardBg
    border.width: 1
    border.color: root.cardBorder

    MouseArea {
      anchors.fill: parent
      enabled: fileRowRoot.clickToOpen
      hoverEnabled: fileRowRoot.clickToOpen
      cursorShape: fileRowRoot.clickToOpen ? Qt.PointingHandCursor : Qt.ArrowCursor
      onClicked: root.openFile(fileRowRoot.openPath, fileRowRoot.localPath, fileRowRoot.repoPath)
    }

    Row {
      anchors.fill: parent
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(8)
      spacing: Style.space(6)

      Column {
        id: fileCol
        width: parent.width - (extraLoader.item ? extraLoader.width + parent.spacing : 0) - (fileRowRoot.clickable ? actionButtons.width + parent.spacing : 0)
        anchors.verticalCenter: parent.verticalCenter
        spacing: 2
        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: fileRowRoot.pathLabel
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          elide: Text.ElideMiddle
        }
        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: fileRowRoot.summary + (fileRowRoot.statusLabel ? " · " + fileRowRoot.statusLabel : "")
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Loader {
        id: extraLoader
        anchors.verticalCenter: parent.verticalCenter
        sourceComponent: fileRowRoot.extra
      }

      Row {
        id: actionButtons
        visible: fileRowRoot.clickable
        spacing: Style.space(4)
        anchors.verticalCenter: parent.verticalCenter

        Rectangle {
          id: fmBtn
          width: Style.space(30)
          height: Style.space(28)
          radius: Style.cornerRadius > 0 ? Style.space(4) : 0
          color: fmMa.containsMouse ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06)
          border.width: 1
          border.color: fmMa.containsMouse ? root.accent : root.cardBorder

          Text {
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: "󰉋"
            color: fmMa.containsMouse ? root.accent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          MouseArea {
            id: fmMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.openFile(fileRowRoot.openPath, fileRowRoot.localPath, fileRowRoot.repoPath)
          }
        }

        Rectangle {
          id: termBtn
          width: Style.space(30)
          height: Style.space(28)
          radius: Style.cornerRadius > 0 ? Style.space(4) : 0
          color: termMa.containsMouse ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06)
          border.width: 1
          border.color: termMa.containsMouse ? root.accent : root.cardBorder

          Text {
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: "󰞷"
            color: termMa.containsMouse ? root.accent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          MouseArea {
            id: termMa
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.openTerminal(fileRowRoot.openPath, fileRowRoot.localPath, fileRowRoot.repoPath)
          }
        }
      }
    }
  }

  component QuickPill: Rectangle {
    property string icon: ""
    property string label: ""
    property string value: ""
    property color highlightColor: root.foreground
    implicitHeight: Style.space(42)
    radius: Style.cornerRadius
    color: root.cardBg
    border.width: 1
    border.color: root.cardBorder
    Column {
      anchors.centerIn: parent
      spacing: 1
      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(4)
        Text {
          textFormat: Text.PlainText
          text: icon
          color: highlightColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
        Text {
          textFormat: Text.PlainText
          text: value
          color: highlightColor
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
      }
      Text {
        textFormat: Text.PlainText
        anchors.horizontalCenter: parent.horizontalCenter
        text: label
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption - 2
      }
    }
  }

  component CardBox: Rectangle {
    default property alias content: innerCol.children
    width: parent.width
    implicitHeight: innerCol.implicitHeight + Style.space(16)
    radius: Style.cornerRadius
    color: root.cardBg
    border.width: 1
    border.color: root.cardBorder

    Column {
      id: innerCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.margins: Style.space(8)
      spacing: Style.space(6)
    }
  }

  component TablePair: Item {
    property string label: ""
    property string value: ""
    width: parent.width
    implicitHeight: Math.max(pairLabel.implicitHeight, pairVal.implicitHeight)
    Text {
      id: pairLabel
      textFormat: Text.PlainText
      text: label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      anchors.left: parent.left
      anchors.top: parent.top
      width: Math.min(Style.space(140), parent.width * 0.34)
      wrapMode: Text.WordWrap
    }
    Text {
      id: pairVal
      textFormat: Text.PlainText
      text: value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      anchors.left: pairLabel.right
      anchors.leftMargin: Style.space(8)
      anchors.right: parent.right
      wrapMode: Text.WordWrap
    }
  }
}
