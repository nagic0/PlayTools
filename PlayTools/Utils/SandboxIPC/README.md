# SandboxIPC

**PlayTools 进程间通信（IPC）通用底层库**

---

## 概述

`SandboxIPC` 是一套运行在 macOS 沙盒环境下的高性能 IPC 框架，
专为在 **PlayTools 注入进程** 与 **外部 Python 客户端** 之间进行低延迟通信而设计。

### 核心特性

- **零拷贝通信**：截图数据直接渲染到 Python 侧的共享内存，无额外拷贝
- **Cache Line 对齐**：环形缓冲区索引独占 64 字节 CPU Cache Line，消除伪共享
- **内存屏障**：通过 `OSMemoryBarrier()` 保证多核可见性
- **Lock-free 环形缓冲区**：SPSC 无锁设计（单生产者单消费者），最低延迟
- **文件描述符传递**：利用 `SCM_RIGHTS` 跨进程传递 `mmap fd`，无需共享命名 SHM
- **Mach 信号量**：使用 POSIX `sem_post/sem_wait` 通知，内核级零唤醒延迟

---

## 文件结构

```
SandboxIPC/
├── IPCSharedTypes.swift    # 共享数据结构、协议常量、C 宏辅助
├── ShmRingBuffer.swift     # 泛型共享内存环形缓冲区
├── SandboxIPCServer.swift  # Unix Socket 服务器（只负责传输，无业务逻辑）
└── README.md               # 本文档
```

业务实现位于上层：
```
Utils/
└── MaaToolsIPC.swift       # 业务逻辑（截图/触控），实现 SandboxIPCServerDelegate
```

---

## 通信协议

### 握手流程

```
Client (Python)                          Server (Swift / SandboxIPCServer)
────────────────────────────────────────────────────────────────────────
1. connect()                    →
                                ←   accept()

2. send(JSON config)            →
   {
     "shm_name": "/maa_ipc_xxx"        # Python 创建的命令/事件 SHM 名称
   }

                                    delegate.buildResponseFor(config)
                                    → 创建 POSIX 信号量
                                    → 返回响应字典
                                ←   send(JSON response)
                                    {
                                      "status": "ok",
                                      "cmd_sem_name":   "/maa_cmd_xxx",
                                      "event_sem_name": "/maa_evt_xxx",
                                      "screencap_size": 4915200,
                                      "width": 1920,
                                      "height": 1080
                                    }

3. 创建截图 SHM
   send([ipcFd, screencapFd]    →   # SCM_RIGHTS 传递两个 fd
    via SCM_RIGHTS)

                                ←   send("FD_ACK")

                                    delegate.didEstablishSession(fds:)
                                    → 映射共享内存
                                    → 打开信号量
                                    → 启动命令处理循环

4. [heartbeat loop]             →   [monitorConnection: recv() 阻塞]

5. close() / crash              →   recv() 返回 0
                                    delegate.serverDidLoseConnection()
                                    → 清理资源
                                    → 等待下一个客户端
```

### 共享内存布局

```
IPC SHM (由 Python 创建，通过 fd 传给 Swift)
┌────────────────────────────────────────────────┐
│ Header (256 bytes)                              │
│  offset   0 : cmdWriteIndex  [UInt32] 外部写    │  ← Python 写
│  offset  64 : cmdReadIndex   [UInt32] App 写    │  ← Swift 写
│  offset 128 : eventWriteIndex [UInt32] App 写   │  ← Swift 写
│  offset 192 : eventReadIndex  [UInt32] 外部写   │  ← Python 写
├────────────────────────────────────────────────┤
│ Command Ring  (2048 × 32 bytes = 64 KB)         │
│  IPCCommandPacket × 2048                        │
├────────────────────────────────────────────────┤
│ Event Ring    (2048 × 32 bytes = 64 KB)         │
│  IPCEventPacket × 2048                          │
└────────────────────────────────────────────────┘

Screencap SHM (由 Python 创建，通过 fd 传给 Swift)
┌────────────────────────────────────────────────┐
│ Raw RGBA Pixels (width × height × 4 bytes)      │
│  Swift 直接渲染到此内存，Python 直接读取          │
└────────────────────────────────────────────────┘
```

### 命令/事件类型

| 命令 (Python → Swift) | 值 | 说明                            |
|-----------------------|----|-------------------------------|
| `CMD_SCREENSHOT`      | 0  | 截图，结果写入 Screencap SHM     |
| `CMD_TAP`             | 1  | 点击 (x, y, duration)           |
| `CMD_SWIPE`           | 2  | 滑动 (x1,y1) → (x2,y2, duration)|
| `CMD_DRAG`            | 3  | 拖拽（长按后移动）                |
| `CMD_GET_SIZE`        | 4  | 查询屏幕尺寸                     |
| `CMD_GET_VERSION`     | 5  | 查询协议版本号                   |

| 事件 (Swift → Python) | 值 | 说明                                         |
|-----------------------|----|---------------------------------------------|
| `EVENT_SCREENSHOT_READY` | 0  | 截图完成，Screencap SHM 中有新帧          |
| `EVENT_ACK`           | 1  | 命令执行成功                                 |
| `EVENT_ERROR`         | 2  | 命令执行失败（errorCode 携带错误码）          |
| `EVENT_SIZE_INFO`     | 3  | 屏幕尺寸（低16位=宽，高16位=高，编码在 errorCode）|
| `EVENT_VERSION_INFO`  | 4  | 协议版本号（编码在 errorCode）               |

---

## 如何使用

### 1. 实现 `SandboxIPCServerDelegate`

```swift
import UIKit
import OSLog

final class MyIPCHandler: SandboxIPCServerDelegate {

    private let server: SandboxIPCServer
    private var cmdRing: ShmRingBuffer<IPCCommandPacket>?
    private var eventRing: ShmRingBuffer<IPCEventPacket>?

    init() {
        let socketPath = NSHomeDirectory() + "/tmp/maa_ipc.sock"
        server = SandboxIPCServer(socketPath: socketPath)
        server.delegate = self
    }

    func start() {
        server.start()
    }

    // MARK: - Delegate

    func server(_ server: SandboxIPCServer,
                buildResponseFor config: [String: Any]) -> [String: Any]? {
        // 1. 创建 POSIX 信号量
        let ts = Int(Date().timeIntervalSince1970 * 1000)
        let cmdSemName = "/my_cmd_\(ts)"
        let evtSemName = "/my_evt_\(ts)"
        // ... sem_open(O_CREAT | O_EXCL) ...

        // 2. 返回握手响应
        return [
            "status": "ok",
            "cmd_sem_name": cmdSemName,
            "event_sem_name": evtSemName,
            "screencap_size": 1920 * 1080 * 4,
            "width": 1920,
            "height": 1080
        ]
    }

    func server(_ server: SandboxIPCServer,
                didEstablishSessionWith socket: Int32,
                config: [String: Any],
                fds: [Int32]) {
        // 1. mmap 共享内存
        let base = mmap(nil, IPCConfig.totalSize,
                        PROT_READ | PROT_WRITE, MAP_SHARED, fds[0], 0)!
        // 2. 创建环形缓冲区
        cmdRing = ShmRingBuffer<IPCCommandPacket>(
            base: base,
            dataOffset: IPCConfig.cmdRingOffset,
            capacity: IPCConfig.ringCapacity,
            writeIndexOffset: IPCConfig.cmdWriteIndexOffset,
            readIndexOffset: IPCConfig.cmdReadIndexOffset
        )
        // 3. 开始处理循环...
    }

    func serverDidLoseConnection(_ server: SandboxIPCServer) {
        // 清理资源
        cmdRing = nil
        eventRing = nil
    }
}
```

### 2. 向事件环写入响应

```swift
var event = IPCEventPacket(
    type: IPCEventType.ack.rawValue,
    _pad: (0, 0, 0),
    reqSeqId: cmd.seqId,
    errorCode: 0,
    reserved: (/* 20 zeros */)
)
eventRing.tryWrite(event)
sem_post(eventSemaphore)  // 通知 Python
```

---

## 设计决策

### 为何使用文件描述符传递而非命名 SHM？

macOS 沙盒限制了 `shm_open` 命名空间的访问权限，但传递已打开的 fd 不受此限制。
Python 侧在无沙盒下创建 SHM，再通过 `SCM_RIGHTS` 安全地把 fd 传入沙盒进程。

### 为何索引范围是 `[0, capacity*2)`？

若索引直接用 `% capacity` 包裹，**满**和**空**均表现为 `writeIdx == readIdx`，
无法区分。使用 `2 * capacity` 范围后：
- 空：`readIdx == writeIdx`
- 满：`writeIdx % capacity == readIdx % capacity && writeIdx != readIdx`

### Cache Line 对齐的意义

四个索引分别由不同角色读写：
- `cmdWriteIndex`：Python 写，Swift 读
- `cmdReadIndex`：Swift 写，Python 读
- `eventWriteIndex`：Swift 写，Python 读
- `eventReadIndex`：Python 写，Swift 读

若多个索引共处一个 Cache Line（64 字节），一侧写入会导致另一侧的读取产生
**伪共享（False Sharing）**缓存失效，使延迟大幅上升。每个索引独占一个 64 字节
Cache Line 可完全消除此问题。
