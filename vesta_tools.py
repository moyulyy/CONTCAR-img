# -*- coding: utf-8 -*-
"""
VESTA 自动化工具
================

核心类 :class:`Vesta`，封装两个常用的命令行功能：

1. 格式转换：把 VASP 的 ``CONTCAR`` / ``POSCAR`` 等结构文件转成 VESTA 的
   ``.vesta`` 格式（内部调用 ``VESTA.exe -nogui -i xxx -o yyy.vesta``）。
2. 导出图片：**不调整视角**，直接按 VESTA 当前保存的视角输出图片，
   并通过 ``scale`` 参数控制图片质量（``-export_img scale=N out.png``）。
   默认以“隐藏窗口”的方式后台出图（伪无头），屏幕上不会弹窗。

.. note::
   VESTA 的图片导出依赖 GUI/OpenGL，真正的 ``-nogui`` 无头模式不会生成图片；
   本工具用 Windows ``SW_HIDE`` 隐藏窗口来模拟无头，实现无人值守出图。

运行环境（Windows）::

    D:\\miniconda3\\envs\\chem_env\\python.exe

命令行用法示例::

    # 1) 只做格式转换：CONTCAR -> CONTCAR.vesta
    D:\\miniconda3\\envs\\chem_env\\python.exe vesta_tools.py test\\CONTCAR --no-image

    # 2) 转格式 + 导出图片（质量 scale=5，默认不调整视角）
    D:\\miniconda3\\envs\\chem_env\\python.exe vesta_tools.py test\\CONTCAR -o out.png --scale 5

    # 3) 批量处理
    D:\\miniconda3\\envs\\chem_env\\python.exe vesta_tools.py a\\CONTCAR b\\CONTCAR --scale 3

Python 调用示例::

    from vesta_tools import Vesta
    v = Vesta()                        # 使用默认 VESTA 路径
    v.contcar_to_vesta("test/CONTCAR")            # -> test/CONTCAR.vesta
    v.export_image("test/CONTCAR.vesta", "a.png", scale=5)   # 不调视角，输出图片
    # 或者一步到位：
    v.contcar_to_image("test/CONTCAR", "a.png", scale=5)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_VESTA_EXE = r"D:\software\VESTA-win64\VESTA-win64\VESTA.exe"
DEFAULT_SCALE = 5          # 图片质量 / 缩放倍数，越大越清晰、文件越大
DEFAULT_TIMEOUT = 300      # 单个 VESTA 进程最长等待时间（秒）

# Windows 下隐藏 VESTA 控制台窗口
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class VestaError(RuntimeError):
    """VESTA 执行失败时抛出。"""


class Vesta:
    """VESTA 命令行封装。

    Parameters
    ----------
    exe:
        ``VESTA.exe`` 的路径，默认使用 :data:`DEFAULT_VESTA_EXE`。
    timeout:
        单个任务默认超时时间（秒）。
    verbose:
        是否打印运行日志。
    show_window:
        **伪无头开关**。

        VESTA 导出图片依赖 GUI/OpenGL 渲染，真正的 ``-nogui`` 模式**不会**
        产生图片文件（实测直接退出且无输出）。因此这里的做法是：仍然用 GUI
        进程渲染，但通过 Windows ``STARTUPINFO(SW_HIDE)`` 把窗口隐藏，
        实现“无人值守、屏幕上不弹窗”的效果。

        * ``show_window=False``（默认）：隐藏窗口，后台静默出图；
        * ``show_window=True``：显示窗口（调试用）。

        注意：隐藏窗口仍要求 Windows 有可用的交互式桌面/图形会话；
        在“无用户登录的服务/计划任务”里 OpenGL 可能不可用。
    """

    def __init__(
        self,
        exe: str | Path = DEFAULT_VESTA_EXE,
        timeout: float = DEFAULT_TIMEOUT,
        verbose: bool = True,
        show_window: bool = False,
    ) -> None:
        self.exe = Path(exe)
        if not self.exe.exists():
            raise FileNotFoundError(f"找不到 VESTA 可执行文件: {self.exe}")
        self.timeout = float(timeout)
        self.verbose = verbose
        self.show_window = show_window

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """尽量优雅地结束进程，不行就强杀。"""
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def _run(
        self,
        args: list,
        cwd: Path,
        wait_for: Path | None = None,
        timeout: float | None = None,
        show_window: bool | None = None,
    ) -> str:
        """执行 VESTA 命令。

        由于 VESTA 导出图片后进程通常不会自动退出（即使带 ``-close``），
        这里用“等待输出文件生成并稳定”的方式判断任务完成，然后主动结束进程。

        注意：VESTA 的返回码不可靠（转换成功也可能返回 0xFFFFFFFF），
        因此以 **输出文件是否存在且非空** 作为成功判据。
        """
        timeout = float(timeout) if timeout else self.timeout
        cwd = Path(cwd)
        cmd = [str(self.exe)] + [str(a) for a in args]
        self._log("[VESTA] " + " ".join(cmd))

        out_path = Path(wait_for) if wait_for else None
        if out_path is not None and out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass

        # 无头（隐藏窗口）设置：VESTA 出图必须走 GUI，这里只是把窗口藏起来
        show_window = self.show_window if show_window is None else show_window
        startupinfo = None
        if not show_window and os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE

        ok = False
        with tempfile.TemporaryFile("w+b") as logf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=logf,
                stderr=subprocess.STDOUT,
                startupinfo=startupinfo,
                creationflags=_CREATE_NO_WINDOW,
            )
            try:
                if out_path is None:
                    # 没有明确输出文件：只能等进程结束
                    try:
                        proc.wait(timeout=timeout)
                        ok = True
                    except subprocess.TimeoutExpired:
                        ok = False
                else:
                    deadline = time.time() + timeout
                    last_size = -1
                    stable = 0
                    while time.time() < deadline:
                        if out_path.exists():
                            size = out_path.stat().st_size
                            if size > 0 and size == last_size:
                                stable += 1
                                if stable >= 2:      # 文件大小连续两次不变 => 写完
                                    time.sleep(0.3)  # 再给一点落盘时间
                                    ok = out_path.stat().st_size > 0
                                    break
                            else:
                                stable = 0
                            last_size = size

                        if proc.poll() is not None:
                            # 进程已退出（转换功能通常是这种情况）
                            time.sleep(0.3)
                            if out_path.exists() and out_path.stat().st_size > 0:
                                ok = True
                            break

                        time.sleep(0.5)
            finally:
                self._terminate(proc)

            logf.seek(0)
            log = logf.read().decode("utf-8", "replace")

        # 最终以输出文件为准
        if out_path is not None:
            ok = out_path.exists() and out_path.stat().st_size > 0

        if not ok:
            raise VestaError(
                "VESTA 执行失败或超时：\n"
                f"  命令: {' '.join(cmd)}\n"
                f"  输出: {out_path}\n"
                f"  日志:\n{log[-2000:]}"
            )

        if out_path is not None:
            size_kb = out_path.stat().st_size / 1024.0
            self._log(f"[VESTA] 完成 -> {out_path}  ({size_kb:.1f} KB)")
        else:
            self._log("[VESTA] 完成")
        return log

    # ------------------------------------------------------------------ #
    # 功能 1：格式转换
    # ------------------------------------------------------------------ #
    def convert(
        self,
        src: str | Path,
        dst: str | Path,
        timeout: float | None = None,
    ) -> Path:
        """通用格式转换（VESTA 支持 vasp/cif/vesta/xsf 等）。

        Parameters
        ----------
        src:
            输入结构文件，例如 ``CONTCAR`` / ``POSCAR`` / ``POSCAR.cif``。
        dst:
            输出文件，扩展名决定目标格式，例如 ``POSCAR.vesta``。
        """
        src = Path(src).resolve()
        dst = Path(dst).resolve()
        if not src.exists():
            raise FileNotFoundError(f"输入文件不存在: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)

        self._run(
            ["-nogui", "-i", str(src), "-o", str(dst)],
            cwd=src.parent,
            wait_for=dst,
            timeout=timeout,
        )
        return dst

    def contcar_to_vesta(
        self,
        contcar: str | Path,
        vesta_file: str | Path | None = None,
        timeout: float | None = None,
    ) -> Path:
        """把 ``CONTCAR`` / ``POSCAR`` 转成 ``.vesta`` 格式。

        默认输出到与输入同目录、同名的 ``.vesta`` 文件。
        """
        contcar = Path(contcar).resolve()
        if vesta_file is None:
            vesta_file = contcar.with_suffix(".vesta")
        return self.convert(contcar, vesta_file, timeout=timeout)

    # ------------------------------------------------------------------ #
    # 功能 2：导出图片（不调整视角）
    # ------------------------------------------------------------------ #
    def export_image(
        self,
        vesta_file: str | Path,
        image_file: str | Path,
        scale: float = DEFAULT_SCALE,
        rotate: dict | None = None,
        flush: bool = False,
        timeout: float | None = None,
        show_window: bool | None = None,
    ) -> Path:
        """打开 ``.vesta`` 并按当前视角输出图片。

        Parameters
        ----------
        vesta_file:
            VESTA 结构文件（``.vesta``）。
        image_file:
            输出图片路径，扩展名决定格式（``.png`` / ``.jpg`` / ``.bmp`` / ``.tif`` ...）。
        scale:
            图片质量（缩放倍数）。越大越清晰、文件越大，推荐 3~10。
        rotate:
            可选的视角旋转，形如 ``{"x": -90}`` 或 ``{"z": 45}``。
            **默认为 ``None``，即不调整视角**（符合本工具的主要使用场景）。
        flush:
            是否在导出前刷新视图（对应 ``-flush``）。
        show_window:
            是否显示 VESTA 窗口；``None`` 时沿用实例设置（默认隐藏窗口 = 伪无头）。
        """
        vesta_file = Path(vesta_file).resolve()
        image_file = Path(image_file).resolve()
        if not vesta_file.exists():
            raise FileNotFoundError(f"找不到 vesta 文件: {vesta_file}")
        image_file.parent.mkdir(parents=True, exist_ok=True)

        args: list = ["-open", str(vesta_file)]

        if rotate:  # 需要时才旋转；默认不传 => 不调整视角
            for axis, angle in rotate.items():
                axis = str(axis).lower().strip("_")
                if axis not in ("x", "y", "z"):
                    raise ValueError(f"旋转轴只能是 x/y/z，收到: {axis}")
                args += [f"-rotate_{axis}", str(angle)]
        if flush:
            args.append("-flush")

        args += ["-export_img", f"scale={scale}", str(image_file), "-close"]

        self._run(
            args,
            cwd=vesta_file.parent,
            wait_for=image_file,
            timeout=timeout,
            show_window=show_window,
        )
        return image_file

    def contcar_to_image(
        self,
        contcar: str | Path,
        image_file: str | Path | None = None,
        scale: float = DEFAULT_SCALE,
        rotate: dict | None = None,
        vesta_file: str | Path | None = None,
        keep_vesta: bool = True,
        timeout: float | None = None,
        show_window: bool | None = None,
    ) -> Path:
        """一步完成：CONTCAR -> .vesta -> 图片（不调整视角）。

        Parameters
        ----------
        keep_vesta:
            是否保留中间生成的 ``.vesta`` 文件，默认保留。
        """
        contcar = Path(contcar).resolve()
        if vesta_file is None:
            vesta_file = contcar.with_suffix(".vesta")
        vesta_file = Path(vesta_file).resolve()

        self.contcar_to_vesta(contcar, vesta_file, timeout=timeout)

        if image_file is None:
            image_file = contcar.with_name(contcar.stem + ".png")
        image_file = self.export_image(
            vesta_file, image_file, scale=scale, rotate=rotate, timeout=timeout,
            show_window=show_window,
        )

        if not keep_vesta:
            try:
                vesta_file.unlink()
            except OSError:
                pass
        return image_file


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="VESTA 工具：CONTCAR -> .vesta，以及按当前视角导出图片（scale 控制质量）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("contcar", nargs="+", help="一个或多个 CONTCAR/POSCAR 文件")
    p.add_argument("-o", "--output", default=None,
                   help="输出图片路径（仅单个输入时可用；默认 <CONTCAR>.png）")
    p.add_argument("-s", "--scale", type=float, default=DEFAULT_SCALE,
                   help="图片质量（VESTA 的 scale 参数），越大越清晰")
    p.add_argument("--vesta-file", default=None,
                   help="中间 .vesta 文件路径（仅单个输入时可用）")
    p.add_argument("--no-image", action="store_true",
                   help="只做格式转换，不导出图片")
    p.add_argument("--keep-vesta", action="store_true",
                   help="导出图片后保留中间的 .vesta 文件")
    p.add_argument("--rotate-x", type=float, default=None, help="绕 X 轴旋转角度（一般不用）")
    p.add_argument("--rotate-y", type=float, default=None, help="绕 Y 轴旋转角度（一般不用）")
    p.add_argument("--rotate-z", type=float, default=None, help="绕 Z 轴旋转角度（一般不用）")
    p.add_argument("--exe", default=DEFAULT_VESTA_EXE, help="VESTA.exe 路径")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单个任务超时（秒）")
    p.add_argument("--show-window", action="store_true",
                   help="显示 VESTA 窗口（默认隐藏窗口，后台伪无头出图）")
    p.add_argument("-q", "--quiet", action="store_true", help="静默模式")
    return p


def main(argv: list | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.output and len(args.contcar) > 1:
        print("错误：-o/--output 只能在单个输入时使用。", file=sys.stderr)
        return 2

    rotate = {}
    for axis in ("x", "y", "z"):
        val = getattr(args, f"rotate_{axis}")
        if val is not None:
            rotate[axis] = val
    rotate = rotate or None

    v = Vesta(exe=args.exe, timeout=args.timeout, verbose=not args.quiet,
              show_window=args.show_window)

    failed = 0
    for contcar in args.contcar:
        try:
            if args.no_image:
                v.contcar_to_vesta(contcar, args.vesta_file)
            else:
                v.contcar_to_image(
                    contcar,
                    image_file=args.output,
                    scale=args.scale,
                    rotate=rotate,
                    vesta_file=args.vesta_file,
                    keep_vesta=args.keep_vesta or args.vesta_file is not None,
                    show_window=args.show_window,
                )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[失败] {contcar}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
