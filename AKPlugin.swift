//
//  MacPlugin.swift
//  AKInterface
//
//  Created by Isaac Marovitz on 13/09/2022.
//

import AppKit
import CoreGraphics
import Foundation
import Darwin
import OSLog

// Add a lightweight struct so we can decode only the flag we care about
private struct AKAppSettingsData: Codable {
    var hideTitleBar: Bool?
    var floatingWindow: Bool?
    var resolution: Int?
    var resizableAspectRatioWidth: Int?
    var resizableAspectRatioHeight: Int?
}

// swiftlint:disable type_body_length
class AKPlugin: NSObject, Plugin {
    private let logger = Logger(subsystem: "PlayTools", category: "AKPlugin")

    required override init() {
        super.init()
        if let window = NSApplication.shared.windows.first {
            window.styleMask.insert([.resizable])
            window.collectionBehavior = [.fullScreenPrimary, .managed, .participatesInCycle]
            window.isMovable = true
            window.isMovableByWindowBackground = true

            if self.hideTitleBarSetting == true {
                window.styleMask.insert([.fullSizeContentView])
                window.titlebarAppearsTransparent = true
                window.titleVisibility = .hidden
                window.toolbar = nil
                window.title = ""
            }

            if self.floatingWindowSetting == true {
                window.level = .floating
            }

            if let aspectRatio = self.aspectRatioSetting {
                window.contentAspectRatio = aspectRatio
            }

            NSWindow.allowsAutomaticWindowTabbing = true
        }

        // Apply the same appearance rules to any subsequent windows that may be created
        NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification,
            object: nil,
            queue: .main) { notif in
                guard let win = notif.object as? NSWindow else { return }
                win.styleMask.insert([.resizable])

                if self.hideTitleBarSetting == true {
                    win.styleMask.insert([.fullSizeContentView])
                    win.titlebarAppearsTransparent = true
                    win.titleVisibility = .hidden
                    win.toolbar = nil
                    win.title = ""
                }

                if self.floatingWindowSetting == true {
                    win.level = .floating
                }

                if let aspectRatio = self.aspectRatioSetting {
                    win.contentAspectRatio = aspectRatio
                }
        }
    }

    var screenCount: Int {
        NSScreen.screens.count
    }

    var mousePoint: CGPoint {
        NSApplication.shared.windows.first?.mouseLocationOutsideOfEventStream ?? CGPoint()
    }

    var windowFrame: CGRect {
        NSApplication.shared.windows.first?.frame ?? CGRect()
    }

    var isMainScreenEqualToFirst: Bool {
        return NSScreen.main == NSScreen.screens.first
    }

    var mainScreenFrame: CGRect {
        return NSScreen.main!.frame as CGRect
    }

    var isFullscreen: Bool {
        NSApplication.shared.windows.first!.styleMask.contains(.fullScreen)
    }

    // --- window title manager (base + tags) -------------------------
    private var _windowTitleBase: String? = nil
    private var _windowTitleTags: [String: String] = [:]

    private func composeWindowTitle() -> String {
        let base = (_windowTitleBase?.isEmpty == false) ? _windowTitleBase! : (NSApplication.shared.windows.first?.title ?? "")
        let tags = _windowTitleTags.values.sorted()
        if tags.isEmpty { return base }
        return ([base] + tags).joined(separator: " ")
    }

    private func setWindowTitleBase(_ newBase: String?) {
        _windowTitleBase = newBase ?? ""
        NSApplication.shared.windows.first?.title = composeWindowTitle()
    }

    var windowTitle: String? {
        get {
            NSApplication.shared.windows.first?.title
        }
        set {
            let newBase = stripTrailingBracketTags(from: newValue ?? "")
            if Thread.isMainThread {
                setWindowTitleBase(newBase)
            } else {
                DispatchQueue.main.async { [weak self] in
                    self?.setWindowTitleBase(newBase)
                }
            }
        }
    }

    func setWindowTitleTag(_ key: String, _ value: String?) {
        // Ensure base is canonicalized from the existing window title the first time we set a tag.
        func ensureBaseInitialized() {
            if _windowTitleBase == nil || _windowTitleBase?.isEmpty == true {
                let current = NSApplication.shared.windows.first?.title ?? ""
                // strip ANY bracketed tags from the existing window title so they
                // don't survive as part of the base and cause duplicate tags
                _windowTitleBase = stripAllBracketTags(from: current)
            }
        }

        if Thread.isMainThread {
            ensureBaseInitialized()
            if let titleValue = value {
                _windowTitleTags[key] = titleValue.trimmingCharacters(in: .whitespacesAndNewlines)
            } else {
                _windowTitleTags.removeValue(forKey: key)
            }
            NSApplication.shared.windows.first?.title = composeWindowTitle()
        } else {
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                ensureBaseInitialized()
                if let titleValue = value {
                    self._windowTitleTags[key] = titleValue.trimmingCharacters(in: .whitespacesAndNewlines)
                } else {
                    self._windowTitleTags.removeValue(forKey: key)
                }
                NSApplication.shared.windows.first?.title = self.composeWindowTitle()
            }
        }
    }

    private func stripTrailingBracketTags(from str: String) -> String {
        var base = str.trimmingCharacters(in: .whitespacesAndNewlines)
        while let lastClose = base.lastIndex(of: "]"),
              let lastOpen = base[..<lastClose].lastIndex(of: "[") {
            // only strip if the closing bracket is at the end (allow trailing whitespace)
            if base.distance(from: lastClose, to: base.endIndex) <= 1 {
                let removeStart = (lastOpen > base.startIndex && base[base.index(before: lastOpen)] == " ")
                    ? base.index(before: lastOpen) : lastOpen
                base.removeSubrange(removeStart...lastClose)
                base = base.trimmingCharacters(in: .whitespacesAndNewlines)
            } else {
                break
            }
        }
        return base
    }

    /// Remove any bracketed segments anywhere in the string (used only when
    /// initializing the internal base from an existing window title).
    private func stripAllBracketTags(from str: String) -> String {
        var titleStr = str
        while let open = titleStr.firstIndex(of: "["),
              let close = titleStr[open...].firstIndex(of: "]") {
            let start = (open > titleStr.startIndex && titleStr[titleStr.index(before: open)] == " ") ? titleStr.index(before: open) : open
            titleStr.removeSubrange(start...close)
            titleStr = titleStr.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return titleStr
    }

    var windowImage: CGImage? {
        guard let windowID = NSApplication.shared.windows.first?.windowNumber else {
            return nil
        }
        return CGWindowListCreateImage(.null, .optionIncludingWindow, CGWindowID(windowID), [.bestResolution, .boundsIgnoreFraming])
    }

    var windowImageLogical: CGImage? {
        guard let windowID = NSApplication.shared.windows.first?.windowNumber else {
            return nil
        }
        // 逻辑分辨率（不带 bestResolution）：数据量是 Retina 版的 1/4，WindowServer 传输耗时大幅降低
        // 专供 MaaToolsIPC 使用［MAA 图像识别不需要 Retina 精度｝
        return CGWindowListCreateImage(.null, .optionIncludingWindow, CGWindowID(windowID), [.boundsIgnoreFraming])
    }

    var cmdPressed: Bool = false
    var cursorHideLevel = 0
    func hideCursor() {
        NSCursor.hide()
        cursorHideLevel += 1
        CGAssociateMouseAndMouseCursorPosition(0)
        warpCursor()
    }

    func hideCursorMove() {
        NSCursor.setHiddenUntilMouseMoves(true)
    }

    func warpCursor() {
        guard let firstScreen = NSScreen.screens.first else {return}
        let frame = windowFrame
        // Convert from NS coordinates to CG coordinates
        CGWarpMouseCursorPosition(CGPoint(x: frame.midX, y: firstScreen.frame.height - frame.midY))
    }

    func unhideCursor() {
        NSCursor.unhide()
        cursorHideLevel -= 1
        if cursorHideLevel <= 0 {
            CGAssociateMouseAndMouseCursorPosition(1)
        }
    }

    func terminateApplication() {
        logger.info("terminateApplication() requested — attempting graceful terminate")

        // Try Cocoa termination first (gives app a chance to clean up)
        NSApplication.shared.terminate(self)

        // Immediate synchronous fallback: ensure the process exits even if the app
        // is background-suspended or the main runloop doesn't process timers.
        // Using exit(0) here guarantees the process will terminate now.
        logger.warning("terminateApplication(): synchronous exit(0) fallback executing")
        exit(0)
    }

    private var modifierFlag: UInt = 0

    // swiftlint:disable:next function_body_length
    func setupKeyboard(keyboard: @escaping (UInt16, Bool, Bool, Bool) -> Bool,
                       swapMode: @escaping () -> Bool) {
        func checkCmd(modifier: NSEvent.ModifierFlags) -> Bool {
            if modifier.contains(.command) {
                self.cmdPressed = true
                return true
            } else if self.cmdPressed {
                self.cmdPressed = false
            }
            return false
        }
        NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { event in
            if checkCmd(modifier: event.modifierFlags) {
                return event
            }
            let consumed = keyboard(event.keyCode, true, event.isARepeat,
                                    event.modifierFlags.contains(.control))
            if consumed {
                return nil
            }
            return event
        })
        NSEvent.addLocalMonitorForEvents(matching: .keyUp, handler: { event in
            if checkCmd(modifier: event.modifierFlags) {
                return event
            }
            let consumed = keyboard(event.keyCode, false, false,
                                    event.modifierFlags.contains(.control))
            if consumed {
                return nil
            }
            return event
        })
        NSEvent.addLocalMonitorForEvents(matching: .flagsChanged, handler: { event in
            if checkCmd(modifier: event.modifierFlags) {
                return event
            }
            let pressed = self.modifierFlag < event.modifierFlags.rawValue
            let changed = self.modifierFlag ^ event.modifierFlags.rawValue
            self.modifierFlag = event.modifierFlags.rawValue
            let changedFlags = NSEvent.ModifierFlags(rawValue: changed)
            if pressed && changedFlags.contains(.option) {
                if swapMode() {
                    return nil
                }
                return event
            }
            let consumed = keyboard(event.keyCode, pressed, false,
                                    event.modifierFlags.contains(.control))
            if consumed {
                return nil
            }
            return event
        })
    }

    func setupMouseMoved(_ mouseMoved: @escaping (CGFloat, CGFloat) -> Bool) {
        let mask: NSEvent.EventTypeMask = [.leftMouseDragged, .otherMouseDragged, .rightMouseDragged]
        NSEvent.addLocalMonitorForEvents(matching: mask, handler: { event in
            let consumed = mouseMoved(event.deltaX, event.deltaY)
            if consumed {
                return nil
            }
            return event
        })
        // transpass mouse moved event when no button pressed, for traffic light button to light up
        NSEvent.addLocalMonitorForEvents(matching: .mouseMoved, handler: { event in
            _ = mouseMoved(event.deltaX, event.deltaY)
            return event
        })
    }

    func setupMouseButton(left: Bool, right: Bool, _ consumed: @escaping (Int, Bool) -> Bool) {
        let downType: NSEvent.EventTypeMask = left ? .leftMouseDown : right ? .rightMouseDown : .otherMouseDown
        let upType: NSEvent.EventTypeMask = left ? .leftMouseUp : right ? .rightMouseUp : .otherMouseUp

        // Helper to detect whether the event is inside any of the window "traffic-light" buttons
        func isInTrafficLightArea(_ event: NSEvent) -> Bool {
            if self.hideTitleBarSetting == false {
                return false
            }
            guard let win = event.window else { return false }
            let pointInWindow = event.locationInWindow
            let buttonTypes: [NSWindow.ButtonType] = [.closeButton, .miniaturizeButton, .zoomButton, .fullScreenButton]
            for type in buttonTypes {
                if let button = win.standardWindowButton(type) {
                    let localPoint = button.convert(pointInWindow, from: nil) // convert from window coords
                    if button.bounds.contains(localPoint) {
                        return true
                    }
                }
            }
            return false
        }

        NSEvent.addLocalMonitorForEvents(matching: downType, handler: { event in
            // Always allow clicks on the window traffic-light buttons to pass through
            if isInTrafficLightArea(event) {
                return event
            }

            // Detect double-clicks on the title-bar area (respecting system preference)

            if left && event.clickCount == 2, self.hideTitleBarSetting, let win = event.window {
                let contentRect = win.contentLayoutRect
                // Title-bar area is the region above contentLayoutRect
                if event.locationInWindow.y > contentRect.maxY {
                    win.performZoom(nil)
                    return nil
                }
            }

            // For traffic light buttons when fullscreen
            if event.window != NSApplication.shared.windows.first! {
                return event
            }
            if consumed(event.buttonNumber, true) {
                return nil
            }
            return event
        })
        NSEvent.addLocalMonitorForEvents(matching: upType, handler: { event in
            // Always allow releases on the traffic-light buttons to pass through
            if isInTrafficLightArea(event) {
                return event
            }
            if consumed(event.buttonNumber, false) {
                return nil
            }
            return event
        })
    }

    func setupScrollWheel(_ onMoved: @escaping (CGFloat, CGFloat) -> Bool) {
        NSEvent.addLocalMonitorForEvents(matching: NSEvent.EventTypeMask.scrollWheel, handler: { event in
            var deltaX = event.scrollingDeltaX, deltaY = event.scrollingDeltaY
            if !event.hasPreciseScrollingDeltas {
                deltaX *= 16
                deltaY *= 16
            }
            let consumed = onMoved(deltaX, deltaY)
            if consumed {
                return nil
            }
            return event
        })
    }

    func urlForApplicationWithBundleIdentifier(_ value: String) -> URL? {
        NSWorkspace.shared.urlForApplication(withBundleIdentifier: value)
    }

    func setMenuBarVisible(_ visible: Bool) {
        NSMenu.setMenuBarVisible(visible)
    }

    /// Convenience instance property that exposes the cached static preference.
    private var hideTitleBarSetting: Bool { Self.akAppSettingsData?.hideTitleBar ?? false }
    private var floatingWindowSetting: Bool { Self.akAppSettingsData?.floatingWindow ?? false }
    private var aspectRatioSetting: NSSize? {
        guard Self.akAppSettingsData?.resolution == 6 else {
            return nil
        }
        let width = Self.akAppSettingsData?.resizableAspectRatioWidth ?? 0
        let height = Self.akAppSettingsData?.resizableAspectRatioHeight ?? 0
        guard width > 0 && height > 0 else {
            return nil
        }
        return NSSize(width: width, height: height)
    }

    fileprivate static var akAppSettingsData: AKAppSettingsData? = {
        let bundleIdentifier = Bundle.main.bundleIdentifier ?? ""
        let settingsURL = URL(fileURLWithPath: "/Users/\(NSUserName())/Library/Containers/io.playcover.PlayCover")
            .appendingPathComponent("App Settings")
            .appendingPathComponent("\(bundleIdentifier).plist")
        guard let data = try? Data(contentsOf: settingsURL),
              let decoded = try? PropertyListDecoder().decode(AKAppSettingsData.self, from: data) else {
            return nil
        }
        return decoded
    }()
}
