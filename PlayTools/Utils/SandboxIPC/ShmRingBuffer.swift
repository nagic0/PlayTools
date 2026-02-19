//
//  ShmRingBuffer.swift
//  PlayTools
//
//  SandboxIPC 子模块 - 基于共享内存的泛型环形缓冲区
//
//  特性：
//  - 零拷贝：直接在共享内存上进行 load/store，无额外内存分配
//  - Lock-free：基于原子索引更新，无锁设计（SPSC：单生产者单消费者）
//  - Memory Barrier：通过 OSMemoryBarrier() 保证多核可见性顺序
//  - 索引使用 0..<(capacity*2) 范围，通过 % capacity 映射到实际槽位，
//    避免写满时索引追及问题
//
//  用法示例（由 MaaToolsIPC 使用）：
//
//    // 创建命令环形缓冲区（外部写、App 读）
//    let cmdRing = ShmRingBuffer<IPCCommandPacket>(
//        base: ipcBasePtr,
//        dataOffset: IPCConfig.cmdRingOffset,
//        capacity: IPCConfig.ringCapacity,
//        writeIndexOffset: IPCConfig.cmdWriteIndexOffset,
//        readIndexOffset: IPCConfig.cmdReadIndexOffset
//    )
//
//    // 消费者读取
//    while let cmd = cmdRing.tryRead() {
//        handle(cmd)
//    }
//
//    // 生产者写入
//    let event = IPCEventPacket(...)
//    eventRing.tryWrite(event)
//

import Foundation
import Darwin

/// 基于共享内存的 SPSC（单生产者单消费者）无锁环形缓冲区
///
/// - 类型参数 `T`：数据包类型，必须是值类型（struct）
/// - 索引值域：`[0, capacity * 2)`，通过 `% capacity` 映射到实际槽位，
///   这样可以用 `writeIdx == readIdx` 表示空，同时在 capacity 倍循环下仍正确
final class ShmRingBuffer<T> {

    /// 环形缓冲区容量（槽位数）
    let capacity: Int

    // 写索引指针（生产者更新）
    private let writeIndexPtr: UnsafeMutablePointer<UInt32>

    // 读索引指针（消费者更新）
    private let readIndexPtr: UnsafeMutablePointer<UInt32>

    // 数据区基址
    private let dataPtr: UnsafeMutableRawPointer

    // 单个元素步长（包含对齐填充）
    private let stride: Int

    /// 初始化环形缓冲区
    ///
    /// - Parameters:
    ///   - base: 共享内存区域的基址（来自 mmap）
    ///   - dataOffset: 数据区相对于 base 的字节偏移
    ///   - capacity: 缓冲区槽位数（必须为 2 的幂以获得最佳性能）
    ///   - writeIndexOffset: 写索引（UInt32）相对于 base 的字节偏移
    ///   - readIndexOffset:  读索引（UInt32）相对于 base 的字节偏移
    init(base: UnsafeMutableRawPointer,
         dataOffset: Int,
         capacity: Int,
         writeIndexOffset: Int,
         readIndexOffset: Int) {
        self.capacity = capacity
        self.stride = MemoryLayout<T>.stride
        self.dataPtr = base.advanced(by: dataOffset)
        self.writeIndexPtr = base.advanced(by: writeIndexOffset)
            .assumingMemoryBound(to: UInt32.self)
        self.readIndexPtr = base.advanced(by: readIndexOffset)
            .assumingMemoryBound(to: UInt32.self)
    }

    // MARK: - 生产者 API

    /// 尝试写入一条数据（生产者调用）
    ///
    /// - Parameter item: 要写入的数据包
    /// - Returns: 写入成功返回 `true`；缓冲区已满返回 `false`
    @discardableResult
    func tryWrite(_ item: T) -> Bool {
        let writeIdx = Int(writeIndexPtr.pointee)
        let readIdx  = Int(readIndexPtr.pointee)

        // 检查缓冲区是否已满
        // 当 wrappedWriteSlot == readSlot 且写计数比读计数多一轮时为满
        let writeSlot = writeIdx % capacity
        let readSlot  = readIdx  % capacity
        let writeLap  = writeIdx / capacity
        let readLap   = readIdx  / capacity

        if writeSlot == readSlot && writeLap != readLap {
            return false  // 缓冲区已满
        }

        // 写入数据到对应槽位
        dataPtr.advanced(by: writeSlot * stride)
            .storeBytes(of: item, as: T.self)

        // ⚡ 内存屏障：确保数据写入对其他 CPU 核心可见后再更新写索引
        OSMemoryBarrier()

        // 更新写索引（范围 0..<capacity*2，避免追及问题）
        writeIndexPtr.pointee = UInt32((writeIdx + 1) % (capacity * 2))
        return true
    }

    // MARK: - 消费者 API

    /// 尝试读取一条数据（消费者调用）
    ///
    /// - Returns: 有数据时返回数据包；缓冲区为空返回 `nil`
    func tryRead() -> T? {
        let readIdx  = Int(readIndexPtr.pointee)
        let writeIdx = Int(writeIndexPtr.pointee)

        // 检查缓冲区是否为空
        if readIdx == writeIdx {
            return nil
        }

        let readSlot = readIdx % capacity

        // 读取数据
        let item = dataPtr.advanced(by: readSlot * stride)
            .load(as: T.self)

        // ⚡ 内存屏障：确保数据读取完成后再更新读索引
        OSMemoryBarrier()

        // 更新读索引
        readIndexPtr.pointee = UInt32((readIdx + 1) % (capacity * 2))
        return item
    }

    // MARK: - 状态查询

    /// 当前缓冲区中的数据条数（近似值，多核环境下可能不精确）
    var count: Int {
        let writeIdx = Int(writeIndexPtr.pointee)
        let readIdx  = Int(readIndexPtr.pointee)
        if writeIdx >= readIdx {
            return writeIdx - readIdx
        } else {
            return (capacity * 2) - readIdx + writeIdx
        }
    }

    /// 缓冲区是否为空
    var isEmpty: Bool {
        return readIndexPtr.pointee == writeIndexPtr.pointee
    }
}
