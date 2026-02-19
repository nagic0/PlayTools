#!/usr/bin/env python3
"""
测试 MaaTools TCP 协议功能

功能测试：
1. 版本查询 (VERSION)
2. 屏幕尺寸查询 (SIZE)
3. 截图 (SCREENCAP)
4. 触摸操作 (TOUCH)

性能测试：
- 各操作耗时统计
- 截图帧率测试
- 批量触摸延迟测试

使用方法：
    # 默认端口 1717
    python test_maa_tools_tcp.py
    
    # 指定端口
    python test_maa_tools_tcp.py --port 1717
    
    # 指定主机
    python test_maa_tools_tcp.py --host localhost --port 1717
"""

import socket
import struct
import time
import sys
import argparse
from pathlib import Path
from typing import Optional, Tuple


class MaaToolsTCPClient:
    """MaaTools TCP 客户端"""
    
    # 协议魔术字
    MAGIC_CONNECTION = b'MAA\x00'  # 握手
    MAGIC_SCREENCAP = b'SCRN'      # 截图
    MAGIC_SIZE = b'SIZE'            # 屏幕尺寸
    MAGIC_TERMINATE = b'TERM'       # 终止应用
    MAGIC_TOUCH = b'TUCH'           # 触摸
    MAGIC_VERSION = b'VERN'         # 版本
    
    # 触摸阶段
    TOUCH_DOWN = 0
    TOUCH_MOVE = 1
    TOUCH_UP = 3
    
    def __init__(self, host: str = 'localhost', port: int = 1717):
        """初始化客户端
        
        Args:
            host: 服务器地址
            port: 服务器端口
        """
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.connected = False
        
    def connect(self) -> bool:
        """连接到 MaaTools 服务器
        
        Returns:
            是否连接成功
        """
        try:
            print(f"🔌 正在连接 {self.host}:{self.port}...")
            
            # 创建 TCP socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)  # 5秒超时
            
            # 连接
            self.sock.connect((self.host, self.port))
            
            # 发送握手魔术字
            self.sock.sendall(self.MAGIC_CONNECTION)
            
            # 接收响应（应该是 "OKAY"）
            response = self.sock.recv(4)
            if response != b'OKAY':
                print(f"❌ 握手失败，收到: {response}")
                return False
            
            self.connected = True
            print(f"✅ 连接成功，握手完成")
            return True
            
        except socket.timeout:
            print("❌ 连接超时")
            return False
        except ConnectionRefusedError:
            print("❌ 连接被拒绝，请确保：")
            print("   1. PlayCover 应用正在运行")
            print("   2. 游戏已启动")
            print("   3. MaaTools 功能已启用")
            print(f"   4. 端口 {self.port} 正确")
            return False
        except Exception as e:
            print(f"❌ 连接失败: {e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        self.connected = False
        print("🔌 已断开连接")
    
    def _send_command(self, magic: bytes, data: bytes = b'') -> None:
        """发送命令
        
        Args:
            magic: 命令魔术字（4字节）
            data: 附加数据
        """
        if not self.connected or not self.sock:
            raise RuntimeError("未连接到服务器")
        
        # 构造 payload
        payload = magic + data
        
        # 发送：2字节长度 + payload
        length = len(payload)
        header = struct.pack('>H', length)  # 大端序 uint16
        
        self.sock.sendall(header + payload)
    
    def get_version(self) -> Optional[int]:
        """获取 MaaTools 版本
        
        Returns:
            版本号，失败返回 None
        """
        try:
            self._send_command(self.MAGIC_VERSION)
            
            # 接收 4 字节版本号
            data = self.sock.recv(4)
            if len(data) != 4:
                return None
            
            version = struct.unpack('>I', data)[0]  # 大端序 uint32
            return version
            
        except Exception as e:
            print(f"❌ 获取版本失败: {e}")
            return None
    
    def get_screen_size(self) -> Optional[Tuple[int, int]]:
        """获取屏幕尺寸
        
        Returns:
            (width, height) 元组，失败返回 None
        """
        try:
            self._send_command(self.MAGIC_SIZE)
            
            # 接收 4 字节尺寸信息
            data = self.sock.recv(4)
            if len(data) != 4:
                return None
            
            width = struct.unpack('>H', data[0:2])[0]
            height = struct.unpack('>H', data[2:4])[0]
            return (width, height)
            
        except Exception as e:
            print(f"❌ 获取屏幕尺寸失败: {e}")
            return None
    
    def capture_screen(self) -> Optional[bytes]:
        """截取屏幕
        
        Returns:
            RGBA 原始图像数据，失败返回 None
        """
        try:
            self._send_command(self.MAGIC_SCREENCAP)
            
            # 接收 4 字节长度
            length_data = self.sock.recv(4)
            if len(length_data) != 4:
                return None
            
            image_length = struct.unpack('>I', length_data)[0]
            
            # 接收图像数据（用列表收集 chunks，避免 O(n²) 的 bytes 拼接）
            chunks = []
            received = 0
            while received < image_length:
                chunk = self.sock.recv(min(image_length - received, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            data = b''.join(chunks)
            
            if len(data) != image_length:
                print(f"⚠️  图像数据不完整: {len(data)}/{image_length}")
                return None
            
            return data
            
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None
    
    def touch(self, x: int, y: int, phase: int) -> bool:
        """发送触摸事件
        
        Args:
            x: X 坐标
            y: Y 坐标
            phase: 触摸阶段（TOUCH_DOWN/TOUCH_MOVE/TOUCH_UP）
        
        Returns:
            是否发送成功
        """
        try:
            # 构造触摸数据：1字节phase + 2字节x + 2字节y
            touch_data = struct.pack('>BHH', phase, x, y)
            self._send_command(self.MAGIC_TOUCH, touch_data)
            return True
            
        except Exception as e:
            print(f"❌ 发送触摸事件失败: {e}")
            return False
    
    def tap(self, x: int, y: int, duration: float = 0.05) -> bool:
        """点击
        
        Args:
            x: X 坐标
            y: Y 坐标
            duration: 按下持续时间（秒）
        
        Returns:
            是否成功
        """
        if not self.touch(x, y, self.TOUCH_DOWN):
            return False
        time.sleep(duration)
        if not self.touch(x, y, self.TOUCH_UP):
            return False
        return True
    
    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> bool:
        """滑动
        
        Args:
            x1: 起始 X 坐标
            y1: 起始 Y 坐标
            x2: 结束 X 坐标
            y2: 结束 Y 坐标
            duration: 滑动持续时间（秒）
        
        Returns:
            是否成功
        """
        # 按下
        if not self.touch(x1, y1, self.TOUCH_DOWN):
            return False
        
        # 移动（分成多个步骤）
        steps = max(10, int(duration * 60))  # 至少10步，或按60fps计算
        for i in range(1, steps):
            t = i / steps
            x = int(x1 + (x2 - x1) * t)
            y = int(y1 + (y2 - y1) * t)
            if not self.touch(x, y, self.TOUCH_MOVE):
                return False
            time.sleep(duration / steps)
        
        # 抬起
        if not self.touch(x2, y2, self.TOUCH_UP):
            return False
        
        return True
    
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> bool:
        """拖拽（与滑动类似，但语义上用于拖动元素）
        
        Args:
            x1: 起始 X 坐标
            y1: 起始 Y 坐标
            x2: 结束 X 坐标
            y2: 结束 Y 坐标
            duration: 拖拽持续时间（秒）
        
        Returns:
            是否成功
        """
        return self.swipe(x1, y1, x2, y2, duration)


def save_screenshot(data: bytes, width: int, height: int, filename: str = "screenshot.png") -> bool:
    """保存截图为 PNG 文件
    
    Args:
        data: RGBA 原始数据
        width: 图像宽度
        height: 图像高度
        filename: 保存的文件名
    
    Returns:
        是否保存成功
    """
    try:
        from PIL import Image
        
        # 创建图像（RGBA 模式）
        img = Image.frombytes('RGBA', (width, height), data)
        
        # 转换为 RGB（移除 alpha 通道）
        img = img.convert('RGB')
        
        # 保存
        img.save(filename, 'PNG')
        print(f"💾 截图已保存: {filename} ({width}x{height})")
        
        return True
        
    except ImportError:
        print("⚠️  未安装 Pillow 库，无法保存图片")
        print("   安装命令: pip install Pillow")
        return False
    except Exception as e:
        print(f"❌ 保存截图失败: {e}")
        return False


def test_basic_features(client: MaaToolsTCPClient):
    """测试基本功能"""
    print("\n" + "="*60)
    print("📋 基本功能测试")
    print("="*60)
    
    # 测试1：版本查询
    print("\n🎯 测试 1: 查询版本")
    start = time.time()
    version = client.get_version()
    elapsed = (time.time() - start) * 1000
    if version is not None:
        print(f"   ✓ 版本: {version}")
        print(f"   ⏱  耗时: {elapsed:.2f}ms")
    else:
        print(f"   ✗ 失败")
    
    # 测试2：屏幕尺寸
    print("\n🎯 测试 2: 查询屏幕尺寸")
    start = time.time()
    size = client.get_screen_size()
    elapsed = (time.time() - start) * 1000
    if size:
        width, height = size
        print(f"   ✓ 尺寸: {width}x{height}")
        print(f"   ⏱  耗时: {elapsed:.2f}ms")
    else:
        print(f"   ✗ 失败")
        return None
    
    # 测试3：截图
    print("\n🎯 测试 3: 截图测试")
    start = time.time()
    data = client.capture_screen()
    elapsed = (time.time() - start) * 1000
    if data:
        expected_size = width * height * 4  # RGBA
        print(f"   ✓ 截图成功")
        print(f"   📦 数据大小: {len(data)} 字节 (期望: {expected_size})")
        print(f"   ⏱  耗时: {elapsed:.2f}ms")
        
        # 保存截图
        save_screenshot(data, width, height, "maa_screenshot_test.png")
    else:
        print(f"   ✗ 失败")
    
    return size


def test_touch_operations(client: MaaToolsTCPClient, width: int, height: int):
    """测试触摸操作"""
    print("\n" + "="*60)
    print("👆 触摸操作测试")
    print("="*60)
    
    # 测试4：点击
    print("\n🎯 测试 4: 点击测试")
    x, y = width // 2, height // 2
    print(f"   位置: ({x}, {y})")
    
    start = time.time()
    success = client.tap(x, y)
    elapsed = (time.time() - start) * 1000
    
    if success:
        print(f"   ✓ 点击成功")
        print(f"   ⏱  耗时: {elapsed:.2f}ms")
    else:
        print(f"   ✗ 失败")
    
    time.sleep(0.3)
    
    # 测试5：滑动
    print("\n🎯 测试 5: 滑动测试")
    x1, y1 = width // 4, height // 2
    x2, y2 = width * 3 // 4, height // 2
    duration = 0.5
    print(f"   从 ({x1}, {y1}) 到 ({x2}, {y2})")
    print(f"   持续时间: {duration*1000:.0f}ms")
    
    start = time.time()
    success = client.swipe(x1, y1, x2, y2, duration)
    elapsed = (time.time() - start) * 1000
    
    if success:
        print(f"   ✓ 滑动成功")
        print(f"   ⏱  总耗时: {elapsed:.2f}ms")
    else:
        print(f"   ✗ 滑动失败")
    
    time.sleep(0.3)
    
    # 测试6：拖拽
    print("\n🎯 测试 6: 拖拽测试")
    x1, y1 = width // 3, height // 3
    x2, y2 = width * 2 // 3, height * 2 // 3
    duration = 0.5
    print(f"   从 ({x1}, {y1}) 到 ({x2}, {y2})")
    print(f"   持续时间: {duration*1000:.0f}ms")
    
    start = time.time()
    success = client.drag(x1, y1, x2, y2, duration)
    elapsed = (time.time() - start) * 1000
    
    if success:
        print(f"   ✓ 拖拽成功")
        print(f"   ⏱  总耗时: {elapsed:.2f}ms")
    else:
        print(f"   ✗ 拖拽失败")


def test_performance(client: MaaToolsTCPClient, width: int, height: int) -> dict:
    """性能测试
    
    Returns:
        性能测试结果字典
    """
    print("\n" + "="*60)
    print("⚡ 性能测试")
    print("="*60)
    
    results = {}
    
    # 测试7：连续截图（帧率测试）
    print("\n🎯 测试 7: 截图帧率测试（连续10次）")
    times = []
    success_count = 0
    
    for i in range(10):
        start = time.time()
        data = client.capture_screen()
        elapsed = (time.time() - start) * 1000
        
        if data:
            times.append(elapsed)
            success_count += 1
            print(f"   第 {i+1} 次: {elapsed:.2f}ms")
        else:
            print(f"   第 {i+1} 次: 失败")
    
    if times:
        avg_time = sum(times) / len(times)
        max_time = max(times)
        min_time = min(times)
        fps = 1000 / avg_time if avg_time > 0 else 0
        
        print(f"\n   📊 统计:")
        print(f"   成功: {success_count}/10")
        print(f"   平均耗时: {avg_time:.2f}ms")
        print(f"   最快: {min_time:.2f}ms")
        print(f"   最慢: {max_time:.2f}ms")
        print(f"   理论帧率: {fps:.1f} FPS")
        
        results['screenshot'] = {
            'success': success_count,
            'total': 10,
            'avg_time': avg_time,
            'min_time': min_time,
            'max_time': max_time,
            'fps': fps
        }
    
    # 测试8：批量点击（延迟测试）
    print("\n🎯 测试 8: 批量点击延迟测试（100次）")
    times = []
    success_count = 0
    
    x_base, y_base = width // 2, height // 2
    
    total_start = time.time()
    for i in range(100):
        # 在中心点附近随机点击
        x = x_base + (i % 10 - 5) * 10
        y = y_base + (i // 10 - 5) * 10
        
        start = time.time()
        success = client.tap(x, y, duration=0.01)  # 快速点击
        elapsed = (time.time() - start) * 1000
        
        if success:
            times.append(elapsed)
            success_count += 1
    
    total_elapsed = (time.time() - total_start) * 1000
    
    if times:
        avg_time = sum(times) / len(times)
        max_time = max(times)
        min_time = min(times)
        
        print(f"\n   📊 统计:")
        print(f"   成功: {success_count}/100")
        print(f"   总耗时: {total_elapsed:.2f}ms")
        print(f"   平均延迟: {avg_time:.2f}ms")
        print(f"   最快: {min_time:.2f}ms")
        print(f"   最慢: {max_time:.2f}ms")
        print(f"   吞吐率: {success_count / (total_elapsed / 1000):.1f} 次/秒")
        
        results['tap'] = {
            'success': success_count,
            'total': 100,
            'total_time': total_elapsed,
            'avg_time': avg_time,
            'min_time': min_time,
            'max_time': max_time,
            'throughput': success_count / (total_elapsed / 1000)
        }
    
    # 测试9：连续滑动
    print("\n🎯 测试 9: 连续滑动测试（20次）")
    times = []
    success_count = 0
    
    total_start = time.time()
    for i in range(20):
        # 水平滑动
        if i % 2 == 0:
            x1, y1 = width // 4, height // 2
            x2, y2 = width * 3 // 4, height // 2
        else:
            x1, y1 = width * 3 // 4, height // 2
            x2, y2 = width // 4, height // 2
        
        start = time.time()
        success = client.swipe(x1, y1, x2, y2, 0.3)  # 300ms 快速滑动
        elapsed = (time.time() - start) * 1000
        
        if success:
            times.append(elapsed)
            success_count += 1
        
        time.sleep(0.05)
    
    total_elapsed = (time.time() - total_start) * 1000
    
    if times:
        avg_time = sum(times) / len(times)
        max_time = max(times)
        min_time = min(times)
        
        print(f"\n   📊 统计:")
        print(f"   成功: {success_count}/20")
        print(f"   总耗时: {total_elapsed:.2f}ms")
        print(f"   平均延迟: {avg_time:.2f}ms")
        print(f"   最快: {min_time:.2f}ms")
        print(f"   最慢: {max_time:.2f}ms")
        
        results['swipe'] = {
            'success': success_count,
            'total': 20,
            'total_time': total_elapsed,
            'avg_time': avg_time,
            'min_time': min_time,
            'max_time': max_time
        }
    
    return results


def main():
    parser = argparse.ArgumentParser(description='测试 MaaTools TCP 协议')
    parser.add_argument('--host', default='localhost', help='服务器地址 (默认: localhost)')
    parser.add_argument('--port', type=int, default=1717, help='服务器端口 (默认: 1717)')
    args = parser.parse_args()
    
    print("="*60)
    print("🧪 MaaTools TCP 协议测试")
    print("="*60)
    print()
    print("协议特点:")
    print("  - TCP Socket 通信")
    print("  - 图像数据传输")
    print("  - 触摸事件模拟")
    print()
    print("测试内容:")
    print("  1. 协议版本查询")
    print("  2. 屏幕尺寸查询")
    print("  3. 截图功能")
    print("  4. 触摸点击")
    print("  5. 触摸滑动")
    print("  6. 触摸拖拽")
    print("  7. 截图帧率测试（10次连续截图）")
    print("  8. 批量点击延迟测试（100次点击）")
    print("  9. 连续滑动测试（20次滑动）")
    print()
    print("请确保:")
    print("  - PlayCover 应用正在运行")
    print("  - 游戏已启动")
    print("  - MaaTools 功能已启用（在 PlaySettings 中）")
    print(f"  - 监听端口: {args.port}")
    print()
    
    input("准备好后按 Enter 继续...")
    print()
    
    # 创建客户端
    client = MaaToolsTCPClient(host=args.host, port=args.port)
    
    try:
        # 连接
        if not client.connect():
            return 1
        
        print()
        
        # 基本功能测试
        size = test_basic_features(client)
        if not size:
            print("\n❌ 无法获取屏幕尺寸，跳过触摸和性能测试")
            return 1
        
        width, height = size
        
        # 触摸操作测试
        test_touch_operations(client, width, height)
        
        # 性能测试
        print("\n" + "="*60)
        print("⚠️  即将开始性能测试")
        print("   这将执行大量操作（130次），可能需要 1-2 分钟")
        print("="*60)
        input("\n按 Enter 继续性能测试，或 Ctrl+C 跳过...")
        
        perf_results = test_performance(client, width, height)
        
        # 完成 - 统一输出性能测试结果
        print("\n" + "="*60)
        print("✅ 所有测试完成！")
        print("="*60)
        
        # 性能测试结果汇总
        if perf_results:
            print("\n" + "="*60)
            print("📊 性能测试结果汇总 (TCP 协议)")
            print("="*60)
            
            if 'screenshot' in perf_results:
                s = perf_results['screenshot']
                print(f"\n📸 截图性能:")
                print(f"   成功率: {s['success']}/{s['total']} ({s['success']/s['total']*100:.1f}%)")
                print(f"   平均耗时: {s['avg_time']:.2f}ms")
                print(f"   最快: {s['min_time']:.2f}ms")
                print(f"   最慢: {s['max_time']:.2f}ms")
                print(f"   理论帧率: {s['fps']:.1f} FPS")
            
            if 'tap' in perf_results:
                t = perf_results['tap']
                print(f"\n👆 点击性能:")
                print(f"   成功率: {t['success']}/{t['total']} ({t['success']/t['total']*100:.1f}%)")
                print(f"   总耗时: {t['total_time']:.2f}ms")
                print(f"   平均延迟: {t['avg_time']:.2f}ms")
                print(f"   最快: {t['min_time']:.2f}ms")
                print(f"   最慢: {t['max_time']:.2f}ms")
                print(f"   吞吐率: {t['throughput']:.1f} 次/秒")
            
            if 'swipe' in perf_results:
                sw = perf_results['swipe']
                print(f"\n↔️  滑动性能:")
                print(f"   成功率: {sw['success']}/{sw['total']} ({sw['success']/sw['total']*100:.1f}%)")
                print(f"   总耗时: {sw['total_time']:.2f}ms")
                print(f"   平均延迟: {sw['avg_time']:.2f}ms")
                print(f"   最快: {sw['min_time']:.2f}ms")
                print(f"   最慢: {sw['max_time']:.2f}ms")
            
            print("\n" + "="*60)
        
        return 0
        
    except KeyboardInterrupt:
        print("\n\n⚠️  测试被中断")
        return 1
    except Exception as e:
        print(f"\n\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
