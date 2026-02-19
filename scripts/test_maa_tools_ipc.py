#!/usr/bin/env python3
"""
test_maa_tools_ipc.py — MaaTools IPC 协议测试脚本

对应 Swift 侧重构后的架构：
  Utils/
  ├── SandboxIPC/
  │   ├── IPCSharedTypes.swift   共享数据结构 + 协议常量
  │   ├── ShmRingBuffer.swift    泛型共享内存环形缓冲区（Cache Line 对齐 + 内存屏障）
  │   └── SandboxIPCServer.swift Unix Socket 服务器（纯传输层，无业务逻辑）
  └── MaaToolsIPC.swift          业务逻辑（截图 / 触控，实现 SandboxIPCServerDelegate）

协议特点（与 maa_ipc_semaphore.py 客户端配套）：
  · Unix Socket 握手 + SCM_RIGHTS 文件描述符传递
  · POSIX 共享内存（环形缓冲区 + 截图专用区）
  · POSIX 信号量（Mach 内核，零唤醒延迟）
  · Cache Line 对齐（消除伪共享）+ 内存屏障（OSMemoryBarrier）
  · Hybrid Spin-Wait（高频 burst 自旋，低频 sem_wait 休眠）

功能测试：
  1. 版本查询 (GET_VERSION)
  2. 屏幕尺寸查询 (GET_SIZE)
  3. 截图 (SCREENSHOT)
  4. 触控操作 (TOUCH_DOWN / TOUCH_MOVE / TOUCH_UP)

性能测试：
  7. 截图帧率（连续 10 次）
  8. 批量点击延迟（100 次）
  9. 连续滑动（20 次）

用法：
    # 自动搜索容器
    python test_maa_tools_ipc.py

    # 指定 Bundle ID
    python test_maa_tools_ipc.py --bundle-id com.hypergryph.arknights

    # 指定容器路径
    python test_maa_tools_ipc.py --container /Users/co/Library/Containers/com.example.app
"""

import sys
import time
import argparse
from pathlib import Path
from typing import Optional, Tuple
from maa_ipc_semaphore import MaaToolsIPC


# ============================================================
# 截图保存
# ============================================================

def save_screenshot(data: bytes, width: int, height: int,
                    filename: str = "screenshot.png") -> bool:
    """将 BGRA 字节数据保存为 PNG 文件"""
    try:
        from PIL import Image
        img = Image.frombytes('RGBA', (width, height), data, 'raw', 'BGRA').convert('RGB')
        img.save(filename, 'PNG')
        print(f"💾 截图已保存: {filename} ({width}x{height})")
        return True
    except ImportError:
        print("⚠️  未安装 Pillow，无法保存图片（pip install Pillow）")
        return False
    except Exception as exc:
        print(f"❌ 保存截图失败: {exc}")
        return False


# ============================================================
# 功能测试
# ============================================================

def test_basic_features(client: MaaToolsIPC) -> Optional[Tuple[int, int]]:
    """测试基本功能（版本 / 尺寸 / 截图）"""
    print("\n" + "=" * 60)
    print("📋 基本功能测试")
    print("=" * 60)

    # ── 测试 1：协议版本 ────────────────────────────────────────
    print("\n🎯 测试 1: GET_VERSION")
    version = client.get_version()
    if version is not None:
        print(f"   ✓ 协议版本: {version}")
    else:
        print("   ✗ 查询失败")

    # ── 测试 2：屏幕尺寸 ────────────────────────────────────────
    print("\n🎯 测试 2: GET_SIZE")
    size = client.get_screen_size()
    if size:
        width, height = size
        print(f"   ✓ 屏幕尺寸: {width}x{height}")
    else:
        print("   ✗ 查询失败")
        return None

    # ── 测试 3：截图 ────────────────────────────────────────────
    print("\n🎯 测试 3: SCREENSHOT")
    start = time.perf_counter()
    screenshot_data = client.screenshot()
    elapsed_ms = (time.perf_counter() - start) * 1000

    if screenshot_data:
        expected = width * height * 4
        print(f"   ✓ 截图成功")
        print(f"   📦 数据大小: {len(screenshot_data)} bytes (期望: {expected})")
        print(f"   ⏱  耗时: {elapsed_ms:.2f}ms")
        save_screenshot(screenshot_data, width, height, "maa_ipc_screenshot.png")
    else:
        print("   ✗ 截图失败")

    return size


def test_touch_operations(client: MaaToolsIPC, width: int, height: int):
    """测试触控操作（TOUCH_DOWN / TOUCH_MOVE / TOUCH_UP，高层 tap/swipe/drag 由客户端组合）"""
    print("\n" + "=" * 60)
    print("👆 触控操作测试")
    print("=" * 60)

    # ── 测试 4：点击 ────────────────────────────────────────────
    print("\n🎯 测试 4: TAP")
    x, y = width // 2, height // 2
    start = time.perf_counter()
    ok = client.tap(x, y)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _report("点击", ok, elapsed_ms, f"位置: ({x},{y})")
    time.sleep(0.3)

    # ── 测试 5：滑动 ────────────────────────────────────────────
    print("\n🎯 测试 5: SWIPE")
    x1, y1 = width // 4,     height // 2
    x2, y2 = width * 3 // 4, height // 2
    duration = 500
    start = time.perf_counter()
    ok = client.swipe(x1, y1, x2, y2, duration)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _report("滑动", ok, elapsed_ms, f"({x1},{y1}) → ({x2},{y2}), {duration}ms")
    time.sleep(0.3)

    # ── 测试 6：拖拽 ────────────────────────────────────────────
    print("\n🎯 测试 6: DRAG")
    x1, y1 = width // 3,     height // 3
    x2, y2 = width * 2 // 3, height * 2 // 3
    duration = 500
    start = time.perf_counter()
    ok = client.drag(x1, y1, x2, y2, duration)
    elapsed_ms = (time.perf_counter() - start) * 1000
    _report("拖拽", ok, elapsed_ms, f"({x1},{y1}) → ({x2},{y2}), {duration}ms (含 100ms 长按)")


def _report(name: str, ok: bool, elapsed_ms: float, detail: str = ""):
    """统一格式化测试结果"""
    status = "✓" if ok else "✗"
    print(f"   {status} {name}{'成功' if ok else '失败'}")
    if detail:
        print(f"   📍 {detail}")
    print(f"   ⏱  耗时: {elapsed_ms:.2f}ms")


# ============================================================
# 性能测试
# ============================================================

def test_performance(client: MaaToolsIPC, width: int, height: int) -> dict:
    """
    性能测试套件

    Returns:
        包含各操作统计数据的字典
    """
    print("\n" + "=" * 60)
    print("⚡ 性能测试")
    print("=" * 60)

    results = {}

    # ── 测试 7：截图帧率（10 次）───────────────────────────────
    print("\n🎯 测试 7: 截图帧率（10 次连续截图）")
    times = []
    ok_count = 0
    for i in range(10):
        start = time.perf_counter()
        data = client.screenshot()
        elapsed_ms = (time.perf_counter() - start) * 1000
        if data:
            times.append(elapsed_ms)
            ok_count += 1
            print(f"   第 {i+1:2d} 次: {elapsed_ms:.2f}ms")
        else:
            print(f"   第 {i+1:2d} 次: ✗ 失败")

    if times:
        avg = sum(times) / len(times)
        fps = 1000 / avg if avg > 0 else 0
        _perf_summary("截图", ok_count, 10, times)
        print(f"   理论帧率: {fps:.1f} FPS")
        results['screenshot'] = dict(success=ok_count, total=10,
                                     avg=avg, mn=min(times), mx=max(times), fps=fps)

    # ── 测试 8：批量点击延迟（100 次）─────────────────────────
    print("\n🎯 测试 8: 批量点击延迟（100 次，duration=10ms）")
    times = []
    ok_count = 0
    cx, cy = width // 2, height // 2

    total_start = time.perf_counter()
    for i in range(100):
        px = cx + (i % 10 - 5) * 10
        py = cy + (i // 10 - 5) * 10
        start = time.perf_counter()
        ok = client.tap(px, py, duration=10)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if ok:
            times.append(elapsed_ms)
            ok_count += 1
    total_ms = (time.perf_counter() - total_start) * 1000

    if times:
        _perf_summary("点击", ok_count, 100, times)
        throughput = ok_count / (total_ms / 1000) if total_ms > 0 else 0
        print(f"   总耗时: {total_ms:.2f}ms  |  吞吐率: {throughput:.1f} 次/秒")
        results['tap'] = dict(success=ok_count, total=100, total_time=total_ms,
                              avg=sum(times)/len(times), mn=min(times), mx=max(times),
                              throughput=throughput)

    # ── 测试 9：连续滑动（20 次）──────────────────────────────
    print("\n🎯 测试 9: 连续滑动（20 次，duration=300ms）")
    times = []
    ok_count = 0

    total_start = time.perf_counter()
    for i in range(20):
        if i % 2 == 0:
            sx1, sy1, sx2, sy2 = width // 4, height // 2, width * 3 // 4, height // 2
        else:
            sx1, sy1, sx2, sy2 = width * 3 // 4, height // 2, width // 4, height // 2

        start = time.perf_counter()
        ok = client.swipe(sx1, sy1, sx2, sy2, 300)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if ok:
            times.append(elapsed_ms)
            ok_count += 1
        time.sleep(0.05)  # 50ms 间隔，避免过度占用
    total_ms = (time.perf_counter() - total_start) * 1000

    if times:
        _perf_summary("滑动", ok_count, 20, times)
        print(f"   总耗时: {total_ms:.2f}ms")
        results['swipe'] = dict(success=ok_count, total=20, total_time=total_ms,
                                avg=sum(times)/len(times), mn=min(times), mx=max(times))

    return results


def _perf_summary(name: str, ok: int, total: int, times: list):
    """输出性能统计摘要"""
    avg = sum(times) / len(times) if times else 0
    print(f"\n   📊 {name} 统计:")
    print(f"   成功率: {ok}/{total} ({ok/total*100:.1f}%)")
    print(f"   平均延迟: {avg:.2f}ms  |  最快: {min(times):.2f}ms  |  最慢: {max(times):.2f}ms")


# ============================================================
# 主入口
# ============================================================

def print_perf_summary(results: dict):
    """输出性能汇总表格"""
    print("\n" + "=" * 60)
    print("📊 性能测试结果汇总 (MaaToolsIPC — SandboxIPC 架构)")
    print("=" * 60)

    if 'screenshot' in results:
        s = results['screenshot']
        print(f"\n📸 截图性能:")
        print(f"   成功率: {s['success']}/{s['total']} ({s['success']/s['total']*100:.1f}%)")
        print(f"   平均: {s['avg']:.2f}ms  最快: {s['mn']:.2f}ms  最慢: {s['mx']:.2f}ms")
        print(f"   理论帧率: {s['fps']:.1f} FPS")

    if 'tap' in results:
        t = results['tap']
        print(f"\n👆 点击性能:")
        print(f"   成功率: {t['success']}/{t['total']} ({t['success']/t['total']*100:.1f}%)")
        print(f"   平均: {t['avg']:.2f}ms  最快: {t['mn']:.2f}ms  最慢: {t['mx']:.2f}ms")
        print(f"   总耗时: {t['total_time']:.2f}ms  吞吐率: {t['throughput']:.1f} 次/秒")

    if 'swipe' in results:
        sw = results['swipe']
        print(f"\n↔️  滑动性能:")
        print(f"   成功率: {sw['success']}/{sw['total']} ({sw['success']/sw['total']*100:.1f}%)")
        print(f"   平均: {sw['avg']:.2f}ms  最快: {sw['mn']:.2f}ms  最慢: {sw['mx']:.2f}ms")
        print(f"   总耗时: {sw['total_time']:.2f}ms")

    print("\n" + "=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='MaaToolsIPC 协议测试（SandboxIPC 重构版）',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--bundle-id',  help='应用的 Bundle ID，例如 com.hypergryph.arknights')
    parser.add_argument('--container',  help='沙盒容器完整路径')
    parser.add_argument('--no-perf',    action='store_true', help='跳过性能测试')
    parser.add_argument('--stop-game',  action='store_true',
                        help='所有测试完成后发送 TERMINATE 终止游戏（对应 TCP 的 TERM 命令）')
    args = parser.parse_args()

    print("=" * 60)
    print("🧪 MaaToolsIPC 协议测试（SandboxIPC 重构版）")
    print("=" * 60)
    print()
    print("Swift 侧架构：")
    print("  SandboxIPC/IPCSharedTypes.swift  — 共享数据结构 + 协议常量")
    print("  SandboxIPC/ShmRingBuffer.swift   — Cache Line 对齐环形缓冲区")
    print("  SandboxIPC/SandboxIPCServer.swift — Unix Socket 服务器")
    print("  Utils/MaaToolsIPC.swift          — 业务调度（截图/触控）")
    print()
    print("性能优化：")
    print("  · Hybrid Spin-Wait（自旋 200 次后 sem_wait）")
    print("  · CGContext 复用（截图零重建开销）")
    print("  · 零拷贝截图（直接渲染到共享内存）")
    print()
    print("测试内容：")
    print("  1. GET_VERSION  协议版本查询")
    print("  2. GET_SIZE     屏幕尺寸查询")
    print("  3. SCREENSHOT   截图并保存 maa_ipc_screenshot.png")
    print("  4. TAP          点击（屏幕中心）")
    print("  5. SWIPE        水平滑动")
    print("  6. DRAG         拖拽（含 100ms 长按）")
    if not args.no_perf:
        print("  7. 截图帧率测试（10 次）")
        print("  8. 批量点击延迟（100 次）")
        print("  9. 连续滑动（20 次）")
    if args.stop_game:
        print("  *. TERMINATE    终止游戏进程（所有测试完成后执行）")
    print()
    print("前置条件：")
    print("  · PlayCover 应用已启动")
    print("  · 游戏已运行（PlayTools 已注入）")
    print("  · maaToolsIPC = true（在 PlaySettings 中启用）")
    print()

    input("准备好后按 Enter 继续...")
    print()

    # 创建客户端
    if args.container:
        client = MaaToolsIPC(container_path=args.container)
    elif args.bundle_id:
        client = MaaToolsIPC(bundle_id=args.bundle_id)
    else:
        client = MaaToolsIPC()

    try:
        print("🔌 正在连接 MaaToolsIPC...")
        if not client.connect():
            print("\n❌ 连接失败")
            print("\n提示：")
            print("  · 确认游戏已启动并注入 PlayTools")
            print("  · 确认 maaToolsIPC 已在 PlaySettings 中启用")
            print("  · 使用 --bundle-id 或 --container 指定目标")
            return 1

        print("✅ 连接成功！\n")

        # 基本功能测试
        size = test_basic_features(client)
        if not size:
            print("\n❌ 无法获取屏幕尺寸，跳过后续测试")
            return 1
        width, height = size

        # 触控操作测试
        test_touch_operations(client, width, height)

        # 性能测试
        if not args.no_perf:
            print("\n" + "=" * 60)
            print("⚠️  即将执行性能测试（约 130 次操作，需 1~2 分钟）")
            print("=" * 60)
            input("\n按 Enter 开始，Ctrl+C 取消...\n")

            perf_results = test_performance(client, width, height)

            print("\n" + "=" * 60)
            print("✅ 所有测试完成！")
            print("=" * 60)

            if perf_results:
                print_perf_summary(perf_results)
        print("\n" + "=" * 60)
        print("✅ 功能测试完成！")
        print("=" * 60)

        if args.stop_game:
            print("\n" + "=" * 60)
            print("⚠️  即将发送 TERMINATE 终止游戏进程")
            print("=" * 60)
            input("\n按 Enter 确认，Ctrl+C 取消...\n")
            client.stop_game()

        return 0

    except KeyboardInterrupt:
        print("\n\n⚠️  测试被用户中断")
        return 1
    except Exception as exc:
        print(f"\n\n❌ 测试异常: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
