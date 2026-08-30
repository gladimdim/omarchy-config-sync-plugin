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
  property string confirmKind: ""
  property var bothPicks: ({})
  property var status: ({})
  property var inspect: null
  property var diffFiles: []
  property string lastNotifiedKey: ""

  readonly property bool configured: !!(status && status.configured)
  readonly property string syncState: String((status && status.sync_state) || (configured ? "in-sync" : "not-configured"))
  readonly property bool alarming: syncState === "conflicts" || syncState === "diverged" || syncState === "invalid"
  readonly property bool pending: syncState === "ready" || syncState === "remote-ahead" || syncState === "local-ahead" || alarming
  readonly property color stateColor: alarming ? urgent : (pending ? accent : foreground)
  readonly property var tabs: [
    { name: "Overview", icon: "󰘿" },
    { name: "Changes", icon: "󰦓" },
    { name: "Shortcuts", icon: "󰌌" },
    { name: "Plugins", icon: "󰐱" },
    { name: "Configs", icon: "󰒓" }
  ]

  readonly property var incomingFiles: Model.filesByStatus(diffFiles, ["repo", "added-repo"])
  readonly property var localFiles: Model.filesByStatus(diffFiles, ["local", "added-local"])
  readonly property var bothFiles: Model.filesByStatus(diffFiles, ["both"])
  readonly property var differsFiles: Model.filesByStatus(diffFiles, ["differs"])
  readonly property var conflictFiles: (status && status.conflicts) ? status.conflicts : []
  readonly property int unresolvedBoth: {
    var n = 0
    for (var i = 0; i < bothFiles.length; i++) {
      if (!bothPicks[bothFiles[i].path]) n++
    }
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

  function selectedApplyFiles() {
    var out = []
    for (var i = 0; i < diffFiles.length; i++) {
      var f = diffFiles[i]
      if (!includeMachine && !f.portable) continue
      if (f.status === "both") {
        if (bothPicks[f.path] === "repo") out.push(f.path)
        continue
      }
      if (f.default_apply) out.push(f.path)
    }
    return out
  }

  function selectedPublishFiles() {
    var out = []
    for (var i = 0; i < diffFiles.length; i++) {
      var f = diffFiles[i]
      if (!includeMachine && !f.portable) continue
      if (f.status === "both") {
        if (bothPicks[f.path] === "local") out.push(f.path)
        continue
      }
      if (f.default_publish) out.push(f.path)
    }
    return out
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
    if (selectedApplyFiles().length === 0) {
      lastError = "Nothing from the repo is selected to apply."
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
    if (selectedPublishFiles().length === 0 && Number(status.ahead || 0) === 0) {
      lastError = "No local changes to publish."
      activeTab = 1
      return
    }
    confirmKind = "publish"
  }

  function confirmCurrent() {
    var kind = confirmKind
    confirmKind = ""
    if (kind === "apply") {
      var files = selectedApplyFiles()
      var args = ["apply"]
      if (includeMachine) args.push("--include-machine")
      args.push("--files", files.join(","))
      run(args)
    } else if (kind === "publish") {
      var pub = selectedPublishFiles()
      var pargs = ["publish", "--push"]
      if (includeMachine) pargs.push("--include-machine")
      if (pub.length > 0) pargs.push("--files", pub.join(","))
      run(pargs)
    } else if (kind === "disconnect") {
      run(["disconnect"])
      repoUrlInput = ""
      bothPicks = ({})
    }
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
    if (data.sync_state && status)
      status = Object.assign({}, status, { sync_state: data.sync_state })
    if (!repoUrlInput && status.repo_url)
      repoUrlInput = String(status.repo_url)
    maybeNotify()
  }

  function maybeNotify() {
    if (!configured || !root.opened) return
    var key = syncState + ":" + String(status.head || "") + ":" + String(status.local_changes || 0) + ":" + String(status.repo_changes || 0)
    if (key === lastNotifiedKey) return
    if (syncState === "in-sync" || syncState === "not-configured") return
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
      blocked: urlField.activeFocus || root.confirmKind !== ""

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
            RotationAnimator on rotation {
              running: root.busy
              from: 0; to: 360
              duration: 900
              loops: Animation.Infinite
            }
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
        Column {
          visible: !root.configured
          width: parent.width
          spacing: Style.space(12)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: "Link the git repo that stores your Omarchy configs. The plugin clones it, checks that it really is an omarchy-config tree, then shows shortcuts, plugins, and files before anything is applied."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
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
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: !root.busy
              onClicked: {
                root.repoUrlInput = Quickshell.env("HOME") + "/Github/omarchy-config"
                urlField.text = root.repoUrlInput
              }
            }
          }

          CardBox {
            TablePair { label: "Accepted"; value: "GitHub URL, SSH, owner/repo, or a local git path" }
            TablePair { label: "Required layout"; value: "hypr/ + shell.json, plugins/, or apply.sh" }
            TablePair { label: "Then"; value: "Preview features → Apply, or publish local edits back" }
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
                ? "Apply repo configs to this laptop? A timestamped backup is written first. Display layout stays local unless you opted in."
                : root.confirmKind === "publish"
                  ? "Copy this laptop's changes into the repo, commit, and push so your other machines can pull them?"
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
                text: root.confirmKind === "disconnect" ? "Unlink" : (root.confirmKind === "publish" ? "Publish" : "Apply")
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
          value: String(root.incomingFiles.length + root.differsFiles.length)
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
          text: "Apply"
          iconText: "󰁨"
          tooltipText: "Copy repo → this laptop (a)"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          enabled: !root.busy
          onClicked: root.requestApply()
        }
        Button {
          text: "Publish"
          iconText: "󰓂"
          tooltipText: "Copy this laptop → repo and push (p)"
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
        TablePair { label: "Remote"; value: String(root.status.repo_url || "—") }
        TablePair { label: "Branch"; value: String((root.status.branch || "—") + (root.status.head ? " @ " + root.status.head : "")) }
        TablePair { label: "Ahead / behind"; value: String(root.status.ahead || 0) + " / " + String(root.status.behind || 0) }
        TablePair { label: "Last apply"; value: Model.relativeAgo(root.status.last_apply_at) }
        TablePair { label: "Last publish"; value: Model.relativeAgo(root.status.last_publish_at) }
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
    }
  }

  Component {
    id: tabChangesComp
    Column {
      width: parent.width
      spacing: Style.space(12)

      Text {
        visible: root.unresolvedBoth > 0
        width: parent.width
        textFormat: Text.PlainText
        text: "Choose Keep local or Take repo for every file that changed on both sides before Apply or Publish."
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Toggle {
        label: "Include display layout"
        description: "hypr/monitors.lua is machine-specific and skipped by default."
        checked: root.includeMachine
        foreground: root.foreground
        fontFamily: root.fontFamily
        onClicked: root.includeMachine = !root.includeMachine
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

      ChangeSection { title: "INCOMING FROM REPO"; files: root.incomingFiles.concat(root.differsFiles) }
      ChangeSection { title: "CHANGED ON THIS LAPTOP"; files: root.localFiles }
      ChangeSection { title: "BOTH CHANGED"; files: root.bothFiles; both: true }

      Text {
        visible: root.incomingFiles.length + root.localFiles.length + root.bothFiles.length + root.differsFiles.length + root.conflictFiles.length === 0
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

  component ChangeSection: Column {
    property string title: ""
    property var files: []
    property bool both: false
    width: parent.width
    spacing: Style.space(6)
    visible: files && files.length > 0

    PanelSectionHeader {
      text: title + "  (" + files.length + ")"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Repeater {
      model: files
      FileRow {
        required property var modelData
        width: parent.width
        pathLabel: modelData.path
        summary: modelData.summary
        statusLabel: Model.fileStatusLabel(modelData.status)
        extra: both ? bothButtons : null
        property Component bothButtons: Row {
          spacing: Style.space(4)
          Button {
            text: "Keep local"
            fontSize: Style.font.caption
            foreground: root.foreground
            fontFamily: root.fontFamily
            selected: root.bothPicks[modelData.path] === "local"
            bordered: true
            onClicked: root.setPick(modelData.path, "local")
          }
          Button {
            text: "Take repo"
            fontSize: Style.font.caption
            foreground: root.foreground
            fontFamily: root.fontFamily
            selected: root.bothPicks[modelData.path] === "repo"
            bordered: true
            onClicked: root.setPick(modelData.path, "repo")
          }
        }
      }
    }
  }

  component FileRow: Rectangle {
    property string pathLabel: ""
    property string summary: ""
    property string statusLabel: ""
    property Component extra: null
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
