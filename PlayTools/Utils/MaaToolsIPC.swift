//
//  MaaToolsIPC.swift
//  PlayTools
//
// swiftlint:disable file_length
//  业务逻辑层：截图 + 触控模拟
//
//  基于 SandboxIPC 通用框架（SandboxIPCServer + ShmRingBuffer）实现
//  MAA 工具协议的具体业务处理，包含以下优化：
//
//  【性能优化】
//  · 混合自旋等待（Hybrid Spin-Wait）：先自旋 200 次，高频 burst 时避免 sem_wait
//    内核上下文切换，显著降低连续操作的端到端延迟
//  · Drain Loop：被唤醒后一次性处理所有积压命令，减少不必要的 sem_wait
//  · CGContext 复用：截图时复用 CGContext，避免每帧重建 Context 的分配开销
//  · 零拷贝截图：CGContext 数据区直接指向共享内存，渲染完成即可被 Python 读取
//
//  【架构清晰】
//  · SandboxIPCServer   只管 Unix Socket 传输 + fd 传递（无业务逻辑）
//  · ShmRingBuffer      只管共享内存读写（无业务逻辑）
//  · MaaToolsIPC（本文件） 只管业务调度（截图/触控/查询）
//

import Foundation
import UIKit
import OSLog
import Darwin

// swiftlint:disable:next type_body_length
final class MaaToolsIPC {

    // MARK: - Shared Instance

    public static let shared = MaaToolsIPC()

    // MARK: - Properties

    private let logger = Logger(subsystem: "PlayTools", category: "MaaToolsIPC")

    // IPC 服务层
    private var server: SandboxIPCServer?

    // 环形缓冲区（Session 内有效）
    private var cmdRing: ShmRingBuffer<IPCCommandPacket>?
    private var eventRing: ShmRingBuffer<IPCEventPacket>?

    // 共享内存映射（Session 内有效）
    private var ipcBasePtr: UnsafeMutableRawPointer?
    private var ipcBaseSize: Int = 0
    private var screencapBasePtr: UnsafeMutableRawPointer?
    private var screencapSize: Int = 0

    // POSIX 信号量（Session 内有效，由 Swift 侧创建）
    private var cmdSem: UnsafeMutablePointer<sem_t>?
    private var eventSem: UnsafeMutablePointer<sem_t>?
    private var cmdSemName: String = ""
    private var eventSemName: String = ""

    // 截图 CGContext 缓存（数据区 = screencapBasePtr，无需重建）
    private var cachedContext: CGContext?

    // 屏幕信息（主线程写，其他线程只读）
    private var screenWidth: Int = 0
    private var screenHeight: Int = 0

    // 命令处理 Task 控制
    private var processingTask: Task<Void, Never>?
    private var isSessionActive = false

    // 触摸 ID 追踪（仅在主线程访问）
    // swiftlint:disable:next implicitly_unwrapped_optional
    nonisolated(unsafe) private var tid: Int?

    // MARK: - Init

    private init() {
        let tmpDir = (NSHomeDirectory() as NSString).appendingPathComponent("tmp")
        try? FileManager.default.createDirectory(
            atPath: tmpDir, withIntermediateDirectories: true
        )
        let socketPath = (tmpDir as NSString).appendingPathComponent("maa_ipc.sock")

        let ipcServer = SandboxIPCServer(
            socketPath: socketPath,
            logger: Logger(subsystem: "PlayTools", category: "SandboxIPCServer")
        )
        ipcServer.delegate = self
        self.server = ipcServer
    }

    // MARK: - Lifecycle

    func initialize() {
        guard PlaySettings.shared.maaToolsIPC else {
            logger.info("MaaToolsIPC disabled (maaToolsIPC = false)")
            return
        }
        refreshScreenInfo()
        server?.start()
        logger.info("MaaToolsIPC initialized, socket listening for client...")
    }

    func uninitialize() {
        server?.stop()
        cleanupSession()
        logger.info("MaaToolsIPC uninitialized")
    }

    // MARK: - Screen Info

    private func refreshScreenInfo() {
        assert(Thread.isMainThread, "refreshScreenInfo must run on main thread")
        let window = UIApplication.shared.connectedScenes
            .flatMap { ($0 as? UIWindowScene)?.windows ?? [] }
            .first { $0.isKeyWindow }

        if let screen = window?.windowScene?.screen {
            screenWidth  = Int(screen.nativeBounds.width.rounded())
            screenHeight = Int(screen.nativeBounds.height.rounded())
        }
    }

    // MARK: - Session Cleanup

    private func cleanupSession() {
        isSessionActive = false

        // 取消处理 Task
        processingTask?.cancel()
        processingTask = nil

        // 唤醒可能阻塞在 sem_wait 的处理循环（让它检测到 isSessionActive=false 并退出）
        if let sem = cmdSem {
            sem_post(sem)
        }

        // 关闭并删除信号量（Swift 侧创建，Swift 侧清理）
        if let sem = cmdSem {
            sem_close(sem)
            if !cmdSemName.isEmpty { sem_unlink(cmdSemName) }
            cmdSem = nil
        }
        if let sem = eventSem {
            sem_close(sem)
            if !eventSemName.isEmpty { sem_unlink(eventSemName) }
            eventSem = nil
        }
        cmdSemName   = ""
        eventSemName = ""

        // 取消映射共享内存（fd 在映射后即关闭，无需再 close）
        if let ptr = ipcBasePtr, ipcBaseSize > 0 {
            munmap(ptr, ipcBaseSize)
            ipcBasePtr  = nil
            ipcBaseSize = 0
        }
        if let ptr = screencapBasePtr, screencapSize > 0 {
            munmap(ptr, screencapSize)
            screencapBasePtr = nil
            screencapSize    = 0
        }

        // 清理引用
        cmdRing       = nil
        eventRing     = nil
        cachedContext = nil  // Context 指向 screencapBasePtr，必须在 munmap 后置 nil

        logger.info("🧹 Session cleaned up")
    }

    // MARK: - Command Processing Loop

    private func startProcessingLoop() {
        // 拷贝 Session 局部状态到 Task 闭包，避免 data race
        let capturedCmdRing  = cmdRing
        let capturedEventSem = eventSem
        guard let capturedCmdSem = cmdSem else {
            logger.error("cmdSem is nil, cannot start processing loop")
            return
        }

        processingTask = Task(priority: .userInitiated) { [weak self] in
            guard let self = self else { return }
            self.logger.info("⚡ Command processing loop started (hybrid spin-wait)")

            while self.isSessionActive && !Task.isCancelled {
                // ── Hybrid Spin-Wait ───────────────────────────────────────────
                // 高频 burst 场景（如连续快速滑动）：先自旋，避免 sem_wait 的
                // 内核上下文切换延迟（通常 10~50µs）。
                // 自旋次数上限 = 200 次，约 ~2µs，不会明显占用 CPU。
                var gotCommand = false
                for _ in 0..<200 {
                    if let cmd = capturedCmdRing?.tryRead() {
                        await self.processCommand(cmd, eventSem: capturedEventSem)
                        gotCommand = true
                        break
                    }
                }

                if gotCommand {
                    // Drain Loop：有命令被处理后，立即继续消耗剩余积压
                    while let cmd = capturedCmdRing?.tryRead() {
                        await self.processCommand(cmd, eventSem: capturedEventSem)
                    }
                    continue
                }

                // ── Kernel Sleep ───────────────────────────────────────────────
                // 自旋未命中，进入内核休眠，等待 Python 侧 sem_post 唤醒
                if sem_wait(capturedCmdSem) != 0 {
                    if errno == EINTR  { continue }  // 被信号中断，重试
                    if errno == EINVAL { break }      // 信号量被关闭，优雅退出
                    self.logger.error("sem_wait failed: errno=\(errno)")
                    break
                }

                // 唤醒后 drain 所有积压命令
                while let cmd = capturedCmdRing?.tryRead() {
                    await self.processCommand(cmd, eventSem: capturedEventSem)
                }
            }

            self.logger.info("Command processing loop exited")
        }
    }

    // MARK: - Command Dispatch

    // swiftlint:disable:next function_body_length
    private func processCommand(
        _ cmd: IPCCommandPacket,
        eventSem: UnsafeMutablePointer<sem_t>?
    ) async {
        guard let cmdType = IPCCommandType(rawValue: cmd.type) else {
            logger.warning("Unknown command type: \(cmd.type), seq=\(cmd.seqId)")
            return
        }

        switch cmdType {
        case .screenshot:
            logger.debug("SCREENSHOT seq=\(cmd.seqId)")
            let success = await MainActor.run { [weak self] () -> Bool in
                return self?.captureToSharedMemory() ?? false
            }
            sendEvent(
                type: success ? .screenshotReady : .error,
                reqSeqId: cmd.seqId,
                errorCode: success ? 0 : -1,
                eventSem: eventSem
            )

        case .tap:
            logger.debug("TAP (\(cmd.x),\(cmd.y)) \(cmd.duration)ms seq=\(cmd.seqId)")
            let point = CGPoint(x: Int(cmd.x), y: Int(cmd.y))
            await MainActor.run { [weak self] in
                guard let self = self else { return }
                Toucher.touchcam(point: point, phase: .began,
                                 tid: &self.tid, actionName: "down", keyName: "maa_tap")
            }
            try? await Task.sleep(nanoseconds: UInt64(cmd.duration) * 1_000_000)
            await MainActor.run { [weak self] in
                guard let self = self else { return }
                Toucher.touchcam(point: point, phase: .ended,
                                 tid: &self.tid, actionName: "up", keyName: "maa_tap")
                Toucher.keyView = nil
            }
            sendEvent(type: .ack, reqSeqId: cmd.seqId, errorCode: 0, eventSem: eventSem)

        case .swipe:
            logger.debug("SWIPE (\(cmd.x),\(cmd.y))→(\(cmd.x2),\(cmd.y2)) \(cmd.duration)ms seq=\(cmd.seqId)")
            await performSwipe(cmd, eventSem: eventSem)

        case .drag:
            logger.debug("DRAG (\(cmd.x),\(cmd.y))→(\(cmd.x2),\(cmd.y2)) \(cmd.duration)ms seq=\(cmd.seqId)")
            await performDrag(cmd, eventSem: eventSem)

        case .getSize:
            logger.debug("GET_SIZE seq=\(cmd.seqId)")
            let encoded = IPCConfig.encodeScreenSize(width: screenWidth, height: screenHeight)
            sendEvent(type: .sizeInfo, reqSeqId: cmd.seqId,
                      errorCode: encoded, eventSem: eventSem)

        case .getVersion:
            logger.debug("GET_VERSION seq=\(cmd.seqId)")
            sendEvent(type: .versionInfo, reqSeqId: cmd.seqId,
                      errorCode: Int32(IPCConfig.protocolVersion), eventSem: eventSem)
        }
    }

    // MARK: - Screenshot Capture

    /// 注意：必须在主线程调用（UIKit / CoreGraphics 要求）
    @MainActor
    private func captureToSharedMemory() -> Bool {
        guard let image = AKInterface.shared?.windowImage else {
            logger.error("windowImage unavailable")
            return false
        }
        guard screenWidth > 0, screenHeight > 0 else {
            logger.error("Screen size not available (\(self.screenWidth)×\(self.screenHeight))")
            refreshScreenInfo()
            return false
        }
        guard let capPtr = screencapBasePtr, screencapSize > 0 else {
            logger.error("Screencap shared memory not mapped")
            return false
        }

        // 裁剪标题栏（macOS Catalyst 窗口包含标题栏，保留纯游戏内容区域）
        let titleBarHeight = image.height - image.width * screenHeight / screenWidth
        let contentRect = CGRect(x: 0, y: titleBarHeight,
                                 width: image.width,
                                 height: image.height - titleBarHeight)
        guard let cropped = image.cropping(to: contentRect) else {
            logger.error("Failed to crop image (titleBarHeight=\(titleBarHeight))")
            return false
        }

        // ── CGContext 复用 ──────────────────────────────────────
        // Context 数据区直接指向 screencapBasePtr（共享内存），
        // 只要 Session 未断开，screencapBasePtr 地址不变，Context 可复用。
        // 避免每帧约 ~100µs 的 CGContext 分配 + 初始化开销。
        if cachedContext == nil {
            let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!
            let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue
                           | CGBitmapInfo.byteOrder32Big.rawValue
            cachedContext = CGContext(
                data: capPtr,
                width: screenWidth,
                height: screenHeight,
                bitsPerComponent: 8,
                bytesPerRow: screenWidth * 4,
                space: colorSpace,
                bitmapInfo: bitmapInfo
            )
            logger.debug("CGContext created for \(self.screenWidth)×\(self.screenHeight)")
        }

        guard let ctx = cachedContext else {
            logger.error("Failed to create CGContext")
            return false
        }

        // 直接渲染到共享内存（零拷贝，Python 侧可立即读取）
        ctx.draw(cropped, in: CGRect(x: 0, y: 0,
                                     width: screenWidth,
                                     height: screenHeight))
        return true
    }

    // MARK: - Touch Helpers

    private func performSwipe(
        _ cmd: IPCCommandPacket,
        eventSem: UnsafeMutablePointer<sem_t>?
    ) async {
        let p1       = CGPoint(x: Int(cmd.x),  y: Int(cmd.y))
        let p2       = CGPoint(x: Int(cmd.x2), y: Int(cmd.y2))
        let duration = Double(cmd.duration) / 1000.0
        let steps    = 20

        await MainActor.run { [weak self] in
            guard let self = self else { return }
            Toucher.touchcam(point: p1, phase: .began,
                             tid: &self.tid, actionName: "down", keyName: "maa_swipe")
        }
        try? await Task.sleep(nanoseconds: 10_000_000)  // 10ms 稳定触点

        for step in 1..<steps {
            let ratio = Double(step) / Double(steps)
            let mid = CGPoint(
                x: Double(cmd.x) + (Double(cmd.x2) - Double(cmd.x)) * ratio,
                y: Double(cmd.y) + (Double(cmd.y2) - Double(cmd.y)) * ratio
            )
            await MainActor.run { [weak self] in
                guard let self = self else { return }
                Toucher.touchcam(point: mid, phase: .moved,
                                 tid: &self.tid, actionName: "move", keyName: "maa_swipe")
            }
            try? await Task.sleep(
                nanoseconds: UInt64(duration / Double(steps) * 1_000_000_000)
            )
        }

        await MainActor.run { [weak self] in
            guard let self = self else { return }
            Toucher.touchcam(point: p2, phase: .ended,
                             tid: &self.tid, actionName: "up", keyName: "maa_swipe")
            Toucher.keyView = nil
        }
        sendEvent(type: .ack, reqSeqId: cmd.seqId, errorCode: 0, eventSem: eventSem)
    }

    private func performDrag(
        _ cmd: IPCCommandPacket,
        eventSem: UnsafeMutablePointer<sem_t>?
    ) async {
        let p1       = CGPoint(x: Int(cmd.x),  y: Int(cmd.y))
        let p2       = CGPoint(x: Int(cmd.x2), y: Int(cmd.y2))
        let duration = Double(cmd.duration) / 1000.0
        let steps    = 20

        await MainActor.run { [weak self] in
            guard let self = self else { return }
            Toucher.touchcam(point: p1, phase: .began,
                             tid: &self.tid, actionName: "down", keyName: "maa_drag")
        }
        // DRAG：长按后稍等 100ms 再移动，触发游戏的拖拽识别
        try? await Task.sleep(nanoseconds: 100_000_000)

        for step in 1..<steps {
            let ratio = Double(step) / Double(steps)
            let mid = CGPoint(
                x: Double(cmd.x) + (Double(cmd.x2) - Double(cmd.x)) * ratio,
                y: Double(cmd.y) + (Double(cmd.y2) - Double(cmd.y)) * ratio
            )
            await MainActor.run { [weak self] in
                guard let self = self else { return }
                Toucher.touchcam(point: mid, phase: .moved,
                                 tid: &self.tid, actionName: "move", keyName: "maa_drag")
            }
            try? await Task.sleep(
                nanoseconds: UInt64(duration / Double(steps) * 1_000_000_000)
            )
        }

        await MainActor.run { [weak self] in
            guard let self = self else { return }
            Toucher.touchcam(point: p2, phase: .ended,
                             tid: &self.tid, actionName: "up", keyName: "maa_drag")
            Toucher.keyView = nil
        }
        sendEvent(type: .ack, reqSeqId: cmd.seqId, errorCode: 0, eventSem: eventSem)
    }

    // MARK: - Event Writer

    private func sendEvent(
        type: IPCEventType,
        reqSeqId: UInt32,
        errorCode: Int32,
        eventSem: UnsafeMutablePointer<sem_t>?
    ) {
        let event = IPCEventPacket(
            type: type.rawValue,
            _pad: (0, 0, 0),
            reqSeqId: reqSeqId,
            errorCode: errorCode,
            reserved: (0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                       0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        )

        guard eventRing?.tryWrite(event) == true else {
            logger.error("Event ring full, dropping event type=\(type.rawValue) seq=\(reqSeqId)")
            return
        }

        if let sem = eventSem, sem_post(sem) != 0 {
            logger.error("sem_post(event) failed: errno=\(errno)")
        }
    }
}

// MARK: - SandboxIPCServerDelegate

extension MaaToolsIPC: SandboxIPCServerDelegate {

    /// 客户端握手：创建信号量，返回配置响应
    func server(_ server: SandboxIPCServer,
                buildResponseFor config: [String: Any]) -> [String: Any]? {

        // 确保屏幕尺寸可用（初始化时窗口可能尚未 ready）
        if screenWidth == 0 || screenHeight == 0 {
            // 以同步方式在主线程刷新一次
            let refreshDone = DispatchSemaphore(value: 0)
            DispatchQueue.main.async { [weak self] in
                self?.refreshScreenInfo()
                refreshDone.signal()
            }
            refreshDone.wait()
        }

        guard screenWidth > 0, screenHeight > 0 else {
            logger.error("Screen size unavailable (\(self.screenWidth)×\(self.screenHeight)), rejecting client")
            return nil
        }

        // 生成唯一的信号量名称（防止上一会话残留冲突）
        let timestamp     = Int(Date().timeIntervalSince1970 * 1000)
        let newCmdSemName = "/maa_cmd_\(timestamp)"
        let newEvtSemName = "/maa_evt_\(timestamp)"

        // 安全地清理可能残留的同名信号量
        sem_unlink(newCmdSemName)
        sem_unlink(newEvtSemName)

        // 创建命令信号量（Python sem_post，Swift sem_wait）
        guard let newCmdSem = sem_open(newCmdSemName,
                                       O_CREAT | O_EXCL,
                                       CUnsignedShort(0o666), 0),
              newCmdSem != SEM_FAILED else {
            logger.error("sem_open(cmd) failed: errno=\(errno)")
            return nil
        }

        // 创建事件信号量（Swift sem_post，Python sem_wait）
        guard let newEvtSem = sem_open(newEvtSemName,
                                       O_CREAT | O_EXCL,
                                       CUnsignedShort(0o666), 0),
              newEvtSem != SEM_FAILED else {
            logger.error("sem_open(event) failed: errno=\(errno)")
            sem_close(newCmdSem)
            sem_unlink(newCmdSemName)
            return nil
        }

        // 持久化到实例（didEstablishSession 中使用）
        self.cmdSem      = newCmdSem
        self.eventSem    = newEvtSem
        self.cmdSemName  = newCmdSemName
        self.eventSemName = newEvtSemName

        let screencapBytes = screenWidth * screenHeight * 4
        logger.info("✅ Handshake: sem created, screen \(self.screenWidth)×\(self.screenHeight)")

        return [
            "status":           "ok",
            "cmd_sem_name":     newCmdSemName,
            "event_sem_name":   newEvtSemName,
            "screencap_size":   screencapBytes,
            "width":            screenWidth,
            "height":           screenHeight
        ]
    }

    /// FD 接收完毕，建立会话并启动命令处理
    func server(_ server: SandboxIPCServer,
                didEstablishSessionWith socket: Int32,
                config: [String: Any],
                fds: [Int32]) {

        guard fds.count == 2 else {
            logger.error("Expected 2 FDs, got \(fds.count)")
            cleanupSession()
            return
        }

        let ipcFd       = fds[0]  // IPC 命令/事件环形缓冲区 fd
        let screencapFd = fds[1]  // 截图专用共享内存 fd

        // 1. 映射 IPC 环形缓冲区共享内存
        guard let ipcPtr = mmap(nil, IPCConfig.totalSize,
                                PROT_READ | PROT_WRITE, MAP_SHARED, ipcFd, 0),
              ipcPtr != MAP_FAILED else {
            logger.error("mmap(ipc) failed: errno=\(errno)")
            close(ipcFd); close(screencapFd)
            cleanupSession()
            return
        }
        self.ipcBasePtr  = ipcPtr
        self.ipcBaseSize = IPCConfig.totalSize
        close(ipcFd)   // mmap 保持映射，fd 可安全关闭

        // 2. 映射截图专用共享内存
        let capSize = screenWidth * screenHeight * 4
        guard let capPtr = mmap(nil, capSize,
                                PROT_READ | PROT_WRITE, MAP_SHARED, screencapFd, 0),
              capPtr != MAP_FAILED else {
            logger.error("mmap(screencap) failed: errno=\(errno)")
            close(screencapFd)
            cleanupSession()
            return
        }
        self.screencapBasePtr = capPtr
        self.screencapSize    = capSize
        close(screencapFd)

        // 3. 创建环形缓冲区（包装共享内存指针）
        cmdRing = ShmRingBuffer<IPCCommandPacket>(
            base: ipcPtr,
            dataOffset:       IPCConfig.cmdRingOffset,
            capacity:         IPCConfig.ringCapacity,
            writeIndexOffset: IPCConfig.cmdWriteIndexOffset,
            readIndexOffset:  IPCConfig.cmdReadIndexOffset
        )

        eventRing = ShmRingBuffer<IPCEventPacket>(
            base: ipcPtr,
            dataOffset:       IPCConfig.eventRingOffset,
            capacity:         IPCConfig.ringCapacity,
            writeIndexOffset: IPCConfig.eventWriteIndexOffset,
            readIndexOffset:  IPCConfig.eventReadIndexOffset
        )

        // 4. 激活会话，启动命令处理循环
        isSessionActive = true
        startProcessingLoop()

        logger.info("✅ Session established. IPC is ready.")
    }

    /// 客户端断开连接，清理本次 Session 所有资源
    func serverDidLoseConnection(_ server: SandboxIPCServer) {
        logger.info("Client disconnected, cleaning up...")
        cleanupSession()
    }
}
