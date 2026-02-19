#!/usr/bin/env python3
"""
MaaTools IPC 客户端 - POSIX 共享内存 + Mach 信号量 + 文件描述符传递版本

高性能、低延迟实现：
- TCP 握手进行初始化和心跳监控
- Unix Socket 传递文件描述符（SCM_RIGHTS）
- POSIX 共享内存（shm_open/mmap）
- Mach 信号量（sem_open/sem_wait/sem_post）- 零延迟唤醒
"""

import os
import sys
import time
import json
import struct
import mmap
import socket
import ctypes
import threading
import array
from pathlib import Path
from typing import Optional, Tuple

# ============================================================
# C 库绑定
# ============================================================

libc = ctypes.CDLL(None, use_errno=True)

# POSIX 共享内存
libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
libc.shm_open.restype = ctypes.c_int
libc.shm_unlink.argtypes = [ctypes.c_char_p]
libc.shm_unlink.restype = ctypes.c_int
libc.ftruncate.argtypes = [ctypes.c_int, ctypes.c_long]
libc.ftruncate.restype = ctypes.c_int

# POSIX 信号量
SEM_FAILED = ctypes.c_void_p(-1).value

libc.sem_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
libc.sem_open.restype = ctypes.c_void_p
libc.sem_close.argtypes = [ctypes.c_void_p]
libc.sem_close.restype = ctypes.c_int
libc.sem_unlink.argtypes = [ctypes.c_char_p]
libc.sem_unlink.restype = ctypes.c_int
libc.sem_wait.argtypes = [ctypes.c_void_p]
libc.sem_wait.restype = ctypes.c_int
libc.sem_post.argtypes = [ctypes.c_void_p]
libc.sem_post.restype = ctypes.c_int
libc.sem_trywait.argtypes = [ctypes.c_void_p]
libc.sem_trywait.restype = ctypes.c_int

# 常量
O_RDWR = 0x0002
O_CREAT = 0x0200
O_EXCL = 0x0800

# ============================================================
# 协议定义
# ============================================================

# 命令类型
CMD_SCREENSHOT = 0
CMD_TAP = 1
CMD_SWIPE = 2
CMD_DRAG = 3
CMD_GET_SIZE = 4
CMD_GET_VERSION = 5

# 事件类型
EVENT_SCREENSHOT_READY = 0
EVENT_ACK = 1
EVENT_ERROR = 2
EVENT_SIZE_INFO = 3
EVENT_VERSION_INFO = 4

# 环形缓冲区大小
RING_SIZE = 2048

# 内存布局（与 Swift 侧一致）
HEADER_SIZE = 256
CMD_PACKET_SIZE = 32  # CommandPacket
EVENT_PACKET_SIZE = 32  # EventPacket
CMD_RING_SIZE = RING_SIZE * CMD_PACKET_SIZE
EVENT_RING_SIZE = RING_SIZE * EVENT_PACKET_SIZE
TOTAL_IPC_SIZE = HEADER_SIZE + CMD_RING_SIZE + EVENT_RING_SIZE

# ============================================================
# MaaToolsIPC 客户端
# ============================================================

class MaaToolsIPC:
    def __init__(self, bundle_id: Optional[str] = None, container_path: Optional[str] = None):
        """
        初始化 MaaTools IPC 客户端
        
        Args:
            bundle_id: 应用的 Bundle ID，如 'com.hypergryph.arknights'
            container_path: 沙盒容器完整路径，如 '/Users/co/Library/Containers/com.hypergryph.arknights'
                          如果提供此参数，将忽略 bundle_id
        """
        self.bundle_id = bundle_id
        self.container_path = container_path
        self.unix_socket: Optional[socket.socket] = None
        self.unix_socket_path: Optional[str] = None
        
        # IPC 命令/事件环形缓冲区
        self.shm_fd: Optional[int] = None
        self.shm_name: Optional[str] = None
        self.mmap_obj: Optional[mmap.mmap] = None
        
        # 截图专用共享内存
        self.screencap_fd: Optional[int] = None
        self.screencap_shm_name: Optional[str] = None
        self.screencap_mmap_obj: Optional[mmap.mmap] = None
        self.screencap_width: int = 0
        self.screencap_height: int = 0
        
        self.cmd_sem: Optional[int] = None
        self.event_sem: Optional[int] = None
        self.cmd_sem_name: Optional[str] = None
        self.event_sem_name: Optional[str] = None
        
        self.seq_id = 0
        self.connected = False
        self.heartbeat_thread: Optional[threading.Thread] = None
        self.heartbeat_stop = threading.Event()
        
    def connect(self) -> bool:
        """连接到 MaaTools IPC"""
        print("🚀 初始化 MaaTools IPC (Unix Socket + Shm + Semaphore)...")
        
        # 1. 查找沙盒容器路径
        if not self._find_socket_path():
            return False
        
        # 2. 生成唯一的资源名称
        timestamp = int(time.time() * 1000)
        self.shm_name = f"/maa_ipc_{timestamp}"
        self.cmd_sem_name = f"/maa_cmd_{timestamp}"
        self.event_sem_name = f"/maa_evt_{timestamp}"
        
        print(f"📦 Shm name: {self.shm_name}")
        print(f"🔔 Cmd sem: {self.cmd_sem_name}")
        print(f"🔔 Event sem: {self.event_sem_name}")
        print(f"🔌 Unix socket: {self.unix_socket_path}")
        
        # 3. 创建 POSIX 共享内存
        if not self._create_shared_memory():
            return False
        
        # 4. 创建 Mach 信号量
        if not self._create_semaphores():
            self._cleanup()
            return False
        
        # 5. 通过 Unix Socket 进行连接和握手
        if not self._unix_socket_connect():
            self._cleanup()
            return False
        
        self.connected = True
        print("✅ MaaTools IPC 连接成功\n")
        return True
    
    def _create_shared_memory(self) -> bool:
        """创建 POSIX 共享内存"""
        # 先清理可能存在的旧共享内存
        libc.shm_unlink(self.shm_name.encode('utf-8'))
        
        # 创建共享内存（使用 0o666 权限）
        self.shm_fd = libc.shm_open(
            self.shm_name.encode('utf-8'),
            O_RDWR | O_CREAT | O_EXCL,
            0o666
        )
        
        if self.shm_fd < 0:
            errno = ctypes.get_errno()
            print(f"❌ 创建共享内存失败: errno={errno}")
            return False
        
        # 设置大小
        if libc.ftruncate(self.shm_fd, TOTAL_IPC_SIZE) != 0:
            errno = ctypes.get_errno()
            print(f"❌ 设置共享内存大小失败: errno={errno}")
            os.close(self.shm_fd)
            return False
        
        # 映射到进程地址空间
        try:
            self.mmap_obj = mmap.mmap(self.shm_fd, TOTAL_IPC_SIZE)
            print(f"✅ 共享内存创建成功: {TOTAL_IPC_SIZE} bytes ({TOTAL_IPC_SIZE//1024}KB)")
        except Exception as e:
            print(f"❌ mmap 失败: {e}")
            os.close(self.shm_fd)
            return False
        
        # 初始化同步头
        self.mmap_obj[0:4] = struct.pack('<I', 0)  # cmdWriteIndex
        self.mmap_obj[64:68] = struct.pack('<I', 0)  # cmdReadIndex
        self.mmap_obj[128:132] = struct.pack('<I', 0)  # eventWriteIndex
        self.mmap_obj[192:196] = struct.pack('<I', 0)  # eventReadIndex
        
        return True
    
    def _create_semaphores(self) -> bool:
        """等待 Swift 端创建信号量"""
        # 不在这里创建信号量，等待 Swift 端创建并返回名称
        print(f"⏳ 等待 Swift 端创建信号量...")
        return True
    
    def _open_semaphores(self) -> bool:
        """打开 Swift 端创建的信号量"""
        # 打开命令信号量（Swift 创建，Python 打开）
        # 注意：即使不创建，sem_open 仍需要 4 个参数（mode 和 value 会被忽略）
        self.cmd_sem = libc.sem_open(
            self.cmd_sem_name.encode('utf-8'),
            0,  # 不创建，只打开
            0o666,  # mode（打开时被忽略）
            0  # value（打开时被忽略）
        )
        
        if self.cmd_sem == SEM_FAILED:
            errno = ctypes.get_errno()
            print(f"❌ 打开命令信号量失败: errno={errno} ({self.cmd_sem_name})")
            return False
        
        # 打开事件信号量（Swift 创建，Python 打开）
        self.event_sem = libc.sem_open(
            self.event_sem_name.encode('utf-8'),
            0,  # 不创建，只打开
            0o666,  # mode（打开时被忽略）
            0  # value（打开时被忽略）
        )
        
        if self.event_sem == SEM_FAILED:
            errno = ctypes.get_errno()
            print(f"❌ 打开事件信号量失败: errno={errno} ({self.event_sem_name})")
            libc.sem_close(self.cmd_sem)
            self.cmd_sem = None
            return False
        
        return True
    
    def _find_socket_path(self) -> bool:
        """查找沙盒容器内的 Unix Socket 路径"""
        # 如果已提供完整路径，直接使用
        if self.container_path:
            socket_path = Path(self.container_path) / "Data" / "tmp" / "maa_ipc.sock"
            if socket_path.exists():
                self.unix_socket_path = str(socket_path)
                print(f"✅ 使用指定路径: {self.unix_socket_path}")
                return True
            else:
                print(f"❌ 指定路径不存在: {socket_path}")
                return False
        
        # 如果提供了 bundle_id，构造路径
        if self.bundle_id:
            containers_base = Path.home() / "Library" / "Containers"
            container_dir = containers_base / self.bundle_id
            socket_path = container_dir / "Data" / "tmp" / "maa_ipc.sock"
            
            if socket_path.exists():
                self.unix_socket_path = str(socket_path)
                print(f"✅ 找到 Socket: {self.unix_socket_path}")
                return True
            else:
                print(f"❌ Socket 不存在: {socket_path}")
                print(f"   请确保 PlayCover 应用正在运行")
                return False
        
        # 自动搜索常见游戏的容器
        print("🔍 未指定 bundle_id，开始自动搜索...")
        containers_base = Path.home() / "Library" / "Containers"
        
        # 常见游戏 bundle IDs
        common_bundles = [
            "com.hypergryph.arknights",
            "com.miHoYo.GenshinImpact",
            "com.miHoYo.Yuanshen",
            # 添加更多常见游戏...
        ]
        
        # 先尝试常见的
        for bundle in common_bundles:
            socket_path = containers_base / bundle / "Data" / "tmp" / "maa_ipc.sock"
            if socket_path.exists():
                self.unix_socket_path = str(socket_path)
                self.bundle_id = bundle
                print(f"✅ 自动找到 Socket: {self.unix_socket_path}")
                print(f"   Bundle ID: {bundle}")
                return True
        
        # 遍历所有容器查找
        print("   在所有容器中搜索...")
        if containers_base.exists():
            for container in containers_base.iterdir():
                if container.is_dir():
                    socket_path = container / "Data" / "tmp" / "maa_ipc.sock"
                    if socket_path.exists():
                        self.unix_socket_path = str(socket_path)
                        self.bundle_id = container.name
                        print(f"✅ 找到 Socket: {self.unix_socket_path}")
                        print(f"   Bundle ID: {container.name}")
                        return True
        
        print("❌ 未找到任何 MaaTools IPC Socket")
        print("   请确保：")
        print("   1. PlayCover 应用正在运行")
        print("   2. 游戏已启动（注入了 PlayTools）")
        print("   或者使用 bundle_id 或 container_path 参数明确指定")
        return False
    
    def _unix_socket_connect(self) -> bool:
        """通过 TCP 与 PlayCover 握手，发送配置信息"""
        print(f"🤝 连接 TCP 服务器 127.0.0.1:{self.tcp_port}...")
        
        try:
            # 创建 TCP socket 并连接
            self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_socket.settimeout(10.0)  # 连接超时
            self.tcp_socket.connect(("127.0.0.1", self.tcp_port))
            print(f"✅ TCP 连接已建立")
            
            # 发送配置信息（只包含 shm_name，信号量由 Swift 创建）
            config = {
                "shm_name": self.shm_name
            }
            config_json = json.dumps(config) + "\n"
            self.tcp_socket.sendall(config_json.encode('utf-8'))
            print(f"📤 已发送配置: {len(config_json)} bytes")
            
            # 接收响应
            self.tcp_socket.settimeout(5.0)
            response_data = b""
            while b"\n" not in response_data:
                chunk = self.tcp_socket.recv(1024)
                if not chunk:
                    print(f"❌ 连接已关闭")
                    self.tcp_socket.close()
                    self.tcp_socket = None
                    return False
                response_data += chunk
            
            response = response_data.decode('utf-8').strip()
            response_obj = json.loads(response)
            
            if response_obj.get("status") != "ok":
                print(f"❌ PlayCover 返回错误: {response_obj.get('message', 'unknown')}")
                self.tcp_socket.close()
                self.tcp_socket = None
                return False
            
            print(f"✅ PlayCover 握手成功")
            
            # 启动心跳线程（保持连接）
            self.tcp_socket.settimeout(None)  # 设置为阻塞模式
            self.heartbeat_stop.clear()
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.heartbeat_thread.start()
            print(f"💓 心跳线程已启动")
            
            return True
                
        except socket.timeout:
            print(f"❌ TCP 连接超时")
            print(f"   请确保 PlayCover 应用正在运行")
            return False
        except ConnectionRefusedError:
            print(f"❌ TCP 连接被拒绝")
            print(f"   请确保 PlayCover 应用正在运行且端口 {self.tcp_port} 可用")
            return False
        except Exception as e:
            print(f"❌ TCP 握手失败: {e}")
            return False
    
    def _unix_socket_connect(self) -> bool:
        """通过 Unix Socket 与 PlayCover 连接、握手和传递文件描述符"""
        print(f"🤝 连接 Unix Socket: {self.unix_socket_path}...")
        
        try:
            # 创建 Unix Socket 并连接
            self.unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.unix_socket.settimeout(10.0)
            self.unix_socket.connect(self.unix_socket_path)
            print(f"✅ Unix Socket 连接已建立")
            
            # 1. 发送配置信息（握手）
            config = {
                "shm_name": self.shm_name,
                "cmd_sem_name": self.cmd_sem_name,
                "event_sem_name": self.event_sem_name
            }
            config_json = json.dumps(config) + "\n"
            self.unix_socket.sendall(config_json.encode('utf-8'))
            print(f"📤 已发送配置: {len(config_json)} bytes")
            
            # 2. 接收确认
            self.unix_socket.settimeout(5.0)
            response_data = b""
            while b"\n" not in response_data:
                chunk = self.unix_socket.recv(1024)
                if not chunk:
                    print(f"❌ 连接已关闭")
                    self.unix_socket.close()
                    self.unix_socket = None
                    return False
                response_data += chunk
            
            response = response_data.decode('utf-8').strip()
            response_obj = json.loads(response)
            
            if response_obj.get("status") != "ok":
                print(f"❌ PlayCover 返回错误: {response_obj.get('message', 'unknown')}")
                self.unix_socket.close()
                self.unix_socket = None
                return False
            
            # 获取 Swift 端创建的信号量名称和截图尺寸信息
            self.cmd_sem_name = response_obj.get("cmd_sem_name")
            self.event_sem_name = response_obj.get("event_sem_name")
            screencap_size = response_obj.get("screencap_size")
            self.screencap_width = response_obj.get("width", 0)
            self.screencap_height = response_obj.get("height", 0)
            
            if not self.cmd_sem_name or not self.event_sem_name or screencap_size is None:
                print(f"❌ 响应中缺少必要信息")
                self.unix_socket.close()
                self.unix_socket = None
                return False
            
            print(f"✅ PlayCover 握手成功")
            print(f"   🔔 Cmd sem: {self.cmd_sem_name}")
            print(f"   🔔 Event sem: {self.event_sem_name}")
            print(f"   📸 Screencap size: {screencap_size} bytes ({self.screencap_width}x{self.screencap_height})")
            
            # 3. 创建截图共享内存
            print(f"📸 创建截图共享内存...")
            self.screencap_shm_name = f"/maa_screencap_{int(time.time() * 1000)}"
            self.screencap_fd = libc.shm_open(
                self.screencap_shm_name.encode('utf-8'),
                os.O_CREAT | os.O_RDWR,
                0o666
            )
            
            if self.screencap_fd < 0:
                print(f"❌ 创建截图共享内存失败: errno={ctypes.get_errno()}")
                self.unix_socket.close()
                self.unix_socket = None
                return False
            
            os.ftruncate(self.screencap_fd, screencap_size)
            
            # 映射截图共享内存（只读，避免意外修改）
            self.screencap_mmap_obj = mmap.mmap(
                self.screencap_fd,
                screencap_size,
                mmap.MAP_SHARED,
                mmap.PROT_READ
            )
            print(f"✅ 截图共享内存已创建: {self.screencap_shm_name}")
            
            # 4. 发送两个文件描述符（IPC 缓冲区 + 截图数据）
            print(f"🔌 发送共享内存文件描述符...")
            fds = array.array('i', [self.shm_fd, self.screencap_fd])
            msg = b"FD_TRANSFER"
            
            self.unix_socket.sendmsg(
                [msg],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)]
            )
            
            print(f"✅ 已发送 shm_fd={self.shm_fd}, screencap_fd={self.screencap_fd}")
            
            # 5. 接收 FD 确认
            ack = self.unix_socket.recv(16)
            if ack != b"FD_ACK":
                print(f"❌ 未收到 FD_ACK 确认: {ack}")
                self.unix_socket.close()
                self.unix_socket = None
                return False
            
            print(f"✅ 收到 FD_ACK 确认")
            
            # 6. 打开 Swift 端创建的信号量
            print(f"🔓 打开信号量...")
            if not self._open_semaphores():
                print(f"❌ 打开信号量失败")
                self.unix_socket.close()
                self.unix_socket = None
                return False
            print(f"✅ 信号量打开成功")
            
            # 6. 启动心跳线程（保持连接）
            self.unix_socket.settimeout(None)  # 设置为阻塞模式
            self.heartbeat_stop.clear()
            self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.heartbeat_thread.start()
            print(f"💓 心跳线程已启动")
            
            return True
                
        except socket.timeout:
            print(f"❌ Unix Socket 连接超时")
            print(f"   请确保 PlayCover 应用正在运行")
            return False
        except ConnectionRefusedError:
            print(f"❌ Unix Socket 连接被拒绝")
            print(f"   请确保 PlayCover 应用正在运行")
            return False
        except FileNotFoundError:
            print(f"❌ Unix Socket 文件不存在: {self.unix_socket_path}")
            print(f"   请确保 PlayCover 应用正在运行")
            return False
        except Exception as e:
            print(f"❌ Unix Socket 连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _heartbeat_loop(self):
        """心跳线程：定期发送心跳包，检测连接断开"""
        print("💓 心跳循环启动")
        
        try:
            while not self.heartbeat_stop.is_set():
                # 每 5 秒发送一次心跳
                time.sleep(5.0)
                
                if self.heartbeat_stop.is_set():
                    break
                
                try:
                    # 发送心跳字节
                    self.unix_socket.sendall(b'\x01')
                    print("💓 心跳发送")
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    print(f"\n❌ 连接已断开: {e}")
                    print("❌ PlayCover 可能已退出或崩溃")
                    self.connected = False
                    break
        except Exception as e:
            print(f"\n❌ 心跳线程异常: {e}")
            self.connected = False
        
        print("💓 心跳循环结束")
    
    def _cleanup(self):
        """清理资源（Python 负责创建和删除）"""
        # 停止心跳线程
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_stop.set()
            self.heartbeat_thread.join(timeout=1.0)
        
        # 关闭 Unix socket
        if self.unix_socket:
            try:
                self.unix_socket.close()
            except:
                pass
            self.unix_socket = None
        
        # 关闭信号量（不删除，由 Swift 端负责删除）
        if self.cmd_sem and self.cmd_sem != SEM_FAILED:
            libc.sem_close(self.cmd_sem)
            self.cmd_sem = None
        
        if self.event_sem and self.event_sem != SEM_FAILED:
            libc.sem_close(self.event_sem)
            self.event_sem = None
        
        # 关闭并删除共享内存
        if self.mmap_obj:
            self.mmap_obj.close()
            self.mmap_obj = None
        
        if self.shm_fd and self.shm_fd >= 0:
            os.close(self.shm_fd)
            if self.shm_name:
                libc.shm_unlink(self.shm_name.encode('utf-8'))
            self.shm_fd = -1
        
        # 关闭并删除截图共享内存
        if self.screencap_mmap_obj:
            self.screencap_mmap_obj.close()
            self.screencap_mmap_obj = None
        
        if self.screencap_fd and self.screencap_fd >= 0:
            os.close(self.screencap_fd)
            if self.screencap_shm_name:
                libc.shm_unlink(self.screencap_shm_name.encode('utf-8'))
            self.screencap_fd = -1
    
    def disconnect(self):
        """断开连接"""
        if not self.connected:
            return
        
        print("🛑 断开连接...")
        self._cleanup()
        self.connected = False
        print("✅ 已断开")
    
    def _send_command(self, cmd_type: int, x: int = 0, y: int = 0,
                     x2: int = 0, y2: int = 0, duration: int = 0) -> int:
        """发送命令到 PlayTools"""
        self.seq_id += 1
        
        # 获取写索引
        write_idx = struct.unpack('<I', self.mmap_obj[0:4])[0]
        index = write_idx % RING_SIZE
        
        # 写入命令包（注意：Swift 结构体有对齐，type 后有 3 字节填充）
        offset = HEADER_SIZE + index * CMD_PACKET_SIZE
        packet = struct.pack('<B3xIiiiiI4B',
                           cmd_type, self.seq_id, x, y, x2, y2, duration,
                           0, 0, 0, 0)  # reserved
        self.mmap_obj[offset:offset+CMD_PACKET_SIZE] = packet
        
        # 更新写索引
        new_write_idx = (write_idx + 1) % (RING_SIZE * 2)
        self.mmap_obj[0:4] = struct.pack('<I', new_write_idx)
        
        # ⚡ 发送信号量通知（零延迟）
        if libc.sem_post(self.cmd_sem) != 0:
            errno = ctypes.get_errno()
            print(f"⚠️ sem_post 失败: errno={errno}")
        
        return self.seq_id
    
    def _wait_event(self, timeout: float = 5.0) -> Optional[Tuple[int, int, int]]:
        """等待事件响应（type, reqSeqId, errorCode）"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # ⚡ 使用信号量等待（阻塞，零延迟）
            # 使用 trywait 配合短超时，避免永久阻塞
            if libc.sem_trywait(self.event_sem) == 0:
                # 有信号，读取事件
                read_idx = struct.unpack('<I', self.mmap_obj[192:196])[0]
                write_idx = struct.unpack('<I', self.mmap_obj[128:132])[0]
                
                if read_idx != write_idx:
                    index = read_idx % RING_SIZE
                    offset = HEADER_SIZE + CMD_RING_SIZE + index * EVENT_PACKET_SIZE
                    
                    event_data = self.mmap_obj[offset:offset+EVENT_PACKET_SIZE]
                    # 注意：Swift 结构体有对齐，type 后有 3 字节填充
                    evt_type, req_seq_id, error_code = struct.unpack('<B3xIi', event_data[:12])
                    
                    # 更新读索引
                    new_read_idx = (read_idx + 1) % (RING_SIZE * 2)
                    self.mmap_obj[192:196] = struct.pack('<I', new_read_idx)
                    
                    return (evt_type, req_seq_id, error_code)
            
            # 短暂休眠，避免 CPU 占用过高
            time.sleep(0.0001)  # 0.1ms
        
        return None
    
    def tap(self, x: int, y: int, duration: int = 50) -> bool:
        """点击"""
        if not self.connected:
            print("❌ 未连接或连接已断开")
            return False
        
        start = time.perf_counter()
        seq_id = self._send_command(CMD_TAP, x, y, duration=duration)
        
        event = self._wait_event()
        latency = (time.perf_counter() - start) * 1000
        
        if event and event[0] == EVENT_ACK and event[1] == seq_id:
            print(f"✅ 点击成功 ({x}, {y}): 延迟 {latency:.1f}ms")
            return True
        else:
            print(f"❌ 点击失败: {event}")
            return False
    
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> bool:
        """滑动"""
        if not self.connected:
            print("❌ 未连接或连接已断开")
            return False
        
        start = time.perf_counter()
        seq_id = self._send_command(CMD_SWIPE, x1, y1, x2, y2, duration)
        
        event = self._wait_event(timeout=10.0)  # 滑动可能需要更长时间
        latency = (time.perf_counter() - start) * 1000
        
        if event and event[0] == EVENT_ACK and event[1] == seq_id:
            print(f"✅ 滑动成功: 延迟 {latency:.1f}ms")
            return True
        else:
            print(f"❌ 滑动失败: {event}")
            return False
    
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> bool:
        """拖拽（长按后移动）"""
        if not self.connected:
            print("❌ 未连接或连接已断开")
            return False
        
        start = time.perf_counter()
        seq_id = self._send_command(CMD_DRAG, x1, y1, x2, y2, duration)
        
        event = self._wait_event(timeout=10.0)
        latency = (time.perf_counter() - start) * 1000
        
        if event and event[0] == EVENT_ACK and event[1] == seq_id:
            print(f"✅ 拖拽成功: 延迟 {latency:.1f}ms")
            return True
        else:
            print(f"❌ 拖拽失败: {event}")
            return False
    
    def get_screen_size(self) -> Optional[Tuple[int, int]]:
        """获取屏幕尺寸"""
        if not self.connected:
            print("❌ 未连接或连接已断开")
            return None
        
        start = time.perf_counter()
        seq_id = self._send_command(CMD_GET_SIZE)
        
        event = self._wait_event(timeout=5.0)
        latency = (time.perf_counter() - start) * 1000
        
        if event and event[0] == EVENT_SIZE_INFO and event[1] == seq_id:
            # 解码：低16位=宽度, 高16位=高度
            encoded = event[2]
            width = encoded & 0xFFFF
            height = (encoded >> 16) & 0xFFFF
            print(f"✅ 屏幕尺寸: {width}x{height}, 延迟 {latency:.1f}ms")
            return (width, height)
        else:
            print(f"❌ 获取屏幕尺寸失败: {event}")
            return None
    
    def get_version(self) -> Optional[int]:
        """获取协议版本"""
        if not self.connected:
            print("❌ 未连接或连接已断开")
            return None
        
        start = time.perf_counter()
        seq_id = self._send_command(CMD_GET_VERSION)
        
        event = self._wait_event(timeout=5.0)
        latency = (time.perf_counter() - start) * 1000
        
        if event and event[0] == EVENT_VERSION_INFO and event[1] == seq_id:
            version = event[2]
            print(f"✅ 协议版本: {version}, 延迟 {latency:.1f}ms")
            return version
        else:
            print(f"❌ 获取协议版本失败: {event}")
            return None
    
    def screenshot(self) -> Optional[bytes]:
        """截图（从专用共享内存读取）
        
        Returns:
            RGBA 格式的图像数据，失败返回 None
        """
        if not self.connected:
            print("❌ 未连接或连接已断开")
            return None
        
        if not self.screencap_mmap_obj:
            print("❌ 截图共享内存未初始化")
            return None
        
        start = time.perf_counter()
        
        # 发送截图命令
        seq_id = self._send_command(CMD_SCREENSHOT)
        
        # 等待截图完成事件
        event = self._wait_event(timeout=5.0)
        latency = (time.perf_counter() - start) * 1000
        
        if event and event[0] == EVENT_SCREENSHOT_READY and event[1] == seq_id:
            # 从共享内存读取截图数据（零拷贝）
            self.screencap_mmap_obj.seek(0)
            screenshot_data = self.screencap_mmap_obj.read()
            print(f"✅ 截图成功: {len(screenshot_data)} 字节, 延迟 {latency:.1f}ms")
            return screenshot_data
        else:
            print(f"❌ 截图失败: {event}")
            return None


# ============================================================
# 测试代码
# ============================================================

def main():
    print("🚀 MaaTools IPC 快速测试 (TCP 长连接 + Semaphore 版本)")
    print("=" * 60)
    
    client = MaaToolsIPC()
    
    try:
        # 连接
        print("\n📡 正在连接...")
        if not client.connect():
            print("❌ 连接失败")
            return 1
        
        # 自动测试
        print("\n🤖 自动测试模式")
        print("=" * 60)
        
        # 测试循环 - 持续运行直到断开
        test_count = 0
        while client.connected:
            test_count += 1
            print(f"\n🔄 第 {test_count} 轮测试")
            
            print("\n1️⃣  测试点击...")
            if not client.tap(500, 500):
                break
            time.sleep(0.5)
            
            if not client.connected:
                break
                
            if not client.tap(100, 100):
                break
            
            print("\n2️⃣  测试滑动...")
            time.sleep(0.5)
            
            if not client.connected:
                break
                
            if not client.swipe(200, 500, 800, 500, 300):
                break
            time.sleep(0.5)
            
            if not client.connected:
                break
                
            if not client.swipe(500, 700, 500, 200, 400):
                break
            
            print(f"\n✅ 第 {test_count} 轮测试完成")
            print("⏳ 等待 3 秒...")
            time.sleep(3)
        
        if not client.connected:
            print("\n" + "=" * 60)
            print("⚠️ 连接已断开")
            print("=" * 60)
        else:
            print("\n" + "=" * 60)
            print("✅ 测试完成")
            print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户中断")
    finally:
        client.disconnect()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
