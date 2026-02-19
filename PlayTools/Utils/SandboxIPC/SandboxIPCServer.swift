//
//  SandboxIPCServer.swift
//  PlayTools
//
//  SandboxIPC 子模块 - 通用 Unix Socket 服务器
//
//  负责以下 纯粹的 IPC 传输工作，不含任何业务逻辑：
//  1. 在后台 DispatchQueue 上运行 Accept Loop，持续监听新连接
//  2. 接收客户端发来的 JSON 握手配置
//  3. 调用 delegate 获取握手响应，并将其发回客户端
//  4. 通过 SCM_RIGHTS（sendmsg/recvmsg）接收客户端传递的文件描述符
//  5. 通知 delegate 会话已建立或连接已断开
//  6. 连接断开后自动重置，等待下一个客户端连接
//
// -----------------------------------------------------------------
//  握手流程时序：
//
//  Client (Python)                     Server (Swift)
//  ─────────────────────────────────────────────────
//    connect()              →
//                           ←  [accept]
//    send(JSON config)      →
//                               delegate.buildResponse(config)
//                           ←  send(JSON response + "\n")
//    send(FDs via SCM_RIGHTS) →
//                           ←  send("FD_ACK")
//                               delegate.didEstablishSession(fds:)
//    [heartbeat loop]       →   [monitor loop]
//    close()                 →
//                               delegate.didLoseConnection()
//                               [ready for next client]
// -----------------------------------------------------------------
//

import Foundation
import OSLog
import Darwin

// MARK: - Delegate Protocol

/// `SandboxIPCServer` 的委托协议
///
/// 所有方法均在服务器的后台队列（非主线程）上调用。
/// 如需操作 UI，请自行通过 `DispatchQueue.main` 或 `Task { @MainActor in ... }` 跳转。
protocol SandboxIPCServerDelegate: AnyObject {

    /// 有新客户端发来握手 JSON，delegate 需要返回握手响应字典。
    ///
    /// - Parameters:
    ///   - server: 调用方
    ///   - config: 客户端发来的 JSON 配置（含 `shm_name` 等字段）
    /// - Returns: 要发给客户端的响应字典（含状态、信号量名等），返回 `nil` 则拒绝连接
    func server(_ server: SandboxIPCServer,
                buildResponseFor config: [String: Any]) -> [String: Any]?

    /// 握手完成，文件描述符已接收，会话正式建立。
    ///
    /// - Parameters:
    ///   - server: 调用方
    ///   - socket: 客户端连接的 socket fd（用于后续断开检测）
    ///   - config: 客户端原始 JSON 配置
    ///   - fds: 通过 SCM_RIGHTS 接收到的文件描述符数组（[ipcFd, screencapFd]）
    func server(_ server: SandboxIPCServer,
                didEstablishSessionWith socket: Int32,
                config: [String: Any],
                fds: [Int32])

    /// 客户端连接已断开（网络错误或对端主动关闭）。
    func serverDidLoseConnection(_ server: SandboxIPCServer)
}

// MARK: - SandboxIPCServer

/// 通用 Unix Socket IPC 服务器
///
/// 在后台线程上运行 accept loop，处理一个客户端后等待其断开再接受下一个。
/// 纯粹负责网络 IO 和文件描述符传输，不含任何业务逻辑。
final class SandboxIPCServer {

    // MARK: Properties

    weak var delegate: SandboxIPCServerDelegate?

    private let socketPath: String
    private let logger: Logger
    private var isRunning = false
    private let queue = DispatchQueue(label: "com.playtools.sandbox-ipc-server",
                                     qos: .userInitiated)

    // MARK: Init

    /// 创建服务器
    /// - Parameter socketPath: Unix Domain Socket 文件路径（位于沙盒 tmp 目录）
    init(socketPath: String,
         logger: Logger = Logger(subsystem: "PlayTools", category: "SandboxIPCServer")) {
        self.socketPath = socketPath
        self.logger = logger
    }

    // MARK: Lifecycle

    /// 启动服务器（非阻塞，在后台 queue 上运行 accept loop）
    func start() {
        guard !isRunning else {
            logger.warning("SandboxIPCServer already running")
            return
        }
        isRunning = true
        logger.info("Starting SandboxIPCServer at \(self.socketPath)")
        queue.async { [weak self] in
            self?.acceptLoop()
        }
    }

    /// 停止服务器
    func stop() {
        isRunning = false
        unlink(socketPath)
        logger.info("SandboxIPCServer stopped")
    }

    // MARK: - Accept Loop

    private func acceptLoop() {
        while isRunning {
            // 删除可能残留的旧 socket 文件
            unlink(socketPath)

            // 创建服务器 socket
            let serverFd = socket(AF_UNIX, SOCK_STREAM, 0)
            guard serverFd >= 0 else {
                logger.error("socket() failed: errno=\(errno)")
                Thread.sleep(forTimeInterval: 5.0)
                continue
            }
            defer { close(serverFd) }

            // 构造地址结构并绑定
            guard bindSocket(serverFd) else {
                Thread.sleep(forTimeInterval: 5.0)
                continue
            }

            // 开始监听（最多 1 个排队连接）
            guard listen(serverFd, 1) == 0 else {
                logger.error("listen() failed: errno=\(errno)")
                Thread.sleep(forTimeInterval: 5.0)
                continue
            }

            logger.info("Listening at \(self.socketPath), waiting for client...")

            // 阻塞等待客户端连接
            let clientFd = accept(serverFd, nil, nil)
            guard clientFd >= 0 else {
                if isRunning {
                    logger.error("accept() failed: errno=\(errno)")
                }
                continue
            }

            logger.info("Client connected (fd=\(clientFd))")

            // 处理该客户端直到断开
            handleClient(clientFd)
        }

        // 清理
        unlink(socketPath)
        logger.info("Accept loop exited")
    }

    // MARK: - Bind Helper

    private func bindSocket(_ serverFd: Int32) -> Bool {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)

        let pathBytes = socketPath.utf8CString
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            logger.error("Socket path too long: \(self.socketPath)")
            close(serverFd)
            return false
        }

        // 将路径安全地拷贝到 sun_path 字段
        withUnsafeMutableBytes(of: &addr.sun_path) { dstPtr in
            pathBytes.withUnsafeBytes { srcPtr in
                dstPtr.copyBytes(from: UnsafeRawBufferPointer(
                    start: srcPtr.baseAddress,
                    count: min(srcPtr.count, dstPtr.count)
                ))
            }
        }

        // sun_len = family(1) + path length (含 NUL)
        let addrLen = socklen_t(
            MemoryLayout<sa_family_t>.size + MemoryLayout<UInt8>.size + pathBytes.count
        )

        let bindResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.bind(serverFd, sockPtr, addrLen)
            }
        }

        guard bindResult == 0 else {
            logger.error("bind() failed: errno=\(errno)")
            return false
        }
        return true
    }

    // MARK: - Client Handling

    private func handleClient(_ clientFd: Int32) {
        defer {
            close(clientFd)
        }

        // ── Step 1: 接收 JSON 配置 ──────────────────────────────
        guard let config = receiveJSONConfig(from: clientFd) else {
            logger.error("Failed to receive JSON config from client")
            return
        }
        logger.info("Received config: \(config.keys.joined(separator: ", "))")

        // ── Step 2: 请求 delegate 构建握手响应 ─────────────────
        guard let response = delegate?.server(self, buildResponseFor: config) else {
            logger.warning("Delegate rejected client connection")
            // 发送拒绝消息
            sendJSON(["status": "rejected", "message": "server not ready"], to: clientFd)
            return
        }

        // ── Step 3: 发送握手响应 ───────────────────────────────
        guard sendJSON(response, to: clientFd) else {
            logger.error("Failed to send handshake response")
            return
        }
        logger.info("Sent handshake response")

        // ── Step 4: 接收文件描述符（SCM_RIGHTS）──────────────
        guard let fds = receiveFDs(from: clientFd, count: 2) else {
            logger.error("Failed to receive file descriptors")
            return
        }
        logger.info("Received \(fds.count) FDs: \(fds)")

        // ── Step 5: 发送 FD_ACK ────────────────────────────────
        let ack = "FD_ACK"
        let ackSent = ack.withCString { ptr in
            Darwin.send(clientFd, ptr, strlen(ptr), 0) > 0
        }
        guard ackSent else {
            logger.error("Failed to send FD_ACK")
            fds.forEach { close($0) }
            return
        }

        // ── Step 6: 通知 delegate 会话建立 ────────────────────
        delegate?.server(self, didEstablishSessionWith: clientFd, config: config, fds: fds)

        // ── Step 7: 心跳监控（阻塞直到客户端断开）─────────────
        monitorConnection(clientFd)

        // ── Step 8: 通知 delegate 连接断开 ────────────────────
        logger.info("Client disconnected, notifying delegate")
        delegate?.serverDidLoseConnection(self)
    }

    // MARK: - I/O Helpers

    /// 接收以 "\n" 结尾的 JSON 数据，解析为字典
    private func receiveJSONConfig(from fd: Int32) -> [String: Any]? {
        var buffer = [UInt8](repeating: 0, count: 4096)
        var received = Data()

        // 读取直到遇到换行符
        while !received.contains(UInt8(ascii: "\n")) {
            let bytesReceived = recv(fd, &buffer, buffer.count, 0)
            guard bytesReceived > 0 else {
                logger.error("recv() failed: n=\(bytesReceived), errno=\(errno)")
                return nil
            }
            received.append(contentsOf: buffer[0..<bytesReceived])
        }

        guard let jsonData = received.split(separator: UInt8(ascii: "\n"), maxSplits: 1).first,
              let config = try? JSONSerialization.jsonObject(with: Data(jsonData)) as? [String: Any] else {
            logger.error("Failed to parse JSON config")
            return nil
        }
        return config
    }

    /// 发送 JSON 字典作为 "{...}\n" 格式
    @discardableResult
    private func sendJSON(_ dict: [String: Any], to fd: Int32) -> Bool {
        guard let data = try? JSONSerialization.data(withJSONObject: dict),
              var str = String(data: data, encoding: .utf8) else {
            return false
        }
        str += "\n"
        return str.withCString { ptr in
            Darwin.send(fd, ptr, strlen(ptr), 0) > 0
        }
    }

    /// 通过 SCM_RIGHTS 接收 `count` 个文件描述符
    private func receiveFDs(from fd: Int32, count: Int) -> [Int32]? {
        var msg = msghdr()
        var iov = iovec()

        // 必须接收至少 1 字节数据（SCM_RIGHTS 要求有 data payload）
        var dummy = [UInt8](repeating: 0, count: 64)
        dummy.withUnsafeMutableBufferPointer { ptr in
            iov.iov_base = UnsafeMutableRawPointer(ptr.baseAddress)
            iov.iov_len = ptr.count
        }
        msg.msg_iov = withUnsafeMutablePointer(to: &iov) { $0 }
        msg.msg_iovlen = 1

        // 分配 ancillary data 缓冲区（足够容纳 count 个 Int32 fd）
        let cmsgSize = ipc_CMSG_SPACE(UInt32(MemoryLayout<Int32>.size * count))
        var cmsgBuffer = [UInt8](repeating: 0, count: cmsgSize)

        cmsgBuffer.withUnsafeMutableBufferPointer { ptr in
            msg.msg_control = UnsafeMutableRawPointer(ptr.baseAddress)
            msg.msg_controllen = socklen_t(cmsgSize)
        }

        let recvRet = recvmsg(fd, &msg, 0)
        guard recvRet > 0 else {
            logger.error("recvmsg() failed: ret=\(recvRet), errno=\(errno)")
            return nil
        }

        // 解析 ancillary data
        var msgCopy = msg
        guard let cmsg = withUnsafePointer(to: &msgCopy, ipc_CMSG_FIRSTHDR),
              cmsg.pointee.cmsg_level == SOL_SOCKET,
              cmsg.pointee.cmsg_type == SCM_RIGHTS else {
            logger.error("No valid SCM_RIGHTS in ancillary data")
            return nil
        }

        let fdPtr = ipc_CMSG_DATA(cmsg).bindMemory(to: Int32.self, capacity: count)
        return (0..<count).map { fdPtr[$0] }
    }

    /// 阻塞读取心跳字节，直到连接断开
    private func monitorConnection(_ fd: Int32) {
        logger.debug("Connection monitor started for fd=\(fd)")
        var byte = [UInt8](repeating: 0, count: 1)
        while true {
            let result = recv(fd, &byte, 1, 0)
            if result <= 0 {
                logger.info("Connection lost (recv returned \(result), errno=\(errno))")
                break
            }
            // 忽略心跳字节内容，只是用来检测连接状态
        }
    }
}
