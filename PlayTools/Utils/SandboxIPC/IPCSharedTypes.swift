//
//  IPCSharedTypes.swift
//  PlayTools
//
//  SandboxIPC 子模块 - 进程间通信共享数据结构与常量定义
//
//  该文件定义了 PlayTools (Swift 沙盒进程) 与外部客户端 (Python) 之间
//  通过共享内存通信时使用的所有数据结构、协议常量和 C 宏辅助函数。
//
//  内存布局（与 Python 客户端严格对齐）：
//  ┌─────────────────────────────────────────────────────────┐
//  │ Header (256 bytes)                                      │
//  │   offset   0 : cmdWriteIndex  (UInt32, 外部写)          │
//  │   offset  64 : cmdReadIndex   (UInt32, App 写)          │
//  │   offset 128 : eventWriteIndex (UInt32, App 写)         │
//  │   offset 192 : eventReadIndex  (UInt32, 外部写)         │
//  ├─────────────────────────────────────────────────────────┤
//  │ Command Ring (2048 × 32 bytes = 64 KB)                  │
//  ├─────────────────────────────────────────────────────────┤
//  │ Event Ring   (2048 × 32 bytes = 64 KB)                  │
//  └─────────────────────────────────────────────────────────┘
//

import Foundation
import Darwin

// MARK: - C 宏辅助函数（Swift 实现）
// CMSG_* 宏在 Swift 中不可用，需要手动实现以支持 SCM_RIGHTS 文件描述符传递

/// 计算 ancillary data 所需的缓冲区大小（包含对齐填充）
@inline(__always)
func ipc_CMSG_SPACE(_ length: UInt32) -> Int {
    let headerSize = MemoryLayout<cmsghdr>.size
    let align = MemoryLayout<Int>.size
    return (headerSize + Int(length) + align - 1) & ~(align - 1)
}

/// 获取 msghdr 中第一个 cmsghdr 的指针
@inline(__always)
func ipc_CMSG_FIRSTHDR(_ msg: UnsafePointer<msghdr>) -> UnsafeMutablePointer<cmsghdr>? {
    guard msg.pointee.msg_controllen >= socklen_t(MemoryLayout<cmsghdr>.size) else {
        return nil
    }
    return msg.pointee.msg_control?.assumingMemoryBound(to: cmsghdr.self)
}

/// 获取 cmsghdr 数据区的起始指针
@inline(__always)
func ipc_CMSG_DATA(_ cmsg: UnsafeMutablePointer<cmsghdr>) -> UnsafeMutableRawPointer {
    return UnsafeMutableRawPointer(cmsg).advanced(by: MemoryLayout<cmsghdr>.size)
}

// MARK: - 命令类型枚举

/// IPC 命令类型（外部 → App）
/// 触摸协议与 TCP 版（MaaTools.swift TUCH 命令）保持一致：
/// 上层不再发送高层 tap/swipe/drag，改为逐帧发送 touchDown/touchMove/touchUp，
/// 插值、时序全部由 C++ Controller 负责。
enum IPCCommandType: UInt8 {
    case screenshot  = 0   // 截图
    case touchDown   = 1   // 触摸按下（对应 UITouch.Phase.began）
    case touchMove   = 2   // 触摸移动（对应 UITouch.Phase.moved）
    case touchUp     = 3   // 触摸抬起（对应 UITouch.Phase.ended）
    case getSize     = 4   // 查询屏幕尺寸
    case getVersion  = 5   // 查询协议版本
    case terminate   = 6   // 终止游戏进程（对应 TCP 的 TERM 命令）
}

/// IPC 事件类型（App → 外部）
enum IPCEventType: UInt8 {
    case screenshotReady = 0   // 截图就绪（数据在截图共享内存中）
    case ack             = 1   // 命令已执行确认
    case error           = 2   // 命令执行失败
    case sizeInfo        = 3   // 屏幕尺寸响应（编码在 errorCode 字段）
    case versionInfo     = 4   // 协议版本响应（编码在 errorCode 字段）
}

// MARK: - 数据包结构（与 Python struct 格式严格对齐）

/// 命令包（外部 → App），固定 32 字节
/// Python pack format: '<B3xIiiiiI4B'
struct IPCCommandPacket {
    var type: UInt8           // 命令类型，IPCCommandType
    var _pad: (UInt8, UInt8, UInt8) = (0, 0, 0)  // 3 字节对齐填充
    var seqId: UInt32         // 请求序列号（用于响应匹配）
    // swiftlint:disable identifier_name
    var x: Int32              // X 坐标（或第一个点 X）
    var y: Int32              // Y 坐标（或第一个点 Y）
    // swiftlint:enable identifier_name
    var x2: Int32             // 第二个点 X（SWIPE/DRAG 使用）
    var y2: Int32             // 第二个点 Y（SWIPE/DRAG 使用）
    var duration: UInt32      // 持续时间（毫秒）
    // swiftlint:disable:next large_tuple
    var reserved: (UInt8, UInt8, UInt8, UInt8) = (0, 0, 0, 0)  // 保留字段
    // 总计 32 字节
}

/// 事件包（App → 外部），固定 32 字节
/// Python pack format: '<B3xIi'（前 12 字节有效，其余保留）
struct IPCEventPacket {
    var type: UInt8           // 事件类型，IPCEventType
    var _pad: (UInt8, UInt8, UInt8) = (0, 0, 0)  // 3 字节对齐填充
    var reqSeqId: UInt32      // 对应请求的序列号
    var errorCode: Int32      // 错误码 / 附加数据
    // 20 字节保留字段，补齐至 32 字节
    // swiftlint:disable:next large_tuple
    var reserved: (UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
                   UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8, UInt8,
                   UInt8, UInt8, UInt8, UInt8) = (
                        0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                        0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    // 总计 32 字节
}

// MARK: - 内存布局常量

/// IPC 共享内存布局的编译期常量
/// 这些偏移量必须与 Python 客户端严格保持一致
enum IPCConfig {
    /// 头部区域大小（字节），包含 4 个 64 字节对齐的索引
    static let headerSize = 256

    /// 环形缓冲区容量（槽位数）
    static let ringCapacity = 2048

    /// 头部内各索引字段的偏移量（每个独占一个 64 字节 CPU Cache Line）
    static let cmdWriteIndexOffset   = 0    // 外部写、App 读
    static let cmdReadIndexOffset    = 64   // App 写、外部读
    static let eventWriteIndexOffset = 128  // App 写、外部读
    static let eventReadIndexOffset  = 192  // 外部写、App 读

    /// 单个命令包大小（字节）
    static let cmdPacketSize = MemoryLayout<IPCCommandPacket>.stride  // 32 bytes

    /// 单个事件包大小（字节）
    static let eventPacketSize = MemoryLayout<IPCEventPacket>.stride  // 32 bytes

    /// IPC 共享内存总大小（字节）~ 132 KB
    static let totalSize = headerSize + ringCapacity * cmdPacketSize + ringCapacity * eventPacketSize

    /// 命令环形缓冲区数据区的起始偏移
    static let cmdRingOffset = headerSize

    /// 事件环形缓冲区数据区的起始偏移
    static let eventRingOffset = headerSize + ringCapacity * cmdPacketSize

    /// 当前协议版本号（对应 TCP 侧 MinimalVersion = 2）
    static let protocolVersion: UInt32 = 2

    /// 服务端所需的最低客户端协议版本
    static let minimalVersion: UInt32 = 2

    /// 屏幕尺寸编码：encode(w, h) → Int32
    /// 低 16 位 = 宽度，高 16 位 = 高度
    static func encodeScreenSize(width: Int, height: Int) -> Int32 {
        return Int32((width & 0xFFFF) | ((height & 0xFFFF) << 16))
    }

    /// 屏幕尺寸解码
    static func decodeScreenSize(_ encoded: Int32) -> (width: Int, height: Int) {
        let decodedWidth  = Int(encoded & 0xFFFF)
        let decodedHeight = Int((encoded >> 16) & 0xFFFF)
        return (decodedWidth, decodedHeight)
    }
}
