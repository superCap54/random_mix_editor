# -*- coding: utf-8 -*-
"""
Random Mix Editor - PyQt6 + FFmpeg

功能：
1. 选择一个音频文件夹，每次随机选择 1 个 mp3 作为成片音频，成片时长以该 mp3 为准。
2. 用户按顺序导入多个视频文件夹，允许重复导入同一个文件夹。
3. 每个导入项都会按顺序随机抽取 1 条视频参与拼接。
   例如：B、B、C、A => 随机抽 B 一条 + 再随机抽 B 一条 + 随机抽 C 一条 + 随机抽 A 一条。
4. 如果视频总时长不足音频时长，会继续按照导入顺序循环随机抽取，直到覆盖音频时长。
5. 最终用 FFmpeg 合成 1080x1920、30fps、H.264/AAC 的 MP4。
6. 可选添加字幕：优先读取随机 MP3 同目录同名 SRT；没有则尝试 Whispy / Whisper 自动识别生成。

打包参考：
# 开发调试：把 ffmpeg.exe 放在本 py 文件同目录即可。ffprobe.exe 可选；没有 ffprobe.exe 时会用 ffmpeg.exe 读取时长。
# 打包 exe：把 ffmpeg.exe 和 impact.ttf 一起封装进去，用户电脑无需安装 FFmpeg。
python -m PyInstaller --onefile --windowed --add-binary "ffmpeg.exe;." --add-data "impact.ttf;." --hidden-import PyQt6 random_mix_editor.py
# 如果同目录也有 ffprobe.exe，可额外加：--add-binary "ffprobe.exe;."
# 如果需要自动识别字幕，请额外安装/打包 whispy、whisper 命令或 openai-whisper Python 库。
"""

from __future__ import annotations

import os
import sys
import json
import time
import random
import shutil
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QFont, QIcon, QColor, QPalette, QPainter, QPen, QBrush, QPainterPath, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QCheckBox,
    QColorDialog,
    QSlider,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QTextEdit,
    QSizePolicy,
    QScrollArea,
    QGraphicsDropShadowEffect,
)


VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3"}
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
OUTPUT_FPS = 30
DEFAULT_SUBTITLE_COLOR = "#FFFFFF"
DEFAULT_SUBTITLE_OUTLINE_COLOR = "#000000"
DEFAULT_SUBTITLE_SIZE = 42
DEFAULT_SUBTITLE_OUTLINE_WIDTH = 3
DEFAULT_SUBTITLE_MARGIN_V = 135
SUBTITLE_MARGIN_MIN = 20
SUBTITLE_MARGIN_MAX = 1760
SUBTITLE_FONT_NAME = "Impact"
SUBTITLE_SAFE_MARGIN_L = 70
SUBTITLE_SAFE_MARGIN_R = 70


# -----------------------------
# Utils
# -----------------------------

def resource_path(relative_path: str) -> str:
    """兼容 PyInstaller 打包后的只读资源路径。"""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def app_base_dir() -> str:
    """应用自身所在目录。用于保存 Whisper 模型等需要长期复用的文件。

    注意：PyInstaller onefile 的 _MEIPASS 是临时解压目录，每次启动都可能变化，
    不能用来保存模型缓存，否则会出现每次运行都重新下载模型的问题。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def app_writable_path(relative_path: str) -> str:
    """返回应用目录下可写路径，并自动创建父目录。"""
    path = os.path.join(app_base_dir(), relative_path)
    os.makedirs(path, exist_ok=True)
    return path


def bundled_executable_candidates(name: str) -> List[str]:
    """只查找应用自身携带的可执行文件，不再读取系统 PATH。"""
    candidates: List[str] = []

    # PyInstaller onefile / onedir 打包后，优先查找 exe 同目录，方便用户把 ffmpeg.exe 放在软件旁边。
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), name))

    # PyInstaller --add-binary / --add-data 会解压到 _MEIPASS。
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, name))  # type: ignore[attr-defined]

    # 开发环境：查找当前 .py 文件同目录。
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))

    # 双击/命令行启动时，也兼容当前工作目录。
    candidates.append(os.path.join(os.path.abspath("."), name))

    # 去重但保持顺序。
    deduped: List[str] = []
    for item in candidates:
        if item not in deduped:
            deduped.append(item)
    return deduped


def find_executable(name: str) -> str:
    """只使用程序自带的 ffmpeg/ffprobe；找不到时返回空字符串，不回退系统 PATH。"""
    for candidate in bundled_executable_candidates(name):
        if os.path.exists(candidate):
            return candidate
    return ""


def run_probe_command(cmd: List[str], timeout: int = 8) -> str:
    """运行检测命令，失败时返回空字符串，避免启动软件时弹错。"""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (result.stdout or "") + "\n" + (result.stderr or "")
    except Exception:
        return ""


def detect_gpu_names() -> List[str]:
    """尽量从系统里读取显卡名称。Windows 优先，其他系统做轻量兜底。"""
    names: List[str] = []

    if sys.platform.startswith("win"):
        # wmic 在部分新 Windows 上可能不存在，所以失败时再尝试 PowerShell。
        output = run_probe_command(["wmic", "path", "win32_VideoController", "get", "name"])
        for line in output.splitlines():
            value = line.strip()
            if value and value.lower() != "name" and "node -" not in value.lower():
                names.append(value)

        if not names:
            output = run_probe_command([
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"
            ])
            names.extend([line.strip() for line in output.splitlines() if line.strip()])
    elif sys.platform.startswith("linux"):
        output = run_probe_command(["lspci"])
        for line in output.splitlines():
            if "vga" in line.lower() or "3d controller" in line.lower():
                names.append(line.strip())
    elif sys.platform == "darwin":
        output = run_probe_command(["system_profiler", "SPDisplaysDataType"])
        for line in output.splitlines():
            if "Chipset Model:" in line:
                names.append(line.split(":", 1)[-1].strip())

    # nvidia-smi 是 NVIDIA 的强信号；有些机器 wmic 读取不到时可以兜底。
    nvidia_output = run_probe_command(["nvidia-smi", "-L"])
    for line in nvidia_output.splitlines():
        value = line.strip()
        if value and value not in names:
            names.append(value)

    # 去重但保持顺序。
    deduped: List[str] = []
    for name in names:
        if name not in deduped:
            deduped.append(name)
    return deduped


def get_ffmpeg_encoders(ffmpeg_path: str) -> str:
    """读取当前 FFmpeg 编译支持的编码器列表。"""
    return run_probe_command([ffmpeg_path, "-hide_banner", "-encoders"], timeout=10).lower()


def detect_gpu_acceleration(ffmpeg_path: str) -> GpuAccelerationInfo:
    """
    自动判断是否显示 GPU 加速选项。
    只有同时满足：检测到对应显卡 + 内置 FFmpeg 支持对应硬件编码器，才显示并默认勾选。
    """
    if not ffmpeg_path or not os.path.exists(ffmpeg_path):
        return GpuAccelerationInfo(False, reason="未找到程序自带的 ffmpeg.exe。")

    gpu_names = detect_gpu_names()
    gpu_text = "\n".join(gpu_names).lower()
    encoders = get_ffmpeg_encoders(ffmpeg_path)

    has_nvidia_gpu = any(word in gpu_text for word in ["nvidia", "geforce", "rtx", "gtx", "quadro"])
    has_amd_gpu = any(word in gpu_text for word in ["amd", "radeon", "advanced micro devices"])

    if has_nvidia_gpu and "h264_nvenc" in encoders:
        return GpuAccelerationInfo(
            available=True,
            vendor="NVIDIA",
            encoder="h264_nvenc",
            gpu_name=gpu_names[0] if gpu_names else "NVIDIA GPU",
        )

    if has_amd_gpu and "h264_amf" in encoders:
        return GpuAccelerationInfo(
            available=True,
            vendor="AMD",
            encoder="h264_amf",
            gpu_name=gpu_names[0] if gpu_names else "AMD GPU",
        )

    if has_nvidia_gpu and "h264_nvenc" not in encoders:
        return GpuAccelerationInfo(False, reason="检测到 NVIDIA 显卡，但当前 FFmpeg 不支持 h264_nvenc。")
    if has_amd_gpu and "h264_amf" not in encoders:
        return GpuAccelerationInfo(False, reason="检测到 AMD 显卡，但当前 FFmpeg 不支持 h264_amf。")
    return GpuAccelerationInfo(False, reason="未检测到可用的 NVIDIA/AMD 硬件编码环境。")


def list_files(folder: str, extensions: set[str]) -> List[str]:
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return []
    return [str(x) for x in p.iterdir() if x.is_file() and x.suffix.lower() in extensions]


def safe_stem(path: str) -> str:
    stem = Path(path).stem
    keep = []
    for ch in stem:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "mix"


def seconds_to_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def seconds_to_srt_time(seconds: float) -> str:
    """把秒数转换成 SRT 时间戳。"""
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    if milliseconds >= 1000:
        milliseconds -= 1000
        whole_seconds += 1
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def clean_subtitle_text(text: str) -> str:
    return (text or "").strip().replace("\r", " ").replace("\n", " ")


def hex_to_ass_color(hex_color: str) -> str:
    """FFmpeg subtitles force_style 使用 ASS 颜色：&HAABBGGRR。"""
    value = (hex_color or DEFAULT_SUBTITLE_COLOR).strip().lstrip("#")
    if len(value) != 6:
        value = DEFAULT_SUBTITLE_COLOR.lstrip("#")
    rr, gg, bb = value[0:2], value[2:4], value[4:6]
    return f"&H00{bb}{gg}{rr}"


def srt_time_to_ass_time(value: str) -> str:
    """把 SRT 时间戳转换成 ASS 时间戳：0:00:00.00。"""
    value = (value or "").strip().replace(",", ".")
    match = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?", value)
    if not match:
        return "0:00:00.00"
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    ms = int((match.group(4) or "0").ljust(3, "0")[:3])
    centiseconds = int(round(ms / 10))
    if centiseconds >= 100:
        centiseconds = 0
        seconds += 1
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        hours += 1
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def escape_ass_dialogue_text(text: str) -> str:
    """转义 ASS Dialogue 文本，避免字幕内容被当成 ASS 样式标签。"""
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("{", "(").replace("}", ")")
    value = value.replace("\\", r"\\")
    lines = [line.strip() for line in value.split("\n") if line.strip()]
    return r"\N".join(lines)


def parse_srt_cues(srt_path: str) -> List[Tuple[str, str, str]]:
    """读取 SRT，返回 [(start_ass, end_ass, text_ass), ...]。"""
    raw = Path(srt_path).read_text(encoding="utf-8-sig", errors="ignore")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []

    cues: List[Tuple[str, str, str]] = []
    blocks = re.split(r"\n\s*\n", raw)
    time_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    )

    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        time_index = -1
        match = None
        for idx, line in enumerate(lines):
            match = time_pattern.search(line)
            if match:
                time_index = idx
                break

        if time_index < 0 or not match:
            continue

        text_lines = lines[time_index + 1:]
        text_value = escape_ass_dialogue_text("\n".join(text_lines))
        if not text_value:
            continue

        cues.append((
            srt_time_to_ass_time(match.group("start")),
            srt_time_to_ass_time(match.group("end")),
            text_value,
        ))

    return cues


def write_ass_from_srt(
    srt_path: str,
    ass_path: str,
    font_name: str,
    font_color: str,
    outline_color: str,
    outline_width: int,
    font_size: int,
    margin_v: int,
):
    """
    把 SRT 转成带真实 1080x1920 分辨率的 ASS 字幕。
    这样字体大小、描边和上下位置会和最终视频坐标一致，不会因为 FFmpeg 默认 ASS 分辨率导致字幕跑到画面外。
    """
    cues = parse_srt_cues(srt_path)
    if not cues:
        raise RuntimeError(f"SRT 文件没有有效字幕片段：{srt_path}")

    primary_color = hex_to_ass_color(font_color)
    outline = hex_to_ass_color(outline_color)
    font_size = max(12, min(120, int(font_size or DEFAULT_SUBTITLE_SIZE)))
    outline_width = max(0, min(12, int(outline_width if outline_width is not None else DEFAULT_SUBTITLE_OUTLINE_WIDTH)))
    # ASS 的 MarginV 在这里按 1080x1920 真实画布解释。
    margin_v = max(SUBTITLE_MARGIN_MIN, min(SUBTITLE_MARGIN_MAX, int(margin_v or DEFAULT_SUBTITLE_MARGIN_V)))

    margin_l = SUBTITLE_SAFE_MARGIN_L
    margin_r = SUBTITLE_SAFE_MARGIN_R
    shadow = 0

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        f"PlayResX: {OUTPUT_WIDTH}",
        f"PlayResY: {OUTPUT_HEIGHT}",
        "Timer: 100.0000",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{font_size},{primary_color},&H000000FF,{outline},&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline_width},{shadow},2,{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for start, end, content in cues:
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{content}")

    Path(ass_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_ffmpeg_filter_path(path: str) -> str:
    """给 subtitles/fontsdir 过滤器参数使用的路径转义，兼容 Windows 盘符和空格。"""
    fixed = os.path.abspath(path).replace("\\", "/")
    fixed = fixed.replace("'", r"\\'")
    fixed = fixed.replace(":", r"\:")
    return fixed


def find_optional_command(names: List[str]) -> str:
    """查找可选外部命令：优先应用自身目录，再允许 PATH 里的开发环境命令。"""
    for name in names:
        for candidate in bundled_executable_candidates(name):
            if os.path.exists(candidate):
                return candidate
        found = shutil.which(name)
        if found:
            return found
    return ""


@dataclass
class GpuAccelerationInfo:
    available: bool
    vendor: str = ""
    encoder: str = ""
    gpu_name: str = ""
    reason: str = ""


@dataclass
class MixJobConfig:
    audio_folder: str
    video_folders: List[str]
    output_folder: str
    output_count: int
    ffmpeg_path: str
    ffprobe_path: str
    gpu_enabled: bool = False
    gpu_encoder: str = ""
    gpu_vendor: str = ""
    subtitle_enabled: bool = False
    subtitle_font_color: str = DEFAULT_SUBTITLE_COLOR
    subtitle_outline_color: str = DEFAULT_SUBTITLE_OUTLINE_COLOR
    subtitle_outline_width: int = DEFAULT_SUBTITLE_OUTLINE_WIDTH
    subtitle_font_size: int = DEFAULT_SUBTITLE_SIZE
    subtitle_margin_v: int = DEFAULT_SUBTITLE_MARGIN_V
    subtitle_font_path: str = ""


# -----------------------------
# Worker Thread
# -----------------------------

class MixWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config: MixJobConfig):
        super().__init__()
        self.config = config
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        try:
            self.validate_environment()
            for index in range(1, self.config.output_count + 1):
                if self._stopped:
                    self.log.emit("任务已停止。")
                    break
                self.create_one_mix(index)
                self.progress.emit(int(index / self.config.output_count * 100))
            self.finished_ok.emit("混剪任务完成。")
        except Exception as exc:
            self.failed.emit(str(exc))

    def validate_environment(self):
        if not os.path.exists(self.config.audio_folder):
            raise RuntimeError("音频文件夹不存在。")
        if not os.path.exists(self.config.output_folder):
            raise RuntimeError("输出文件夹不存在。")
        if not self.config.video_folders:
            raise RuntimeError("请至少导入一个视频文件夹。")

        audio_files = list_files(self.config.audio_folder, AUDIO_EXTS)
        if not audio_files:
            raise RuntimeError("音频文件夹里没有找到 mp3 文件。")

        for folder in self.config.video_folders:
            if not os.path.exists(folder):
                raise RuntimeError(f"视频文件夹不存在：{folder}")
            if not list_files(folder, VIDEO_EXTS):
                raise RuntimeError(f"视频文件夹里没有可用视频：{folder}")

        # 检查内置 ffmpeg 是否可运行。ffprobe 可选：没有 ffprobe 时，会用 ffmpeg 兜底读取时长。
        self.check_tool(self.config.ffmpeg_path, "ffmpeg")
        if self.config.ffprobe_path:
            self.check_tool(self.config.ffprobe_path, "ffprobe")
        else:
            self.log.emit("未检测到内置 ffprobe.exe，将使用内置 ffmpeg.exe 读取素材时长。")

    def check_tool(self, tool_path: str, name: str):
        if not tool_path or not os.path.exists(tool_path):
            raise RuntimeError(
                f"没有找到程序自带的 {name}.exe。\n"
                f"请把 {name}.exe 放到本程序同目录；打包 exe 时请用 --add-binary 把它封装进去。"
            )
        try:
            result = subprocess.run(
                [tool_path, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise RuntimeError
        except Exception:
            raise RuntimeError(
                f"程序自带的 {name}.exe 无法运行。\n"
                f"请确认 {name}.exe 文件完整，并且和本程序位数/系统环境兼容。"
            )

    def get_duration(self, media_path: str) -> float:
        # 优先使用内置 ffprobe.exe，速度更快、结果更干净。
        if self.config.ffprobe_path:
            cmd = [
                self.config.ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                media_path,
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                data = json.loads(result.stdout or "{}")
                duration = float(data.get("format", {}).get("duration", 0) or 0)
                if duration > 0:
                    return duration

        # 如果用户只放了 ffmpeg.exe，没有放 ffprobe.exe，则用 ffmpeg -i 的 Duration 信息兜底。
        cmd = [self.config.ffmpeg_path, "-hide_banner", "-i", media_path]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if not match:
            raise RuntimeError(f"读取媒体时长失败：{media_path}")

        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        duration = hours * 3600 + minutes * 60 + seconds
        if duration <= 0:
            raise RuntimeError(f"媒体时长无效：{media_path}")
        return duration

    def resolve_subtitle_for_audio(self, audio_path: str) -> str:
        """字幕开启时：优先读取同名 SRT；没有就自动识别并保存到音频同目录。"""
        srt_path = str(Path(audio_path).with_suffix(".srt"))
        if os.path.exists(srt_path):
            self.log.emit(f"字幕：已找到同名 SRT：{Path(srt_path).name}")
            return srt_path

        self.log.emit("字幕：未找到同名 SRT，开始调用 Whispy / Whisper 自动识别...")
        self.generate_srt_from_audio(audio_path, srt_path)
        if not os.path.exists(srt_path):
            raise RuntimeError("字幕识别完成后仍未生成 SRT 文件。")
        self.log.emit(f"字幕：已生成并保存：{Path(srt_path).name}")
        return srt_path

    def generate_srt_from_audio(self, audio_path: str, srt_path: str):
        """
        自动字幕识别：
        1. 优先尝试 whispy / whisper 命令行；
        2. 如果没有命令行，再尝试 Python openai-whisper 库；
        3. 都不可用时，提示用户安装识别依赖或手动放同名 srt。
        """
        output_dir = str(Path(srt_path).parent)
        expected_name = Path(srt_path).name

        # 兼容用户说的 whispy，也兼容 OpenAI Whisper CLI 的 whisper 命令。
        command_candidates: List[List[str]] = []
        whispy_cmd = find_optional_command(["whispy.exe", "whispy"])
        if whispy_cmd:
            command_candidates.append([whispy_cmd, audio_path, "--output_format", "srt", "--output_dir", output_dir])
            command_candidates.append([whispy_cmd, audio_path, "--output", srt_path])

        whisper_cmd = find_optional_command(["whisper.exe", "whisper"])
        if whisper_cmd:
            command_candidates.append([
                whisper_cmd,
                audio_path,
                "--model", "base",
                "--output_format", "srt",
                "--output_dir", output_dir,
            ])

        for cmd in command_candidates:
            try:
                self.log.emit(f"字幕：尝试命令行识别：{Path(cmd[0]).name}")
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                # whisper CLI 默认会在 output_dir 里生成同名 srt。
                if os.path.exists(srt_path):
                    return
                generated = Path(output_dir) / expected_name
                if generated.exists():
                    return
                if result.returncode != 0 and result.stderr:
                    self.log.emit(result.stderr[-800:])
            except Exception as exc:
                self.log.emit(f"字幕：命令行识别失败：{exc}")

        # Python 库兜底：pip install openai-whisper
        try:
            self.log.emit("字幕：尝试 Python Whisper 库识别。首次运行可能需要下载模型。")
            import whisper  # type: ignore

            whisper_model_dir = app_writable_path("whisper_models")
            self.log.emit(f"字幕：Whisper 模型缓存目录：{whisper_model_dir}")
            model = whisper.load_model("base", download_root=whisper_model_dir)
            result = model.transcribe(audio_path, fp16=False)
            segments = result.get("segments", []) if isinstance(result, dict) else []
            if not segments:
                raise RuntimeError("Whisper 没有返回有效字幕片段。")

            with open(srt_path, "w", encoding="utf-8") as f:
                for idx, seg in enumerate(segments, start=1):
                    start = seconds_to_srt_time(float(seg.get("start", 0)))
                    end = seconds_to_srt_time(float(seg.get("end", 0)))
                    content = clean_subtitle_text(str(seg.get("text", "")))
                    if not content:
                        continue
                    f.write(f"{idx}\n{start} --> {end}\n{content}\n\n")
            return
        except Exception as exc:
            raise RuntimeError(
                "没有找到同名 SRT，且自动识别失败。\n"
                "你可以：\n"
                "1. 在音频同目录放一个同名 .srt；或\n"
                "2. 安装/打包 whispy、whisper 命令；或\n"
                "3. 安装 openai-whisper：pip install openai-whisper。\n"
                f"原始错误：{exc}"
            )

    def pick_video_sequence(self, audio_duration: float) -> List[str]:
        sequence: List[str] = []
        total = 0.0
        folder_index = 0
        max_rounds = 10000
        rounds = 0

        # 按用户导入顺序循环抽取，允许重复文件夹作为独立步骤。
        while total < audio_duration and rounds < max_rounds:
            folder = self.config.video_folders[folder_index]
            candidates = list_files(folder, VIDEO_EXTS)
            if not candidates:
                raise RuntimeError(f"没有可用视频：{folder}")
            video = random.choice(candidates)
            duration = self.get_duration(video)
            sequence.append(video)
            total += duration

            folder_index = (folder_index + 1) % len(self.config.video_folders)
            rounds += 1

        if total < audio_duration:
            raise RuntimeError("视频素材时长不足，无法覆盖音频时长。")
        return sequence

    def create_one_mix(self, index: int):
        audio_files = list_files(self.config.audio_folder, AUDIO_EXTS)
        audio_path = random.choice(audio_files)
        audio_duration = self.get_duration(audio_path)

        self.log.emit(f"\n[{index}/{self.config.output_count}] 随机音频：{Path(audio_path).name}")
        self.log.emit(f"音频时长：{seconds_to_hms(audio_duration)}")

        subtitle_path = ""
        if self.config.subtitle_enabled:
            subtitle_path = self.resolve_subtitle_for_audio(audio_path)
            self.log.emit(f"字幕样式：字体色 {self.config.subtitle_font_color} / 边框色 {self.config.subtitle_outline_color} / 边框粗细 {self.config.subtitle_outline_width} / 字号 {self.config.subtitle_font_size} / 位置 {self.config.subtitle_margin_v}")

        video_sequence = self.pick_video_sequence(audio_duration)
        self.log.emit("视频顺序：")
        for i, video in enumerate(video_sequence, start=1):
            self.log.emit(f"  {i}. {Path(video).parent.name} / {Path(video).name}")

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        audio_name = safe_stem(audio_path)
        output_name = f"mix_{timestamp}_{index:03d}_{audio_name}.mp4"
        output_path = os.path.join(self.config.output_folder, output_name)

        self.run_ffmpeg(video_sequence, audio_path, audio_duration, output_path, subtitle_path)
        self.log.emit(f"输出完成：{output_name}")

    def gpu_video_options(self) -> List[str]:
        """GPU 编码参数。这里做硬件编码加速，滤镜规格统一逻辑保持原样。"""
        encoder = self.config.gpu_encoder
        if encoder == "h264_nvenc":
            return [
                "-c:v", "h264_nvenc",
                "-preset", "medium",
                "-b:v", "8M",
                "-maxrate", "12M",
                "-bufsize", "16M",
            ]
        if encoder == "h264_amf":
            return [
                "-c:v", "h264_amf",
                "-quality", "balanced",
                "-b:v", "8M",
            ]
        return ["-c:v", encoder or "libx264", "-b:v", "8M"]

    def cpu_video_options(self) -> List[str]:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "23"]

    def build_ffmpeg_command(
        self,
        videos: List[str],
        audio_path: str,
        audio_duration: float,
        output_path: str,
        use_gpu: bool,
        subtitle_path: str = "",
    ) -> List[str]:
        cmd = [self.config.ffmpeg_path, "-hide_banner", "-y"]

        for video in videos:
            cmd.extend(["-i", video])
        audio_input_index = len(videos)
        cmd.extend(["-i", audio_path])

        filter_parts = []
        concat_inputs = []
        for i in range(len(videos)):
            # 统一视频规格，避免 concat 因分辨率/帧率/像素格式不同失败。
            filter_parts.append(
                f"[{i}:v]"
                f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,fps={OUTPUT_FPS},format=yuv420p[v{i}]"
            )
            concat_inputs.append(f"[v{i}]")

        if subtitle_path:
            filter_parts.append("".join(concat_inputs) + f"concat=n={len(videos)}:v=1:a=0[vjoin]")

            # 之前直接用 SRT + force_style 时，FFmpeg/libass 会使用默认脚本分辨率，
            # MarginV/FontSize 在部分机器上会被解释错，导致字幕实际跑到画面外，看起来像“没有字幕”。
            # 这里先把 SRT 转成带 PlayResX/PlayResY=1080x1920 的 ASS，再烧录，位置和字号才稳定。
            ass_path = os.path.splitext(output_path)[0] + "_subtitle.ass"
            write_ass_from_srt(
                subtitle_path,
                ass_path,
                SUBTITLE_FONT_NAME,
                self.config.subtitle_font_color,
                self.config.subtitle_outline_color,
                self.config.subtitle_outline_width,
                self.config.subtitle_font_size,
                self.config.subtitle_margin_v,
            )
            ass_fixed = escape_ffmpeg_filter_path(ass_path)

            if self.config.subtitle_font_path and os.path.exists(self.config.subtitle_font_path):
                font_dir = escape_ffmpeg_filter_path(os.path.dirname(self.config.subtitle_font_path))
            else:
                font_dir = escape_ffmpeg_filter_path(os.path.dirname(resource_path("impact.ttf")))

            filter_parts.append(
                f"[vjoin]subtitles='{ass_fixed}':fontsdir='{font_dir}'[vout]"
            )
        else:
            filter_parts.append("".join(concat_inputs) + f"concat=n={len(videos)}:v=1:a=0[vout]")

        filter_complex = ";".join(filter_parts)

        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", f"{audio_input_index}:a:0",
            "-t", f"{audio_duration:.3f}",
        ])

        if use_gpu:
            cmd.extend(self.gpu_video_options())
        else:
            cmd.extend(self.cpu_video_options())

        cmd.extend([
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
            output_path,
        ])
        return cmd

    def execute_ffmpeg_command(self, cmd: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def run_ffmpeg(self, videos: List[str], audio_path: str, audio_duration: float, output_path: str, subtitle_path: str = ""):
        use_gpu = bool(self.config.gpu_enabled and self.config.gpu_encoder)
        subtitle_note = "，并烧录字幕" if subtitle_path else ""
        temp_ass_path = os.path.splitext(output_path)[0] + "_subtitle.ass" if subtitle_path else ""

        try:
            if use_gpu:
                self.log.emit(f"开始 FFmpeg 合成{subtitle_note}... 使用 GPU 硬件编码：{self.config.gpu_vendor} / {self.config.gpu_encoder}")
                cmd = self.build_ffmpeg_command(videos, audio_path, audio_duration, output_path, use_gpu=True, subtitle_path=subtitle_path)
                result = self.execute_ffmpeg_command(cmd)
                if result.returncode == 0:
                    return

                # 硬件编码对驱动/FFmpeg 版本比较敏感。即使启动时检测可用，也可能因驱动占用或素材异常失败。
                # 这里自动回退 CPU，避免用户任务直接中断。
                self.log.emit("GPU 编码失败，已自动回退 CPU 编码继续生成。")
                if result.stderr:
                    self.log.emit(result.stderr[-1200:])

            else:
                self.log.emit(f"开始 FFmpeg 合成{subtitle_note}... 使用 CPU 编码。")

            cpu_cmd = self.build_ffmpeg_command(videos, audio_path, audio_duration, output_path, use_gpu=False, subtitle_path=subtitle_path)
            cpu_result = self.execute_ffmpeg_command(cpu_cmd)
            if cpu_result.returncode != 0:
                raise RuntimeError(f"FFmpeg 合成失败：\n{cpu_result.stderr[-4000:]}")
        finally:
            # ASS 只是为了稳定烧录字幕的中间文件。合成结束后自动删除，避免输出目录里多出 _subtitle.ass。
            if temp_ass_path and os.path.exists(temp_ass_path):
                try:
                    os.remove(temp_ass_path)
                except Exception:
                    pass


# -----------------------------
# UI Widgets
# -----------------------------

class ModernButton(QPushButton):
    def __init__(self, text: str, kind: str = "primary"):
        super().__init__(text)
        self.kind = kind
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)
        self.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Medium))
        self.apply_style()

    def apply_style(self):
        if self.kind == "primary":
            self.setStyleSheet("""
                QPushButton {
                    background: #111827;
                    color: #FFFFFF;
                    border: none;
                    border-radius: 12px;
                    padding: 9px 18px;
                }
                QPushButton:hover { background: #1F2937; }
                QPushButton:pressed { background: #030712; }
                QPushButton:disabled { background: #CBD5E1; color: #F8FAFC; }
            """)
        elif self.kind == "soft":
            self.setStyleSheet("""
                QPushButton {
                    background: #F8FAFC;
                    color: #334155;
                    border: 1px solid #DDE5EF;
                    border-radius: 12px;
                    padding: 9px 15px;
                }
                QPushButton:hover { background: #EEF2F7; }
                QPushButton:pressed { background: #E2E8F0; }
            """)
        elif self.kind == "danger":
            self.setStyleSheet("""
                QPushButton {
                    background: #FFF5F5;
                    color: #B91C1C;
                    border: 1px solid #FECACA;
                    border-radius: 12px;
                    padding: 9px 15px;
                }
                QPushButton:hover { background: #FEE2E2; }
            """)
        else:
            self.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #64748B;
                    border: none;
                    border-radius: 10px;
                    padding: 8px 12px;
                }
                QPushButton:hover { background: #F1F5F9; color: #0F172A; }
            """)


class Card(QFrame):
    """只给卡片本身加边框，避免 QLabel 标题继承出奇怪的框线。"""
    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QFrame#Card {
                background: #FFFFFF;
                border: 1px solid #E5E7EB;
                border-radius: 20px;
            }
            QFrame#Card QLabel {
                border: none;
                background: transparent;
            }
        """)


class SuccessDialog(QDialog):
    """任务完成后的自定义弹窗。只替换 QMessageBox 的视觉表现，不影响混剪业务逻辑。"""
    def __init__(self, parent=None, message: str = "混剪任务完成。", output_folder: str = ""):
        super().__init__(parent)
        self.output_folder = output_folder
        self.setModal(True)
        self.setWindowTitle("任务完成")
        self.setFixedWidth(460)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("SuccessCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 14)
        shadow.setColor(QColor(15, 23, 42, 55))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        icon = QLabel("✓")
        icon.setObjectName("SuccessIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(66, 66)
        layout.addWidget(icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("混剪任务已完成")
        title.setObjectName("SuccessTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        desc = QLabel("所有视频已经生成完成，可以前往输出文件夹查看成片。")
        desc.setObjectName("SuccessDesc")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        message_label = QLabel(message)
        message_label.setObjectName("SuccessMessage")
        message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_label.setWordWrap(True)
        layout.addWidget(message_label)

        if output_folder:
            path_text = output_folder
            if len(path_text) > 48:
                path_text = "..." + path_text[-45:]
            path_label = QLabel(path_text)
            path_label.setObjectName("SuccessPath")
            path_label.setToolTip(output_folder)
            path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(path_label)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 8, 0, 0)
        btn_row.setSpacing(12)

        close_btn = QPushButton("完成")
        close_btn.setObjectName("CloseButton")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        if output_folder:
            open_btn = QPushButton("打开输出文件夹")
            open_btn.setObjectName("OpenButton")
            open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            open_btn.clicked.connect(self.open_output_folder)
            btn_row.addWidget(open_btn)

        layout.addLayout(btn_row)

        self.setStyleSheet("""
            QFrame#SuccessCard {
                background: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 26px;
            }
            QLabel {
                border: none;
                background: transparent;
                font-family: "Microsoft YaHei UI";
            }
            QLabel#SuccessIcon {
                background: #DCFCE7;
                color: #16A34A;
                border-radius: 33px;
                font-size: 34px;
                font-weight: 900;
            }
            QLabel#SuccessTitle {
                color: #0F172A;
                font-size: 22px;
                font-weight: 900;
            }
            QLabel#SuccessDesc {
                color: #64748B;
                font-size: 13px;
                line-height: 1.45;
            }
            QLabel#SuccessMessage {
                background: #F8FAFC;
                color: #334155;
                border: 1px solid #E2E8F0;
                border-radius: 14px;
                padding: 12px 14px;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#SuccessPath {
                background: #F8FAFC;
                color: #64748B;
                border: 1px dashed #CBD5E1;
                border-radius: 12px;
                padding: 10px 12px;
                font-size: 12px;
            }
            QPushButton {
                min-height: 42px;
                border-radius: 13px;
                font-size: 13px;
                font-weight: 800;
                padding: 0 18px;
                font-family: "Microsoft YaHei UI";
            }
            QPushButton#CloseButton {
                background: #F8FAFC;
                color: #334155;
                border: 1px solid #DDE5EF;
            }
            QPushButton#CloseButton:hover {
                background: #EEF2F7;
            }
            QPushButton#OpenButton {
                background: #111827;
                color: #FFFFFF;
                border: none;
            }
            QPushButton#OpenButton:hover {
                background: #1F2937;
            }
        """)

    def open_output_folder(self):
        if self.output_folder and os.path.isdir(self.output_folder):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(self.output_folder)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", self.output_folder])
                else:
                    subprocess.Popen(["xdg-open", self.output_folder])
            except Exception:
                pass
        self.accept()


class PathPicker(Card):
    """紧凑型路径选择卡片。适合左右并排，减少纵向占位。"""
    def __init__(self, title: str, subtitle: str, button_text: str):
        super().__init__()
        self.path = ""
        self.setMinimumHeight(106)
        self.setMaximumHeight(116)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color:#0F172A; font-size:15px; font-weight:800; border:none; background:transparent;")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setStyleSheet("color:#64748B; font-size:12px; border:none; background:transparent;")
        self.path_label = QLabel("未选择")
        self.path_label.setStyleSheet("""
            QLabel {
                background: #F8FAFC;
                color: #64748B;
                border: 1px solid #E2E8F0;
                border-radius: 12px;
                padding: 9px 11px;
                font-size: 12px;
            }
        """)
        self.path_label.setMinimumHeight(38)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.button = ModernButton(button_text, "soft")
        self.button.setFixedWidth(106)
        self.button.setMinimumHeight(38)

        layout = QGridLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)
        layout.addWidget(self.title_label, 0, 0, 1, 2)
        layout.addWidget(self.subtitle_label, 1, 0, 1, 2)
        layout.addWidget(self.path_label, 2, 0)
        layout.addWidget(self.button, 2, 1)
        layout.setColumnStretch(0, 1)

    def set_path(self, path: str):
        self.path = path
        display = path
        if len(display) > 42:
            display = "..." + display[-39:]
        self.path_label.setText(display)
        self.path_label.setToolTip(path)

def compact_text(text: str, limit: int = 12) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


class FolderStepItem(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, index: int, folder: str, selected: bool = False):
        super().__init__()
        self.index = index - 1
        self.folder = folder
        self.selected = selected
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(86)
        self.setMinimumWidth(118)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(folder)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(7)

        badge = QLabel(str(index))
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(24, 24)
        badge.setObjectName("stepBadge")

        step = QLabel("步骤")
        step.setObjectName("stepText")

        top.addWidget(badge)
        top.addWidget(step)
        top.addStretch()

        folder_name = Path(folder).name or folder
        name = QLabel(compact_text(folder_name, 12))
        name.setObjectName("folderName")
        name.setToolTip(folder)

        layout.addLayout(top)
        layout.addWidget(name)
        self.apply_style()

    def apply_style(self):
        if self.selected:
            border = "2px solid #111827"
            bg = "#F8FAFC"
        else:
            border = "1px solid #E2E8F0"
            bg = "#FFFFFF"
        self.setStyleSheet(f"""
            QFrame {{
                background: {bg};
                border: {border};
                border-radius: 16px;
            }}
            QLabel {{
                border: none;
                background: transparent;
            }}
            QLabel#stepBadge {{
                background: #111827;
                color: white;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 800;
            }}
            QLabel#stepText {{
                color: #64748B;
                font-size: 11px;
                font-weight: 700;
            }}
            QLabel#folderName {{
                color: #0F172A;
                font-size: 13px;
                font-weight: 800;
            }}
        """)

    def mousePressEvent(self, event):
        # 注意：点击卡片后主窗口会刷新/重建网格。
        # 如果在这里继续调用 super().mousePressEvent(event)，当前卡片可能已被 deleteLater，
        # Windows + PyQt6 下会偶发直接闪退。这里直接接管点击事件。
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.clicked.emit(self.index)
            return
        super().mousePressEvent(event)




class CountStepper(QWidget):
    """稳定显示生成数量的自定义步进器，避免 Windows 原生 QSpinBox 在部分缩放/DPI 下文本不可见。"""
    def __init__(self, minimum: int = 1, maximum: int = 999, value: int = 1):
        super().__init__()
        self._minimum = minimum
        self._maximum = maximum
        self._value = value
        self.setFixedSize(148, 42)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget {
                background: #F8FAFC;
                border: 1px solid #DDE5EF;
                border-radius: 12px;
            }
            QPushButton {
                background: transparent;
                color: #0F172A;
                border: none;
                font-size: 18px;
                font-weight: 800;
            }
            QPushButton:hover { background: #EEF2F7; }
            QPushButton:pressed { background: #E2E8F0; }
            QPushButton:disabled { color: #CBD5E1; }
            QLabel {
                background: transparent;
                border: none;
                color: #0F172A;
                font-size: 15px;
                font-weight: 800;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.minus_btn = QPushButton("−")
        self.minus_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.minus_btn.setFixedWidth(40)
        self.minus_btn.clicked.connect(self.decrease)

        self.value_label = QLabel()
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.value_label.setMinimumWidth(68)

        self.plus_btn = QPushButton("+")
        self.plus_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.plus_btn.setFixedWidth(40)
        self.plus_btn.clicked.connect(self.increase)

        layout.addWidget(self.minus_btn)
        layout.addWidget(self.value_label, 1)
        layout.addWidget(self.plus_btn)
        self.setValue(value)

    def setRange(self, minimum: int, maximum: int):
        self._minimum = minimum
        self._maximum = maximum
        self.setValue(self._value)

    def setValue(self, value: int):
        self._value = max(self._minimum, min(self._maximum, int(value)))
        self.value_label.setText(str(self._value))
        self.minus_btn.setEnabled(self._value > self._minimum)
        self.plus_btn.setEnabled(self._value < self._maximum)

    def value(self) -> int:
        return self._value

    def increase(self):
        self.setValue(self._value + 1)

    def decrease(self):
        self.setValue(self._value - 1)


class SubtitlePreviewLabel(QLabel):
    """用 QPainter 绘制带描边的字幕预览，并按预览画布宽度自动换行。"""
    def __init__(self, text: str = "Your final captions will automatically wrap\ninside this 9:16 video preview canvas."):
        super().__init__(text)
        self.font_color = QColor(DEFAULT_SUBTITLE_COLOR)
        self.outline_color = QColor(DEFAULT_SUBTITLE_OUTLINE_COLOR)
        self.final_font_size = DEFAULT_SUBTITLE_SIZE
        self.final_outline_width = DEFAULT_SUBTITLE_OUTLINE_WIDTH
        self.setWordWrap(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 这里必须保持透明，黑色底色由父级 9:16 画布负责。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent; border: none;")

    def _preview_scale(self) -> float:
        # 最终字幕是在 1080x1920 的 ASS 画布上渲染的。
        # 预览文字区域对应最终字幕安全宽度：1080 - 左右边距。
        # 用真实宽度比例缩放，避免预览和成片字号差异过大。
        final_text_width = max(1, OUTPUT_WIDTH - SUBTITLE_SAFE_MARGIN_L - SUBTITLE_SAFE_MARGIN_R)
        current_width = max(1, self.width() or int(final_text_width * 0.24))
        return current_width / final_text_width

    def _preview_font_size(self) -> int:
        # 预览框很小，完全等比缩放时文字可能过小。这里保留等比关系，但给一个可见下限。
        return max(12, min(38, int(round(self.final_font_size * self._preview_scale()))))

    def _font(self) -> QFont:
        return QFont(SUBTITLE_FONT_NAME, self._preview_font_size(), QFont.Weight.Black)

    def set_subtitle_style(self, font_color: str, outline_color: str, font_size: int, outline_width: int = DEFAULT_SUBTITLE_OUTLINE_WIDTH):
        self.font_color = QColor(font_color or DEFAULT_SUBTITLE_COLOR)
        self.outline_color = QColor(outline_color or DEFAULT_SUBTITLE_OUTLINE_COLOR)
        self.final_font_size = max(14, min(120, int(font_size or DEFAULT_SUBTITLE_SIZE)))
        self.final_outline_width = max(0, min(12, int(outline_width if outline_width is not None else DEFAULT_SUBTITLE_OUTLINE_WIDTH)))
        self.updateGeometry()
        self.update()

    def _wrap_paragraph(self, paragraph: str, metrics: QFontMetrics, max_width: int) -> List[str]:
        paragraph = paragraph.strip()
        if not paragraph:
            return []

        words = paragraph.split()
        if not words:
            return []

        lines: List[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if metrics.horizontalAdvance(candidate) <= max_width:
                current = candidate
                continue

            if current:
                lines.append(current)
                current = word
            else:
                # 极端情况：一个超长单词也不能超出屏幕，按字符拆分。
                chunk = ""
                for ch in word:
                    test = chunk + ch
                    if metrics.horizontalAdvance(test) <= max_width or not chunk:
                        chunk = test
                    else:
                        lines.append(chunk)
                        chunk = ch
                current = chunk

        if current:
            lines.append(current)
        return lines

    def wrapped_lines(self, available_width: int) -> List[str]:
        font = self._font()
        metrics = QFontMetrics(font)
        max_width = max(40, available_width - 2)
        lines: List[str] = []
        for paragraph in self.text().split("\n"):
            wrapped = self._wrap_paragraph(paragraph, metrics, max_width)
            lines.extend(wrapped)
        return lines

    def required_height(self, available_width: int) -> int:
        font = self._font()
        metrics = QFontMetrics(font)
        lines = self.wrapped_lines(available_width)
        if not lines:
            return 0
        return max(44, metrics.height() * len(lines) + 14)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        font = self._font()
        painter.setFont(font)
        metrics = QFontMetrics(font)
        lines = self.wrapped_lines(self.width())
        if not lines:
            return

        line_height = metrics.height()
        total_height = line_height * len(lines)
        y = (self.height() - total_height) / 2 + metrics.ascent()
        outline_width = 0 if self.final_outline_width <= 0 else max(1, int(round(self.final_outline_width * self._preview_scale())))

        for line in lines:
            width = metrics.horizontalAdvance(line)
            x = (self.width() - width) / 2
            path = QPainterPath()
            path.addText(x, y, font, line)
            if outline_width > 0:
                painter.setPen(QPen(self.outline_color, outline_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.setBrush(QBrush(self.font_color))
                painter.drawPath(path)
            painter.fillPath(path, QBrush(self.font_color))
            y += line_height


class PhoneSubtitlePreview(Card):
    """中间 9:16 黑色画布字幕预览，只展示字幕样式，不参与实际渲染。"""
    SCREEN_WIDTH = 252
    SCREEN_HEIGHT = 448  # 252:448 = 9:16

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(520)
        self._last_enabled = False
        self._last_color = DEFAULT_SUBTITLE_COLOR
        self._last_outline_color = DEFAULT_SUBTITLE_OUTLINE_COLOR
        self._last_outline_width = DEFAULT_SUBTITLE_OUTLINE_WIDTH
        self._last_font_size = DEFAULT_SUBTITLE_SIZE
        self._last_margin_v = DEFAULT_SUBTITLE_MARGIN_V

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 15, 18, 18)
        outer.setSpacing(12)

        title = QLabel("字幕预览")
        title.setStyleSheet("color:#0F172A; font-size:16px; font-weight:800; border:none; background:transparent;")
        desc = QLabel("中间黑色画布按最终 1080×1920 成片坐标等比预览，用来判断字幕颜色、描边、字号和上下位置。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#64748B; font-size:12px; border:none; background:transparent;")
        outer.addWidget(title)
        outer.addWidget(desc)

        preview_wrap = QWidget()
        preview_wrap.setStyleSheet("background:transparent; border:none;")
        preview_layout = QHBoxLayout(preview_wrap)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addStretch()

        # 外层手机壳只做装饰，真正可见的 9:16 边界是里面的黑色 screen。
        self.phone = QFrame()
        self.phone.setObjectName("PhoneFrame")
        self.phone.setFixedSize(286, 496)
        self.phone.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.phone.setStyleSheet("""
            QFrame#PhoneFrame {
                background: #020617;
                border: 1px solid #111827;
                border-radius: 36px;
            }
        """)
        phone_inner = QVBoxLayout(self.phone)
        phone_inner.setContentsMargins(17, 24, 17, 24)
        phone_inner.setSpacing(0)

        self.screen = QFrame()
        self.screen.setObjectName("PhoneScreen")
        self.screen.setFixedSize(self.SCREEN_WIDTH, self.SCREEN_HEIGHT)
        self.screen.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.screen.setAutoFillBackground(True)
        self.screen.setStyleSheet("""
            QFrame#PhoneScreen {
                background-color: #000000;
                border: 2px solid #1E293B;
                border-radius: 0px;
            }
        """)

        self.subtitle_label = SubtitlePreviewLabel()
        self.subtitle_label.setParent(self.screen)
        self.subtitle_label.raise_()

        phone_inner.addWidget(self.screen, alignment=Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.phone)
        preview_layout.addStretch()
        outer.addWidget(preview_wrap, 1)

        self.update_subtitle(False, DEFAULT_SUBTITLE_COLOR, DEFAULT_SUBTITLE_OUTLINE_COLOR, DEFAULT_SUBTITLE_OUTLINE_WIDTH, DEFAULT_SUBTITLE_SIZE, DEFAULT_SUBTITLE_MARGIN_V)

    def _position_subtitle_label(self, margin_v: int, centered: bool = False):
        screen_w = self.SCREEN_WIDTH
        screen_h = self.SCREEN_HEIGHT

        # ASS 成片里左右安全边距是 70 / 1080。这里按同样比例映射到预览画布，
        # 这样自动换行宽度、字幕横向范围会更接近最终视频。
        scale_x = screen_w / OUTPUT_WIDTH
        scale_y = screen_h / OUTPUT_HEIGHT
        left_margin = int(round(SUBTITLE_SAFE_MARGIN_L * scale_x))
        right_margin = int(round(SUBTITLE_SAFE_MARGIN_R * scale_x))
        label_w = max(80, screen_w - left_margin - right_margin)

        self.subtitle_label.setFixedWidth(label_w)
        label_h = self.subtitle_label.required_height(label_w)
        label_h = max(24, min(screen_h - 16, label_h))
        self.subtitle_label.setFixedHeight(label_h)

        x = int(round(left_margin))
        if centered:
            y = int((screen_h - label_h) / 2)
        else:
            margin_v = max(SUBTITLE_MARGIN_MIN, min(SUBTITLE_MARGIN_MAX, int(margin_v or DEFAULT_SUBTITLE_MARGIN_V)))
            # ASS Alignment=2 时 MarginV 是“距离底部”的真实 1080x1920 坐标。
            # 这里不再用百分比重映射，而是直接按 448/1920 等比换算。
            bottom_margin = int(round(margin_v * scale_y))
            y = screen_h - bottom_margin - label_h
            y = max(8, min(screen_h - label_h - 8, y))

        self.subtitle_label.setGeometry(x, y, label_w, label_h)
        self.subtitle_label.raise_()
        self.subtitle_label.setVisible(True)
        self.subtitle_label.update()
        self.screen.update()

    def update_subtitle(self, enabled: bool, color: str, outline_color: str, outline_width: int, font_size: int, margin_v: int):
        self._last_enabled = enabled
        self._last_color = color
        self._last_outline_color = outline_color
        self._last_outline_width = max(0, min(12, int(outline_width if outline_width is not None else DEFAULT_SUBTITLE_OUTLINE_WIDTH)))
        self._last_font_size = max(14, min(120, int(font_size or DEFAULT_SUBTITLE_SIZE)))
        self._last_margin_v = max(SUBTITLE_MARGIN_MIN, min(SUBTITLE_MARGIN_MAX, int(margin_v or DEFAULT_SUBTITLE_MARGIN_V)))

        if not enabled:
            self.subtitle_label.setText("Captions are currently turned off. Enable subtitles to preview how text will look on the final 9:16 video.")
            self.subtitle_label.set_subtitle_style("#64748B", "#000000", 34, DEFAULT_SUBTITLE_OUTLINE_WIDTH)
            self._position_subtitle_label(self._last_margin_v, centered=True)
            return

        self.subtitle_label.setText("Your final captions will automatically wrap inside this 9:16 video preview canvas.")
        self.subtitle_label.set_subtitle_style(color, outline_color, self._last_font_size, self._last_outline_width)
        self._position_subtitle_label(self._last_margin_v)


# -----------------------------
# Main Window
# -----------------------------

class RandomMixEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("矩阵编导 v2.0")
        self.setMinimumSize(1500, 840)
        self.video_folders: List[str] = []
        self.selected_index: Optional[int] = None
        self.worker: Optional[MixWorker] = None

        self.ffmpeg_path = find_executable("ffmpeg.exe")
        self.ffprobe_path = find_executable("ffprobe.exe")
        self.gpu_info = detect_gpu_acceleration(self.ffmpeg_path)
        self.gpu_checkbox: Optional[QCheckBox] = None
        self.subtitle_checkbox: Optional[QCheckBox] = None
        self.subtitle_color = DEFAULT_SUBTITLE_COLOR
        self.subtitle_outline_color = DEFAULT_SUBTITLE_OUTLINE_COLOR
        self.subtitle_outline_width = DEFAULT_SUBTITLE_OUTLINE_WIDTH
        self.subtitle_size = DEFAULT_SUBTITLE_SIZE
        self.subtitle_position = DEFAULT_SUBTITLE_MARGIN_V
        self.subtitle_color_button: Optional[QPushButton] = None
        self.subtitle_outline_color_button: Optional[QPushButton] = None
        self.subtitle_outline_width_slider: Optional[QSlider] = None
        self.subtitle_outline_width_label: Optional[QLabel] = None
        self.subtitle_size_slider: Optional[QSlider] = None
        self.subtitle_size_label: Optional[QLabel] = None
        self.subtitle_position_slider: Optional[QSlider] = None
        self.subtitle_position_label: Optional[QLabel] = None
        self.phone_preview: Optional[PhoneSubtitlePreview] = None

        self.init_ui()

    def init_ui(self):
        root = QWidget()
        root.setObjectName("root")
        root.setStyleSheet("""
            QWidget#root { background: #EEF2F7; }
            QLabel { border: none; background: transparent; }
            QTextEdit {
                background: #0B1220;
                color: #D1D5DB;
                border: none;
                border-radius: 16px;
                padding: 12px;
                font-family: Consolas, 'Microsoft YaHei UI';
                font-size: 12px;
            }
            QSpinBox {
                background: #F8FAFC;
                border: 1px solid #E2E8F0;
                border-radius: 12px;
                padding: 9px 12px;
                color: #0F172A;
                font-size: 14px;
            }
            QProgressBar {
                background: #E2E8F0;
                border: none;
                border-radius: 8px;
                height: 12px;
                text-align: center;
                color: transparent;
            }
            QProgressBar::chunk {
                background: #111827;
                border-radius: 8px;
            }
            QScrollArea {
                background: #F8FAFC;
                border: 1px solid #E2E8F0;
                border-radius: 16px;
            }
            QScrollArea > QWidget > QWidget {
                background: #F8FAFC;
                border: none;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 8px 2px 8px 0px;
            }
            QScrollBar::handle:vertical {
                background: #CBD5E1;
                border-radius: 5px;
                min-height: 32px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        self.setCentralWidget(root)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(16)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("矩阵混剪台")
        title.setStyleSheet("color:#0F172A; font-size:28px; font-weight:800; border:none; background:transparent;")
        subtitle = QLabel("按你导入的视频文件夹顺序随机抽片段，用随机 MP3 的时长自动生成混剪成片。")
        subtitle.setStyleSheet("color:#64748B; font-size:13px; border:none; background:transparent;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.start_btn = ModernButton("开始生成", "primary")
        self.start_btn.setFixedWidth(160)
        self.start_btn.clicked.connect(self.start_mix)
        header.addWidget(self.start_btn)
        outer.addLayout(header)

        # Body
        body = QHBoxLayout()
        body.setSpacing(18)
        outer.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(14)
        body.addLayout(left, 7)

        center = QVBoxLayout()
        center.setSpacing(14)
        center.setContentsMargins(0, 0, 0, 0)
        body.addLayout(center, 4)

        right = QVBoxLayout()
        right.setSpacing(14)
        right.setContentsMargins(0, 0, 0, 0)
        body.addLayout(right, 3)

        # Path pickers：左右并排，给视频顺序区域让出更多高度。
        picker_row = QHBoxLayout()
        picker_row.setSpacing(14)

        self.audio_picker = PathPicker("音频文件夹", "每次随机选择 1 个 MP3，成片时长以它为准", "选择音频")
        self.audio_picker.button.clicked.connect(self.select_audio_folder)
        picker_row.addWidget(self.audio_picker, 1)

        self.output_picker = PathPicker("输出文件夹", "生成的混剪视频会保存到这里", "选择输出")
        self.output_picker.button.clicked.connect(self.select_output_folder)
        picker_row.addWidget(self.output_picker, 1)

        left.addLayout(picker_row)

        # Video order card
        order_card = Card()
        order_layout = QVBoxLayout(order_card)
        order_layout.setContentsMargins(18, 16, 18, 18)
        order_layout.setSpacing(10)

        order_header = QHBoxLayout()
        order_title_box = QVBoxLayout()
        order_title_box.setSpacing(4)
        order_title = QLabel("视频文件夹顺序")
        order_title.setStyleSheet("color:#0F172A; font-size:16px; font-weight:800; border:none; background:transparent;")
        order_desc = QLabel("按从左到右、从上到下的顺序随机抽取。重复导入同一个文件夹会作为独立步骤。")
        order_desc.setStyleSheet("color:#64748B; font-size:12px; border:none; background:transparent;")
        order_title_box.addWidget(order_title)
        order_title_box.addWidget(order_desc)
        order_header.addLayout(order_title_box)
        order_header.addStretch()
        add_btn = ModernButton("+ 导入视频文件夹", "primary")
        add_btn.setFixedWidth(160)
        add_btn.clicked.connect(self.add_video_folder)
        order_header.addWidget(add_btn)
        order_layout.addLayout(order_header)

        # 操作按钮放在网格上方，避免和滚动区域里的素材卡片互相挤压/遮挡。
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.up_btn = ModernButton("上移", "soft")
        self.down_btn = ModernButton("下移", "soft")
        self.remove_btn = ModernButton("删除选中", "danger")
        self.clear_btn = ModernButton("清空", "soft")
        self.up_btn.clicked.connect(self.move_selected_up)
        self.down_btn.clicked.connect(self.move_selected_down)
        self.remove_btn.clicked.connect(self.remove_selected_folder)
        self.clear_btn.clicked.connect(self.clear_folders)
        for b in [self.up_btn, self.down_btn, self.remove_btn, self.clear_btn]:
            b.setMinimumHeight(36)
            tools.addWidget(b)
        tools.addStretch()
        order_layout.addLayout(tools)

        self.folder_scroll = QScrollArea()
        self.folder_scroll.setWidgetResizable(True)
        self.folder_scroll.setMinimumHeight(230)
        self.folder_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.folder_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.folder_container = QWidget()
        self.folder_container.setObjectName("folderContainer")
        self.folder_container.setStyleSheet("QWidget#folderContainer { background:#F8FAFC; border:none; }")
        self.folder_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        self.folder_grid = QGridLayout(self.folder_container)
        self.folder_grid.setContentsMargins(10, 10, 10, 10)
        self.folder_grid.setHorizontalSpacing(10)
        self.folder_grid.setVerticalSpacing(10)
        self.folder_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        for col in range(5):
            self.folder_grid.setColumnStretch(col, 1)
        self.folder_scroll.setWidget(self.folder_container)
        order_layout.addWidget(self.folder_scroll, 1)

        left.addWidget(order_card, 1)

        # Settings card
        setting_card = Card()
        setting_layout = QGridLayout(setting_card)
        setting_layout.setContentsMargins(18, 13, 18, 14)
        setting_layout.setHorizontalSpacing(12)
        setting_layout.setVerticalSpacing(6)
        setting_title = QLabel("生成设置")
        setting_title.setStyleSheet("color:#0F172A; font-size:16px; font-weight:800; border:none; background:transparent;")
        setting_desc = QLabel("默认输出 1080×1920 / 30fps / MP4。")
        setting_desc.setStyleSheet("color:#64748B; font-size:12px; border:none; background:transparent;")
        # 使用自定义数量选择器，避免部分 Windows 缩放环境下 QSpinBox 数字不可见。
        self.count_spin = CountStepper(1, 999, 1)
        count_label = QLabel("生成数量")
        count_label.setStyleSheet("color:#334155; font-size:13px; font-weight:700; border:none; background:transparent;")

        setting_layout.addWidget(setting_title, 0, 0, 1, 2)
        setting_layout.addWidget(setting_desc, 1, 0, 1, 2)

        count_row = QHBoxLayout()
        count_row.setContentsMargins(0, 6, 0, 0)
        count_row.setSpacing(14)
        count_row.addWidget(count_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        count_row.addStretch()

        if self.gpu_info.available:
            self.gpu_checkbox = QCheckBox(f"使用 GPU 加速（{self.gpu_info.vendor} · {self.gpu_info.encoder}）")
            self.gpu_checkbox.setChecked(True)
            self.gpu_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
            self.gpu_checkbox.setToolTip(self.gpu_info.gpu_name)
            # 使用系统原生复选框，不再用黑/白块模拟选中状态，勾选状态更直观。
            self.gpu_checkbox.setStyleSheet("""
                QCheckBox {
                    color: #334155;
                    font-size: 12px;
                    font-weight: 700;
                    border: none;
                    background: transparent;
                    spacing: 7px;
                }
                QCheckBox:hover {
                    color: #0F172A;
                }
            """)
            count_row.addWidget(self.gpu_checkbox, alignment=Qt.AlignmentFlag.AlignVCenter)

        count_row.addWidget(self.count_spin, alignment=Qt.AlignmentFlag.AlignVCenter)
        setting_layout.addLayout(count_row, 2, 0, 1, 2)
        setting_card.setMaximumHeight(114)

        left.addWidget(setting_card)

        # Center subtitle preview + subtitle controls
        self.phone_preview = PhoneSubtitlePreview()
        center.addWidget(self.phone_preview, 1)
        subtitle_card = self.build_subtitle_settings_card()
        center.addWidget(subtitle_card)
        center.addStretch(1)

        # Right preview card - keep original compact layout
        preview_card = Card()
        preview_card.setFixedHeight(170)
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(18, 15, 18, 15)
        preview_layout.setSpacing(10)
        preview_title = QLabel("成片逻辑预览")
        preview_title.setStyleSheet("color:#0F172A; font-size:16px; font-weight:800; border:none; background:transparent;")
        preview_desc = QLabel("随机 MP3 定时长，视频按左侧顺序抽取，最后自动裁切合成。")
        preview_desc.setWordWrap(True)
        preview_desc.setStyleSheet("color:#64748B; font-size:12px; border:none; background:transparent;")
        preview_layout.addWidget(preview_title)
        preview_layout.addWidget(preview_desc)
        preview_layout.addWidget(self.build_preview_panel())
        right.addWidget(preview_card)

        # Log card - compact
        log_card = Card()
        log_card.setFixedHeight(230)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(18, 15, 18, 16)
        log_layout.setSpacing(10)
        log_head = QHBoxLayout()
        log_title = QLabel("运行日志")
        log_title.setStyleSheet("color:#0F172A; font-size:16px; font-weight:800; border:none; background:transparent;")
        log_head.addWidget(log_title)
        log_head.addStretch()
        clear_log_btn = ModernButton("清空日志", "ghost")
        clear_log_btn.clicked.connect(lambda: self.log_box.clear())
        log_head.addWidget(clear_log_btn)
        log_layout.addLayout(log_head)
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        log_layout.addWidget(self.log_box, 1)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        log_layout.addWidget(self.progress)
        right.addWidget(log_card)
        right.addStretch(1)

        self.append_log("准备就绪。请先选择音频文件夹、输出文件夹，然后按顺序导入视频文件夹。")
        if self.gpu_info.available:
            self.append_log(f"已检测到可用 GPU 加速：{self.gpu_info.vendor} / {self.gpu_info.encoder}。已默认开启。")
        self.refresh_folder_list()

    def build_subtitle_settings_card(self) -> Card:
        card = Card()
        card.setMaximumHeight(270)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 16)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("字幕设置")
        title.setStyleSheet("color:#0F172A; font-size:16px; font-weight:800; border:none; background:transparent;")
        desc = QLabel("可选。优先读取同名 SRT；没有时自动调用 Whispy / Whisper 识别并保存。")
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#64748B; font-size:12px; border:none; background:transparent;")
        title_box.addWidget(title)
        title_box.addWidget(desc)
        head.addLayout(title_box, 1)

        self.subtitle_checkbox = QCheckBox("启用字幕")
        self.subtitle_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.subtitle_checkbox.setStyleSheet("""
            QCheckBox {
                color: #334155;
                font-size: 13px;
                font-weight: 800;
                border: none;
                background: transparent;
                spacing: 8px;
            }
            QCheckBox:hover { color:#0F172A; }
        """)
        self.subtitle_checkbox.stateChanged.connect(self.update_subtitle_controls)
        head.addWidget(self.subtitle_checkbox, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        color_grid = QGridLayout()
        color_grid.setHorizontalSpacing(12)
        color_grid.setVerticalSpacing(6)
        font_color_label = QLabel("字体颜色")
        font_color_label.setStyleSheet("color:#334155; font-size:12px; font-weight:700; border:none; background:transparent;")
        outline_color_label = QLabel("字体边框颜色")
        outline_color_label.setStyleSheet("color:#334155; font-size:12px; font-weight:700; border:none; background:transparent;")

        self.subtitle_color_button = QPushButton(self.subtitle_color)
        self.subtitle_color_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.subtitle_color_button.setMinimumHeight(38)
        self.subtitle_color_button.clicked.connect(self.choose_subtitle_color)

        self.subtitle_outline_color_button = QPushButton(self.subtitle_outline_color)
        self.subtitle_outline_color_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.subtitle_outline_color_button.setMinimumHeight(38)
        self.subtitle_outline_color_button.clicked.connect(self.choose_subtitle_outline_color)

        color_grid.addWidget(font_color_label, 0, 0)
        color_grid.addWidget(outline_color_label, 0, 1)
        color_grid.addWidget(self.subtitle_color_button, 1, 0)
        color_grid.addWidget(self.subtitle_outline_color_button, 1, 1)
        color_grid.setColumnStretch(0, 1)
        color_grid.setColumnStretch(1, 1)
        layout.addLayout(color_grid)

        self.subtitle_outline_width_slider = self.create_subtitle_slider(0, 12, self.subtitle_outline_width)
        self.subtitle_outline_width_slider.valueChanged.connect(self.on_subtitle_outline_width_changed)
        self.subtitle_outline_width_label = self.create_value_label(str(self.subtitle_outline_width))
        layout.addLayout(self.make_slider_row("边框粗细", self.subtitle_outline_width_slider, self.subtitle_outline_width_label))

        self.subtitle_size_slider = self.create_subtitle_slider(18, 76, self.subtitle_size)
        self.subtitle_size_slider.valueChanged.connect(self.on_subtitle_size_changed)
        self.subtitle_size_label = self.create_value_label(str(self.subtitle_size))
        layout.addLayout(self.make_slider_row("字体大小", self.subtitle_size_slider, self.subtitle_size_label))

        self.subtitle_position_slider = self.create_subtitle_slider(SUBTITLE_MARGIN_MIN, SUBTITLE_MARGIN_MAX, self.subtitle_position)
        self.subtitle_position_slider.valueChanged.connect(self.on_subtitle_position_changed)
        self.subtitle_position_label = self.create_value_label(self.format_subtitle_position(self.subtitle_position))
        layout.addLayout(self.make_slider_row("字体位置", self.subtitle_position_slider, self.subtitle_position_label))

        self.refresh_subtitle_color_buttons()
        self.update_subtitle_controls()
        return card

    def create_subtitle_slider(self, minimum: int, maximum: int, value: int) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.setCursor(Qt.CursorShape.PointingHandCursor)
        slider.setStyleSheet("""
            QSlider { border:none; background:transparent; }
            QSlider::groove:horizontal {
                height: 6px;
                background: #E2E8F0;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                width: 18px;
                height: 18px;
                margin: -7px 0;
                background: #111827;
                border-radius: 9px;
            }
            QSlider::sub-page:horizontal {
                background: #111827;
                border-radius: 3px;
            }
        """)
        return slider

    def create_value_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFixedWidth(66)
        label.setStyleSheet("color:#0F172A; font-size:12px; font-weight:800; border:none; background:transparent;")
        return label

    def make_slider_row(self, label_text: str, slider: QSlider, value_label: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        label = QLabel(label_text)
        label.setFixedWidth(112)
        label.setStyleSheet("color:#334155; font-size:12px; font-weight:700; border:none; background:transparent;")
        row.addWidget(label)
        row.addWidget(slider, 1)
        row.addWidget(value_label)
        return row

    def refresh_subtitle_color_buttons(self):
        def style_button(btn: Optional[QPushButton], color: str):
            if not btn:
                return
            text_color = '#0F172A' if color.upper() in ['#FFFFFF', '#FFFF00', '#00FFFF'] else '#FFFFFF'
            btn.setText(color.upper())
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {color};
                    color: {text_color};
                    border: 1px solid #CBD5E1;
                    border-radius: 12px;
                    font-size: 12px;
                    font-weight: 800;
                }}
                QPushButton:hover {{ border: 2px solid #111827; }}
                QPushButton:disabled {{ background:#E2E8F0; color:#94A3B8; }}
            """)
        style_button(self.subtitle_color_button, self.subtitle_color)
        style_button(self.subtitle_outline_color_button, self.subtitle_outline_color)

    def choose_subtitle_color(self):
        color = QColorDialog.getColor(QColor(self.subtitle_color), self, "选择字幕颜色")
        if color.isValid():
            self.subtitle_color = color.name().upper()
            self.refresh_subtitle_color_buttons()
            self.update_subtitle_preview()

    def choose_subtitle_outline_color(self):
        color = QColorDialog.getColor(QColor(self.subtitle_outline_color), self, "选择字体边框颜色")
        if color.isValid():
            self.subtitle_outline_color = color.name().upper()
            self.refresh_subtitle_color_buttons()
            self.update_subtitle_preview()

    def on_subtitle_outline_width_changed(self, value: int):
        self.subtitle_outline_width = int(value)
        if self.subtitle_outline_width_label:
            self.subtitle_outline_width_label.setText(str(value))
        self.update_subtitle_preview()

    def on_subtitle_size_changed(self, value: int):
        self.subtitle_size = int(value)
        if self.subtitle_size_label:
            self.subtitle_size_label.setText(str(value))
        self.update_subtitle_preview()

    def format_subtitle_position(self, value: int) -> str:
        value = max(SUBTITLE_MARGIN_MIN, min(SUBTITLE_MARGIN_MAX, int(value)))
        percent = int(round((value - SUBTITLE_MARGIN_MIN) / (SUBTITLE_MARGIN_MAX - SUBTITLE_MARGIN_MIN) * 100))
        return f"{percent}%"

    def on_subtitle_position_changed(self, value: int):
        self.subtitle_position = int(value)
        if self.subtitle_position_label:
            self.subtitle_position_label.setText(self.format_subtitle_position(value))
        self.update_subtitle_preview()

    def update_subtitle_controls(self):
        enabled = bool(self.subtitle_checkbox and self.subtitle_checkbox.isChecked())
        for widget in [
            self.subtitle_color_button,
            self.subtitle_outline_color_button,
            self.subtitle_outline_width_slider,
            self.subtitle_outline_width_label,
            self.subtitle_size_slider,
            self.subtitle_size_label,
            self.subtitle_position_slider,
            self.subtitle_position_label,
        ]:
            if widget:
                widget.setEnabled(enabled)
        self.update_subtitle_preview()

    def update_subtitle_preview(self):
        enabled = bool(self.subtitle_checkbox and self.subtitle_checkbox.isChecked())
        if self.phone_preview:
            self.phone_preview.update_subtitle(
                enabled,
                self.subtitle_color,
                self.subtitle_outline_color,
                self.subtitle_outline_width,
                self.subtitle_size,
                self.subtitle_position,
            )

    def build_preview_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background:transparent; border:none;")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        def block(text: str, sub: str, dark: bool = False) -> QLabel:
            lb = QLabel(f"<b>{text}</b><br><span style='font-size:10px'>{sub}</span>")
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lb.setMinimumHeight(60)
            if dark:
                lb.setStyleSheet("""
                    QLabel {
                        background: #111827;
                        color: #FFFFFF;
                        border: none;
                        border-radius: 15px;
                        padding: 10px;
                    }
                """)
            else:
                lb.setStyleSheet("""
                    QLabel {
                        background: #F8FAFC;
                        color: #334155;
                        border: 1px solid #E2E8F0;
                        border-radius: 15px;
                        padding: 10px;
                    }
                """)
            return lb

        arrow1 = QLabel("→")
        arrow1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow1.setStyleSheet("color:#94A3B8; font-size:16px; font-weight:800; border:none; background:transparent;")
        arrow2 = QLabel("→")
        arrow2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        arrow2.setStyleSheet("color:#94A3B8; font-size:16px; font-weight:800; border:none; background:transparent;")

        layout.addWidget(block("MP3", "定时长", True), 1)
        layout.addWidget(arrow1)
        layout.addWidget(block("视频顺序", "左→右"), 1)
        layout.addWidget(arrow2)
        layout.addWidget(block("MP4", "输出", True), 1)
        return panel

    # -------- UI Actions --------

    def select_audio_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择音频文件夹")
        if folder:
            self.audio_picker.set_path(folder)
            count = len(list_files(folder, AUDIO_EXTS))
            self.append_log(f"已选择音频文件夹：{folder}（找到 {count} 个 mp3）")

    def select_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择输出文件夹")
        if folder:
            self.output_picker.set_path(folder)
            self.append_log(f"已选择输出文件夹：{folder}")

    def add_video_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "导入视频文件夹")
        if folder:
            self.video_folders.append(folder)
            self.selected_index = len(self.video_folders) - 1
            count = len(list_files(folder, VIDEO_EXTS))
            self.append_log(f"已导入视频文件夹：{folder}（找到 {count} 个视频）")
            self.refresh_folder_list()

    def rebuild_folder_grid(self):
        """每次刷新都重建滚动区域内部容器，彻底清理旧行高/旧 spacer/待删除控件。"""
        # 用 QScrollArea.takeWidget() 安全取出旧容器，再延迟删除，避免 setParent(None)
        # 与 QScrollArea 持有关系冲突导致偶发崩溃。
        old_container = self.folder_scroll.takeWidget() if hasattr(self, "folder_scroll") else None
        if old_container is not None:
            old_container.deleteLater()

        self.folder_container = QWidget()
        self.folder_container.setObjectName("folderContainer")
        self.folder_container.setStyleSheet("QWidget#folderContainer { background:#F8FAFC; border:none; }")
        self.folder_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.folder_grid = QGridLayout(self.folder_container)
        self.folder_grid.setContentsMargins(10, 10, 10, 10)
        self.folder_grid.setHorizontalSpacing(10)
        self.folder_grid.setVerticalSpacing(10)
        self.folder_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for col in range(5):
            self.folder_grid.setColumnStretch(col, 1)

        self.folder_scroll.setWidget(self.folder_container)

    def refresh_folder_list(self):
        self.rebuild_folder_grid()

        if not self.video_folders:
            empty = QLabel("还没有导入视频文件夹\n点击右上角按钮开始添加。")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("color:#94A3B8; font-size:13px; border:none; background:transparent;")
            empty.setMinimumHeight(180)
            self.folder_grid.addWidget(empty, 0, 0, 1, 5)
            self.selected_index = None
            self.folder_container.setMinimumHeight(220)
            return

        if self.selected_index is None:
            self.selected_index = 0
        elif self.selected_index >= len(self.video_folders):
            self.selected_index = len(self.video_folders) - 1
        elif self.selected_index < 0:
            self.selected_index = 0

        item_height = 86
        row_gap = 10
        row_count = (len(self.video_folders) + 4) // 5

        for idx, folder in enumerate(self.video_folders):
            row = idx // 5
            col = idx % 5
            widget = FolderStepItem(idx + 1, folder, selected=(idx == self.selected_index))
            widget.clicked.connect(self.set_selected_index)
            self.folder_grid.addWidget(widget, row, col, alignment=Qt.AlignmentFlag.AlignTop)

        # 给最后一行补透明占位，保证永远是一行 5 个，不会因为删除后被拉伸错位。
        total = len(self.video_folders)
        remainder = total % 5
        if remainder:
            row = total // 5
            for col in range(remainder, 5):
                spacer = QWidget()
                spacer.setStyleSheet("background:transparent; border:none;")
                spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                spacer.setFixedHeight(item_height)
                self.folder_grid.addWidget(spacer, row, col, alignment=Qt.AlignmentFlag.AlignTop)

        for row in range(row_count):
            self.folder_grid.setRowMinimumHeight(row, item_height)
            self.folder_grid.setRowStretch(row, 0)

        # 内容高度只由当前真实行数决定。删除多个步骤后不会残留旧行高，卡片不会错位。
        content_height = 20 + row_count * item_height + max(0, row_count - 1) * row_gap
        self.folder_container.setMinimumHeight(max(220, content_height))
        self.folder_container.adjustSize()
        self.folder_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

    def set_selected_index(self, index: int):
        if 0 <= index < len(self.video_folders):
            self.selected_index = index
            # 不要在 FolderStepItem 的 mousePressEvent 执行栈里立即重建/删除卡片，
            # 否则点击已导入文件夹卡片时可能触发 C++ 对象被提前销毁而闪退。
            QTimer.singleShot(0, self.refresh_folder_list)

    def selected_row(self) -> int:
        if self.selected_index is None:
            return -1
        return self.selected_index

    def move_selected_up(self):
        row = self.selected_row()
        if row > 0:
            self.video_folders[row - 1], self.video_folders[row] = self.video_folders[row], self.video_folders[row - 1]
            self.selected_index = row - 1
            self.refresh_folder_list()

    def move_selected_down(self):
        row = self.selected_row()
        if 0 <= row < len(self.video_folders) - 1:
            self.video_folders[row + 1], self.video_folders[row] = self.video_folders[row], self.video_folders[row + 1]
            self.selected_index = row + 1
            self.refresh_folder_list()

    def remove_selected_folder(self):
        row = self.selected_row()
        if 0 <= row < len(self.video_folders):
            removed = self.video_folders.pop(row)
            self.append_log(f"已删除步骤：{removed}")
            if self.video_folders:
                self.selected_index = min(row, len(self.video_folders) - 1)
            else:
                self.selected_index = None
            self.refresh_folder_list()

    def clear_folders(self):
        if not self.video_folders:
            return
        self.video_folders.clear()
        self.selected_index = None
        self.refresh_folder_list()
        self.append_log("已清空视频文件夹顺序。")

    def validate_form(self) -> Optional[MixJobConfig]:
        if not self.audio_picker.path:
            QMessageBox.warning(self, "缺少音频文件夹", "请先选择音频文件夹。")
            return None
        if not self.output_picker.path:
            QMessageBox.warning(self, "缺少输出文件夹", "请先选择输出文件夹。")
            return None
        if not self.video_folders:
            QMessageBox.warning(self, "缺少视频文件夹", "请至少导入一个视频文件夹。")
            return None

        use_gpu = bool(self.gpu_info.available and self.gpu_checkbox and self.gpu_checkbox.isChecked())
        subtitle_enabled = bool(self.subtitle_checkbox and self.subtitle_checkbox.isChecked())
        subtitle_font_path = resource_path("impact.ttf")

        return MixJobConfig(
            audio_folder=self.audio_picker.path,
            video_folders=list(self.video_folders),
            output_folder=self.output_picker.path,
            output_count=self.count_spin.value(),
            ffmpeg_path=self.ffmpeg_path,
            ffprobe_path=self.ffprobe_path,
            gpu_enabled=use_gpu,
            gpu_encoder=self.gpu_info.encoder if use_gpu else "",
            gpu_vendor=self.gpu_info.vendor if use_gpu else "",
            subtitle_enabled=subtitle_enabled,
            subtitle_font_color=self.subtitle_color,
            subtitle_outline_color=self.subtitle_outline_color,
            subtitle_outline_width=self.subtitle_outline_width,
            subtitle_font_size=self.subtitle_size,
            subtitle_margin_v=self.subtitle_position,
            subtitle_font_path=subtitle_font_path,
        )

    def start_mix(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "正在运行", "当前任务还在运行中。")
            return

        config = self.validate_form()
        if not config:
            return

        self.progress.setValue(0)
        self.start_btn.setEnabled(False)
        self.start_btn.setText("生成中...")
        self.append_log("\n========== 开始新的混剪任务 ==========")
        self.append_log(f"生成数量：{config.output_count}")
        self.append_log(f"视频文件夹步骤数：{len(config.video_folders)}")
        if config.gpu_enabled:
            self.append_log(f"编码方式：GPU 加速（{config.gpu_vendor} / {config.gpu_encoder}）")
        else:
            self.append_log("编码方式：CPU")
        if config.subtitle_enabled:
            self.append_log(f"字幕：已启用（字体色 {config.subtitle_font_color} / 边框色 {config.subtitle_outline_color} / 边框粗细 {config.subtitle_outline_width} / 字号 {config.subtitle_font_size} / 位置 {config.subtitle_margin_v} / 字体 Impact）")
        else:
            self.append_log("字幕：未启用")

        self.worker = MixWorker(config)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_finished(self, message: str):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始生成")
        self.progress.setValue(100)
        self.append_log(message)
        SuccessDialog(self, message, self.output_picker.path).exec()

    def on_failed(self, message: str):
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始生成")
        self.append_log(f"错误：{message}")
        QMessageBox.critical(self, "生成失败", message)

    def append_log(self, text: str):
        self.log_box.append(text)
        self.log_box.verticalScrollBar().setValue(self.log_box.verticalScrollBar().maximum())


# -----------------------------
# App Entry
# -----------------------------

def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 10))

    # 尽量保持系统原生窗口边框，避免无边框拖拽/适配问题。
    window = RandomMixEditor()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
