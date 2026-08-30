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
  property var themeDiff: null
  property string lastNotifiedKey: ""
  property bool openOnChanges: false

  readonly property bool configured: !!(status && status.configured)
  readonly property string syncState: String((status && status.sync_state) || (configured ? "in-sync" : "not-configured"))
  readonly property bool alarming: syncState === "conflicts" || syncState === "diverged" || syncState === "invalid"
  readonly property bool pending: syncState === "ready" || syncState === "empty" || syncState === "remote-ahead" || syncState === "local-ahead" || alarming
  readonly property color stateColor: alarming ? urgent : (pending ? accent : foreground)
  readonly property var tabs: [
    { name: "Overview", icon: "󰘿" },
    { name: "Changes", icon: "󰦓" },
    { name: "Shortcuts", icon: "󰌌" },
    { name: "Plugins", icon: "󰐱" },
    { name: "Configs", icon: "󰒓" }
  ]

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
      if (root.isBundledPath(f.path)) continue
      if (f.group === "theme" && f.path !== "omarchy/theme.name") continue
      if (f.path === "hypr/bindings.lua") continue
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
  readonly property bool hasReviewable: otherFiles.length + shortcutDiffs.length + bundleDiffs.length + pluginDiffs.length + conflictFiles.length + (themeDiff ? 1 : 0) > 0
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
      if (isPicked("p", bothPlugins[i].id) && !bothPicks["p:" + bothPlugins[i].id]) n++
    }
    for (i = 0; i < bothBundles.length; i++) {
      if (isPicked("g", bothBundles[i].id) && !bothPicks["g:" + bothBundles[i].id]) n++
    }
    if (themeDiff && themeDiff.status === "both" && isPicked("t", "selected") && !bothPicks["t:selected"]) n++
    return n
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
    run(["connect", url])
  }

  function cloneMap(obj) {
    var next = {}
    var keys = Object.keys(obj || {})
    for (var i = 0; i < keys.length; i++) next[keys[i]] = obj[keys[i]]
    return next
  }

  function pickId(kind, id) { return kind + ":" + id }

  function isBundledPath(path) {
    var p = String(path || "")
    if (p.indexOf("plugins/") === 0) return true
    if (p.indexOf("omarchy/hooks/") === 0) return true
    if (p.indexOf("omarchy/agents/") === 0) return true
    if (p.indexOf("omarchy/branding/") === 0) return true
    if (p.indexOf("omarchy/extensions/") === 0) return true
    if (p.indexOf("bin/") === 0) return true
    return false
  }

  function isPicked(kind, id) { return !!picks[pickId(kind, id)] }

  function togglePick(kind, id) {
    var key = pickId(kind, id)
    var next = cloneMap(picks)
    next[key] = !next[key]
    picks = next
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
      next[key] = (key in picks) ? picks[key] : !!(item.default_apply || item.default_publish || item.status === "differs")
    }
    for (i = 0; i < bundleDiffs.length; i++) {
      item = bundleDiffs[i]
      key = pickId("g", item.id)
      next[key] = (key in picks) ? picks[key] : !!(item.default_apply || item.default_publish || item.status === "differs")
    }
    if (themeDiff) {
      key = pickId("t", "selected")
      next[key] = (key in picks) ? picks[key] : !!(themeDiff.default_apply || themeDiff.default_publish || themeDiff.status === "differs")
    }
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

  function selectedApplyShortcuts() {
    var out = []
    var i, s
    for (i = 0; i < shortcutDiffs.length; i++) {
      s = shortcutDiffs[i]
      if (!isPicked("s", s.keys)) continue
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
      if (s.status === "both") {
        if (bothPicks["s:" + s.keys] === "local") out.push(s.keys)
        continue
      }
      if (s.status === "added-local" || s.status === "local") out.push(s.keys)
    }
    return out
  }

  function selectedApplyPlugins() {
    var out = []
    var i, b
    for (i = 0; i < bundleDiffs.length; i++) {
      b = bundleDiffs[i]
      if (b.kind !== "plugin" || !isPicked("g", b.id)) continue
      if (b.status === "both") {
        if (bothPicks["g:" + b.id] === "repo") out.push(b.plugin_id)
        continue
      }
      if (b.status === "added-repo" || b.status === "repo" || b.status === "differs") out.push(b.plugin_id)
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
    var i, b
    for (i = 0; i < bundleDiffs.length; i++) {
      b = bundleDiffs[i]
      if (b.kind !== "plugin" || !isPicked("g", b.id)) continue
      if (b.status === "both") {
        if (bothPicks["g:" + b.id] === "local") out.push(b.plugin_id)
        continue
      }
      if (b.status === "added-local" || b.status === "local" || b.status === "differs") out.push(b.plugin_id)
    }
    return out
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
    if (selectedPublishFiles().length + selectedPublishShortcuts().length + selectedPublishPlugins().length + selectedBundleFiles("publish").length === 0 && !selectedPublishTheme() && Number(status.ahead || 0) === 0) {
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
      run(["connect", String(repoUrlInput || "").trim()])
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

  function applySnapshot(data) {
    status = data.status || {}
    inspect = data.inspect || null
    diffFiles = (data.diff && data.diff.files) ? data.diff.files : []
    shortcutDiffs = (data.diff && data.diff.shortcuts) ? data.diff.shortcuts : []
    pluginDiffs = (data.diff && data.diff.plugins) ? data.diff.plugins : []
    bundleDiffs = (data.diff && data.diff.bundles) ? data.diff.bundles : []
    themeDiff = (data.diff && data.diff.theme) ? data.diff.theme : null
    if (data.sync_state && status)
      status = Object.assign({}, status, { sync_state: data.sync_state })
    if (!editingRepo && status.repo_url)
      repoUrlInput = String(status.repo_url)
    Qt.callLater(function() {
      root.seedPicks()
      if (root.openOnChanges && root.hasReviewable) {
        root.activeTab = 1
        root.openOnChanges = false
      } else {
        root.openOnChanges = false
      }
      root.maybeNotify()
    })
  }

  function maybeNotify() {
    if (!configured || !root.opened) return
    var key = syncState + ":" + String(status.head || "") + ":" + String(status.local_changes || 0) + ":" + String(status.repo_changes || 0)
    if (key === lastNotifiedKey) return
    if (syncState === "in-sync" || syncState === "not-configured") return
    if (syncState === "empty" && !root.opened) return
    lastNotifiedKey = key
    var title = Model.stateTitle(syncState)
    var body = Model.stateHint(syncState, status)
    notifyProc.command = ["omarchy-notification-send", "-u", alarming ? "critical" : "normal", "-g", "󰘿", title, body]
    notifyProc.running = true
  }

  function run(args) {
    if (syncProc.running) {
      pendingArgs = args
      return
    }
    busy = true
    lastError = ""
    pendingAction = args[0] || ""
    syncProc.command = ["python3", "-u", root.scriptPath].concat(args)
    syncProc.running = true
  }

  function handleOutput(text) {
    busy = false
    var raw = String(text || "").trim()
    if (!raw) {
      lastError = "The sync helper returned no output."
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
      if (data.conflicts) {
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
      status = { configured: false, sync_state: "not-configured" }
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
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.handleOutput(text)
        if (root.pendingArgs.length > 0) {
          var next = root.pendingArgs
          root.pendingArgs = []
          root.run(next)
        }
      }
    }
    stderr: StdioCollector { waitForEnd: true }
  }

  Process { id: notifyProc }

  IpcHandler {
    target: "gladimdim.config-sync"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(true); return "ok" }
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
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(Style.space(580))

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
        else if (t >= "1" && t <= "5") root.activeTab = parseInt(t) - 1
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

            Text {
              textFormat: Text.PlainText
              text: root.configured ? Model.repoName(root.status.repo_url) : "Config Sync"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
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

            Button {
              iconText: "󰑐"
              tooltipText: "Refresh (r)"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              enabled: !root.busy
              onClicked: root.refresh(true)
            }

            Button {
              visible: root.configured
              text: "Edit"
              tooltipText: "Use a different git repo"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              enabled: !root.busy
              onClicked: root.startEditRepo()
            }

            Button {
              visible: root.configured
              iconText: "󰅖"
              tooltipText: "Unlink this repo"
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
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
        Flickable {
          visible: !root.configured
          width: parent.width
          height: Math.max(80, panel.contentHeight - mainCol.spacing * 4 - heroInfo.implicitHeight - Style.space(36))
          contentWidth: width
          contentHeight: setupCol.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.VerticalFlick
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

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
              body: "HTTPS (https://github.com/you/omarchy-config.git), SSH, or owner/repo. An empty private repo is what you want on the first laptop. On the next laptop, paste this same URL and Apply."
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
                text: "Use this laptop's clone"
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
              title: "Review, then Publish this laptop"
              body: "Empty repo: the tabs show this machine. Publish seeds GitHub (still private). Next laptop: Connect the same URL and press Apply. Display layout is skipped unless you opt in."
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

        Flickable {
          id: scrollArea
          visible: root.configured
          width: parent.width
          height: Math.max(80, panel.contentHeight - mainCol.spacing * 6 - heroInfo.implicitHeight - tabRow.height - Style.space(70))
          contentWidth: width
          contentHeight: loader.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.VerticalFlick
          ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

          Loader {
            id: loader
            width: parent.width
            sourceComponent: {
              if (root.activeTab === 1) return tabChangesComp
              if (root.activeTab === 2) return tabShortcutsComp
              if (root.activeTab === 3) return tabPluginsComp
              if (root.activeTab === 4) return tabConfigsComp
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
                ? "Apply the checked incoming shortcuts, plugins, and files onto this laptop? A timestamped backup is written first."
                : root.confirmKind === "publish"
                  ? (root.syncState === "empty"
                    ? "Seed this private GitHub repo with the checked items from this laptop, then push? Keep the repo private so shortcuts, hooks, and scripts are not public."
                    : "Copy the checked local shortcuts, plugins, and files into the repo, commit, and push?")
                  : root.confirmKind === "switch-repo"
                    ? "Point this laptop at a different git repo? Local files are not deleted. The new repo is cloned and checked before anything is applied."
                    : "Unlink the config repo on this laptop? Local files are left as they are."
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Row {
              spacing: Style.space(8)
              layoutDirection: Qt.RightToLeft
              width: parent.width

              Button {
                text: root.confirmKind === "disconnect" ? "Unlink" : (root.confirmKind === "switch-repo" ? "Switch repo" : (root.confirmKind === "publish" ? (root.syncState === "empty" ? "Seed & push" : "Publish") : "Apply"))
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
    Column {
      width: parent.width
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
          value: String(root.incomingBundles.length + root.incomingFiles.length + root.incomingAddedShortcuts.length + root.incomingChangedShortcuts.length)
          highlightColor: (root.incomingFiles.length + root.differsFiles.length) > 0 ? root.accent : root.foreground
        }
        QuickPill {
          width: parent.pillW
          icon: "󰈸"
          label: "Local"
          value: String(root.localFiles.length)
          highlightColor: root.localFiles.length > 0 ? root.accent : root.foreground
        }
      }

      Row {
        spacing: Style.space(8)

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
          text: root.syncState === "empty" ? "Publish this laptop" : "Publish"
          iconText: "󰓂"
          tooltipText: root.syncState === "empty"
            ? "Seed the empty private repo from this machine, then push"
            : "Publish checked local items (p)"
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
          foreground: root.foreground
          fontFamily: root.fontFamily
          enabled: !root.busy
          onClicked: root.pullRemote()
        }
      }

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
              text: "HTTPS, SSH, owner/repo, or a local path. Empty private repos can be seeded from this laptop."
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
        TablePair { label: "Plugin"; value: "config-sync " + String((root.status && root.status.plugin_version) || "1.2.0") }
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

      Text {
        visible: root.status && root.status.fetch_error
        width: parent.width
        textFormat: Text.PlainText
        text: "Fetch: " + root.status.fetch_error
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Column {
        visible: root.hasReviewable
        width: parent.width
        spacing: Style.space(10)

        PanelSeparator { foreground: root.foreground }

        Text {
          width: parent.width
          textFormat: Text.PlainText
          text: "Include in Apply / Publish — flip the switch or tick the box on each row."
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
        }

        ChangeSection {
          title: "THEME"
          kind: "t"
          idField: "id"
          files: root.themeDiff ? [root.themeDiff] : []
          labelField: "display"
          summaryField: "slug"
          both: !!(root.themeDiff && root.themeDiff.status === "both")
        }

        ChangeSection { title: "INCOMING SHORTCUTS — ADDED"; kind: "s"; idField: "keys"; files: root.incomingAddedShortcuts; labelField: "keys"; summaryField: "label" }
        ChangeSection { title: "INCOMING SHORTCUTS — CHANGED"; kind: "s"; idField: "keys"; files: root.incomingChangedShortcuts; labelField: "keys"; summaryField: "detail" }
        ChangeSection { title: "LOCAL SHORTCUTS — ADDED"; kind: "s"; idField: "keys"; files: root.localAddedShortcuts; labelField: "keys"; summaryField: "label" }
        ChangeSection { title: "LOCAL SHORTCUTS — CHANGED"; kind: "s"; idField: "keys"; files: root.localChangedShortcuts; labelField: "keys"; summaryField: "detail" }
        ChangeSection { title: "INCOMING PLUGINS & FOLDERS"; kind: "g"; idField: "id"; files: root.incomingBundles; labelField: "name"; summaryField: "summary" }
        ChangeSection { title: "LOCAL PLUGINS & FOLDERS"; kind: "g"; idField: "id"; files: root.localBundles; labelField: "name"; summaryField: "summary" }
        ChangeSection { title: "INCOMING FILES"; kind: "f"; idField: "path"; files: root.incomingFiles.concat(root.differsFiles); labelField: "path"; summaryField: "summary" }
        ChangeSection { title: "CHANGED ON THIS LAPTOP"; kind: "f"; idField: "path"; files: root.localFiles; labelField: "path"; summaryField: "summary" }
      }
    }
  }

  Component {
    id: tabChangesComp
    Column {
      width: parent.width
      spacing: Style.space(12)

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: "Each row has an Include switch. On = Apply (incoming) or Publish (local). Off = leave that item alone."
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Text {
        visible: root.unresolvedBoth > 0
        width: parent.width
        textFormat: Text.PlainText
        text: "Checked items that changed on both sides still need Keep local or Take repo."
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Row {
        spacing: Style.space(6)
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
      }

      Row {
        spacing: Style.space(8)
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
          text: root.syncState === "empty" ? "Publish selected" : "Publish selected"
          iconText: "󰓂"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          enabled: !root.busy
          onClicked: root.requestPublish()
        }
      }

      Toggle {
        label: "Include display layout"
        description: "hypr/monitors.lua is machine-specific and skipped by default."
        checked: root.includeMachine
        foreground: root.foreground
        fontFamily: root.fontFamily
        onClicked: {
          root.includeMachine = !root.includeMachine
          Qt.callLater(function() { root.seedPicks() })
        }
      }

      Column {
        visible: root.conflictFiles.length > 0
        width: parent.width
        spacing: Style.space(6)
        PanelSectionHeader { text: "GIT CONFLICTS"; foreground: root.foreground; fontFamily: root.fontFamily }
        Repeater {
          model: root.conflictFiles
          FileRow {
            required property var modelData
            width: parent.width
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

      ChangeSection {
        title: "THEME"
        kind: "t"
        idField: "id"
        files: root.themeDiff ? [root.themeDiff] : []
        labelField: "display"
        summaryField: "slug"
        both: !!(root.themeDiff && root.themeDiff.status === "both")
      }

      ChangeSection {
        title: "INCOMING SHORTCUTS — ADDED"
        kind: "s"
        idField: "keys"
        files: root.incomingAddedShortcuts
        labelField: "keys"
        summaryField: "label"
      }
      ChangeSection {
        title: "INCOMING SHORTCUTS — CHANGED"
        kind: "s"
        idField: "keys"
        files: root.incomingChangedShortcuts
        labelField: "keys"
        summaryField: "detail"
      }
      ChangeSection {
        title: "LOCAL SHORTCUTS — ADDED"
        kind: "s"
        idField: "keys"
        files: root.localAddedShortcuts
        labelField: "keys"
        summaryField: "label"
      }
      ChangeSection {
        title: "LOCAL SHORTCUTS — CHANGED"
        kind: "s"
        idField: "keys"
        files: root.localChangedShortcuts
        labelField: "keys"
        summaryField: "detail"
      }
      ChangeSection {
        title: "SHORTCUTS CHANGED ON BOTH SIDES"
        kind: "s"
        idField: "keys"
        files: root.bothShortcuts
        labelField: "keys"
        summaryField: "label"
        both: true
      }

      ChangeSection {
        title: "INCOMING PLUGINS & FOLDERS"
        kind: "g"
        idField: "id"
        files: root.incomingBundles
        labelField: "name"
        summaryField: "summary"
      }
      ChangeSection {
        title: "LOCAL PLUGINS & FOLDERS"
        kind: "g"
        idField: "id"
        files: root.localBundles
        labelField: "name"
        summaryField: "summary"
      }
      ChangeSection {
        title: "PLUGINS CHANGED ON BOTH SIDES"
        kind: "g"
        idField: "id"
        files: root.bothBundles
        labelField: "name"
        summaryField: "summary"
        both: true
      }

      ChangeSection { title: "INCOMING FILES"; kind: "f"; idField: "path"; files: root.incomingFiles.concat(root.differsFiles); labelField: "path"; summaryField: "summary" }
      ChangeSection { title: "CHANGED ON THIS LAPTOP"; kind: "f"; idField: "path"; files: root.localFiles; labelField: "path"; summaryField: "summary" }
      ChangeSection { title: "FILES CHANGED ON BOTH SIDES"; kind: "f"; idField: "path"; files: root.bothFiles; labelField: "path"; summaryField: "summary"; both: true }

      Text {
        visible: !root.hasReviewable
        width: parent.width
        textFormat: Text.PlainText
        text: "No portable config differences. This laptop matches the repo."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        horizontalAlignment: Text.AlignHCenter
      }
    }
  }

  Component {
    id: tabShortcutsComp
    Column {
      width: parent.width
      spacing: Style.space(8)
      PanelSectionHeader {
        visible: root.shortcutDiffs.length > 0
        text: "CHANGED SHORTCUTS — INCLUDE TO SYNC"
        foreground: root.foreground
        fontFamily: root.fontFamily
      }
      ChangeSection {
        title: "INCOMING — ADDED"
        kind: "s"
        idField: "keys"
        files: root.incomingAddedShortcuts
        labelField: "keys"
        summaryField: "label"
      }
      ChangeSection {
        title: "INCOMING — CHANGED"
        kind: "s"
        idField: "keys"
        files: root.incomingChangedShortcuts
        labelField: "keys"
        summaryField: "detail"
      }
      ChangeSection {
        title: "LOCAL — ADDED"
        kind: "s"
        idField: "keys"
        files: root.localAddedShortcuts
        labelField: "keys"
        summaryField: "label"
      }
      ChangeSection {
        title: "LOCAL — CHANGED"
        kind: "s"
        idField: "keys"
        files: root.localChangedShortcuts
        labelField: "keys"
        summaryField: "detail"
      }

      PanelSectionHeader { text: "BINDINGS IN THE REPO"; foreground: root.foreground; fontFamily: root.fontFamily }
      Text {
        visible: !root.inspect || !root.inspect.shortcuts || root.inspect.shortcuts.length === 0
        text: "No o.bind() shortcuts found in hypr/bindings.lua."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      Repeater {
        model: root.inspect && root.inspect.shortcuts ? root.inspect.shortcuts : []
        CardBox {
          required property var modelData
          Row {
            width: parent.width
            spacing: Style.space(10)
            Text {
              textFormat: Text.PlainText
              text: modelData.keys
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              width: parent.width * 0.46
              wrapMode: Text.WordWrap
            }
            Text {
              textFormat: Text.PlainText
              text: modelData.label
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              width: parent.width * 0.5
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }

  Component {
    id: tabPluginsComp
    Column {
      width: parent.width
      spacing: Style.space(8)
      ChangeSection {
        title: "CHANGED PLUGINS — INCLUDE TO SYNC"
        kind: "g"
        idField: "id"
        files: root.incomingBundles.concat(root.localBundles).concat(root.bothBundles)
        labelField: "name"
        summaryField: "summary"
      }

      PanelSectionHeader { text: "PLUGINS THAT WILL LOAD"; foreground: root.foreground; fontFamily: root.fontFamily }
      Text {
        visible: !root.inspect || !root.inspect.plugins || root.inspect.plugins.length === 0
        text: "No plugins/ directory in the repo."
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }
      Repeater {
        model: root.inspect && root.inspect.plugins ? root.inspect.plugins : []
        CardBox {
          required property var modelData
          Column {
            width: parent.width
            spacing: Style.space(4)
            Row {
              width: parent.width
              spacing: Style.space(8)
              Text {
                textFormat: Text.PlainText
                text: modelData.name || modelData.id
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                elide: Text.ElideRight
                width: parent.width - ver.implicitWidth - Style.space(12)
              }
              Text {
                id: ver
                textFormat: Text.PlainText
                text: modelData.version || ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
            Text {
              visible: String(modelData.description || "") !== ""
              width: parent.width
              textFormat: Text.PlainText
              text: modelData.description
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
            Text {
              textFormat: Text.PlainText
              text: modelData.id
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      PanelSectionHeader {
        visible: root.inspect && root.inspect.bar
        text: "BAR LAYOUT"
        foreground: root.foreground
        fontFamily: root.fontFamily
      }
      Repeater {
        model: ["left", "center", "right"]
        Column {
          required property var modelData
          width: parent.width
          spacing: Style.space(4)
          visible: root.inspect && root.inspect.bar && root.inspect.bar.widgets && (root.inspect.bar.widgets[modelData] || []).length > 0
          Text {
            text: modelData.toUpperCase()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.inspect && root.inspect.bar && root.inspect.bar.widgets ? (root.inspect.bar.widgets[modelData] || []).join("  ·  ") : ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }

  Component {
    id: tabConfigsComp
    Column {
      width: parent.width
      spacing: Style.space(8)
      ChangeSection {
        title: "CHANGED FILES — INCLUDE TO SYNC"
        kind: "f"
        idField: "path"
        files: root.incomingFiles.concat(root.localFiles).concat(root.bothFiles).concat(root.differsFiles)
        labelField: "path"
        summaryField: "summary"
      }

      PanelSectionHeader { text: "FILES IN THE REPO"; foreground: root.foreground; fontFamily: root.fontFamily }
      Repeater {
        model: root.inspect && root.inspect.configs ? root.inspect.configs : []
        FileRow {
          required property var modelData
          width: parent.width
          pathLabel: modelData.path
          summary: modelData.summary
          statusLabel: modelData.portable ? "Portable" : "This machine"
        }
      }
      PanelSectionHeader {
        visible: root.inspect && root.inspect.hooks && root.inspect.hooks.length > 0
        text: "HOOKS"
        foreground: root.foreground
        fontFamily: root.fontFamily
      }
      Repeater {
        model: root.inspect && root.inspect.hooks ? root.inspect.hooks : []
        FileRow {
          required property var modelData
          width: parent.width
          pathLabel: modelData.event
          summary: modelData.name
          statusLabel: modelData.sample ? "Sample" : "Hook"
        }
      }
      PanelSectionHeader {
        visible: root.inspect && root.inspect.bins && root.inspect.bins.length > 0
        text: "HELPER SCRIPTS"
        foreground: root.foreground
        fontFamily: root.fontFamily
      }
      Text {
        visible: root.inspect && root.inspect.bins && root.inspect.bins.length > 0
        width: parent.width
        textFormat: Text.PlainText
        text: (root.inspect.bins || []).join("  ·  ")
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
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

  component ChangeSection: Column {
    id: sectionRoot
    property string title: ""
    property var files: []
    property bool both: false
    property string kind: "f"
    property string idField: "path"
    property string labelField: "path"
    property string summaryField: "summary"
    width: parent.width
    spacing: Style.space(8)
    visible: files && files.length > 0

    PanelSectionHeader {
      text: title + "  (" + files.length + ")"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Repeater {
      model: files

      Rectangle {
        id: rowBox
        required property var modelData
        required property int index

        readonly property string rowId: String(modelData[sectionRoot.idField] || "")
        readonly property string rowKind: sectionRoot.kind
        readonly property bool included: !!(root.picks[root.pickId(rowKind, rowId)])
        readonly property string bothKey: rowKind === "f" ? rowId : (rowKind + ":" + rowId)

        width: sectionRoot.width
        implicitHeight: rowInner.implicitHeight + Style.space(16)
        radius: Style.cornerRadius
        color: included ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12) : root.cardBg
        border.width: 2
        border.color: included ? root.accent : root.foreground

        Row {
          id: rowInner
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.space(10)
          anchors.rightMargin: Style.space(10)
          spacing: Style.space(10)

          // Large tick box — this is the checkbox.
          Rectangle {
            width: 28
            height: 28
            radius: 4
            anchors.verticalCenter: parent.verticalCenter
            color: rowBox.included ? root.accent : Color.background
            border.width: 2
            border.color: root.foreground

            Text {
              anchors.centerIn: parent
              text: rowBox.included ? "✓" : ""
              color: Color.background
              font.family: root.fontFamily
              font.pixelSize: 18
              font.bold: true
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.togglePick(rowBox.rowKind, rowBox.rowId)
            }
          }

          Column {
            width: parent.width - 28 - includeBtn.width - (sectionRoot.both ? 168 : 0) - parent.spacing * (sectionRoot.both ? 3 : 2)
            spacing: 2
            anchors.verticalCenter: parent.verticalCenter

            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: String(rowBox.modelData[sectionRoot.labelField] || "")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
              wrapMode: Text.WordWrap
            }
            Text {
              width: parent.width
              textFormat: Text.PlainText
              text: {
                var sum = String(rowBox.modelData[sectionRoot.summaryField] || "")
                if (sectionRoot.kind === "p")
                  sum = sum + " · " + String(rowBox.modelData.changed_count || 0) + " files"
                var st = Model.fileStatusLabel(rowBox.modelData.status)
                return (st ? st + " · " : "") + sum + (rowBox.included ? " · will sync" : " · skipped")
              }
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }

          Row {
            visible: sectionRoot.both
            spacing: Style.space(4)
            anchors.verticalCenter: parent.verticalCenter
            Button {
              text: "Keep local"
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              selected: root.bothPicks[rowBox.bothKey] === "local"
              bordered: true
              onClicked: root.selectSide(rowBox.rowKind, rowBox.rowId, "local")
            }
            Button {
              text: "Take repo"
              fontSize: Style.font.caption
              foreground: root.foreground
              fontFamily: root.fontFamily
              selected: root.bothPicks[rowBox.bothKey] === "repo"
              bordered: true
              onClicked: root.selectSide(rowBox.rowKind, rowBox.rowId, "repo")
            }
          }

          Button {
            id: includeBtn
            text: rowBox.included ? "Included" : "Skip"
            selected: rowBox.included
            bordered: true
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            anchors.verticalCenter: parent.verticalCenter
            onClicked: root.togglePick(rowBox.rowKind, rowBox.rowId)
          }
        }

        MouseArea {
          z: -1
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: root.togglePick(rowBox.rowKind, rowBox.rowId)
        }
      }
    }
  }

  component PickRow: Rectangle {
    id: pickRoot
    property string kind: "f"
    property string itemId: ""
    property string pathLabel: ""
    property string summary: ""
    property string statusLabel: ""
    property bool both: false
    readonly property bool checked: root.isPicked(kind, itemId)
    readonly property string bothKey: kind === "f" ? itemId : (kind + ":" + itemId)
    readonly property string direction: {
      var s = String(statusLabel || "").toLowerCase()
      if (s.indexOf("incoming") !== -1 || s === "new in repo") return "in"
      if (s.indexOf("local") !== -1 || s.indexOf("this laptop") !== -1) return "out"
      if (s.indexOf("both") !== -1) return "both"
      return ""
    }

    width: parent ? parent.width : 100
    implicitHeight: innerCol.implicitHeight + Style.space(14)
    radius: Style.cornerRadius
    color: checked ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.10) : root.cardBg
    border.width: 1
    border.color: checked ? root.accent : root.cardBorder

    Column {
      id: innerCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      anchors.topMargin: Style.space(8)
      spacing: Style.space(8)

      Row {
        width: parent.width
        spacing: Style.space(10)

        // Visible checkbox: 22px square, strong border, label beside it.
        Item {
          width: Style.space(22)
          height: Style.space(22)
          anchors.verticalCenter: parent.verticalCenter

          Rectangle {
            anchors.fill: parent
            radius: 4
            color: pickRoot.checked ? root.accent : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.06)
            border.width: 2
            border.color: pickRoot.checked ? root.accent : root.foreground
          }
          Text {
            anchors.centerIn: parent
            text: pickRoot.checked ? "✓" : ""
            color: Color.background
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: root.togglePick(pickRoot.kind, pickRoot.itemId)
          }
        }

        Column {
          width: parent.width - Style.space(22) - Style.space(10) - includeSwitch.width - Style.space(10)
          spacing: 2
          anchors.verticalCenter: parent.verticalCenter

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: pickRoot.pathLabel
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            wrapMode: Text.WordWrap
          }
          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: {
              var bits = []
              if (pickRoot.statusLabel) bits.push(pickRoot.statusLabel)
              if (pickRoot.summary) bits.push(pickRoot.summary)
              bits.push(pickRoot.checked ? "will sync" : "skipped")
              return bits.join(" · ")
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }

        Column {
          id: includeSwitch
          spacing: 2
          anchors.verticalCenter: parent.verticalCenter
          Text {
            text: pickRoot.checked ? "Include" : "Skip"
            color: pickRoot.checked ? root.accent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            anchors.horizontalCenter: parent.horizontalCenter
          }
          ToggleSwitch {
            checked: pickRoot.checked
            foreground: root.foreground
            accent: root.accent
            onToggled: root.togglePick(pickRoot.kind, pickRoot.itemId)
          }
        }
      }

      Row {
        visible: pickRoot.both
        spacing: Style.space(6)
        Button {
          text: "Keep local"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          selected: root.bothPicks[pickRoot.bothKey] === "local"
          bordered: true
          onClicked: root.selectSide(pickRoot.kind, pickRoot.itemId, "local")
        }
        Button {
          text: "Take repo"
          fontSize: Style.font.caption
          foreground: root.foreground
          fontFamily: root.fontFamily
          selected: root.bothPicks[pickRoot.bothKey] === "repo"
          bordered: true
          onClicked: root.selectSide(pickRoot.kind, pickRoot.itemId, "repo")
        }
      }
    }

    MouseArea {
      z: -1
      anchors.fill: parent
      cursorShape: Qt.PointingHandCursor
      onClicked: root.togglePick(pickRoot.kind, pickRoot.itemId)
    }
  }

  component FileRow: Rectangle {
    property string pathLabel: ""
    property string summary: ""
    property string statusLabel: ""
    property Component extra: Item { width: 0; height: 1 }
    width: parent ? parent.width : 100
    implicitHeight: Math.max(fileCol.implicitHeight, extraLoader.implicitHeight) + Style.space(12)
    radius: Style.cornerRadius
    color: root.cardBg
    border.width: 1
    border.color: root.cardBorder

    Column {
      id: fileCol
      anchors.left: parent.left
      anchors.right: extraLoader.left
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(8)
      anchors.rightMargin: Style.space(8)
      spacing: 2
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: pathLabel
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideMiddle
      }
      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: summary + (statusLabel ? " · " + statusLabel : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    Loader {
      id: extraLoader
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      sourceComponent: extra
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
