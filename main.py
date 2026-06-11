from __future__ import annotations

import asyncio
import json
import random
import shutil
import sys
import time
from collections import deque
from ctypes import c_void_p
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
from OpenGL import GL
from PySide6.QtCore import QEasingCurve, QEvent, QFileSystemWatcher, QPropertyAnimation, QThread, QSize, QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPixmap, QShortcut, QSurfaceFormat
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioFormat, QAudioOutput, QMediaPlayer
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QGraphicsDropShadowEffect,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "resources"
ICONS = ROOT / "icons"
SHADER = ROOT / "shaders" / "main.frag"
STATE = ROOT / "meltake.json"
DOWNLOADS = ROOT / "downloads"


def icon(name: str) -> QIcon:
    for path in (ICONS / f"{name}.svg", ICONS / f"{name}.png", ASSETS / f"buttons/{name}.png"):
        if path.exists():
            return QIcon(str(path))
    return QIcon()


class NeonButton(QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.base_icon, self.hover_icon, self.down_icon = QSize(40, 40), QSize(48, 48), QSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.glow = QGraphicsDropShadowEffect(self)
        self.glow.setBlurRadius(0)
        self.glow.setOffset(0)
        self.glow.setColor(QColor("#ff5fb8"))
        self.setGraphicsEffect(self.glow)
        self._icon_anim = QPropertyAnimation(self, b"iconSize", self)
        self._glow_anim = QPropertyAnimation(self.glow, b"blurRadius", self)
        for anim, duration in ((self._icon_anim, 110), (self._glow_anim, 130)):
            anim.setDuration(duration)
            anim.setEasingCurve(QEasingCurve.OutCubic)
        self.setIconSize(self.base_icon)

    def visual(self, base=40, hover=48, down=34):
        self.base_icon, self.hover_icon, self.down_icon = QSize(base, base), QSize(hover, hover), QSize(down, down)
        self.setIconSize(self.base_icon)
        return self

    def pop(self, size: QSize | None = None, glow: int = 18):
        self._icon_anim.stop()
        self._glow_anim.stop()
        self._icon_anim.setEndValue(size or self.base_icon)
        self._glow_anim.setEndValue(glow if (self.underMouse() or self.isChecked()) else 0)
        self._icon_anim.start()
        self._glow_anim.start()

    def enterEvent(self, event):
        self.setProperty("hot", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.pop(self.hover_icon, 24)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setProperty("hot", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.pop(self.base_icon, 16 if self.isChecked() else 0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.pop(self.down_icon, 34)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.pop(self.hover_icon if self.underMouse() else self.base_icon, 22 if (self.underMouse() or self.isChecked()) else 0)

    def nextCheckState(self):
        super().nextCheckState()
        QTimer.singleShot(0, lambda: self.pop(self.hover_icon if self.underMouse() else self.base_icon, 22 if self.isChecked() else 0))


@dataclass
class Track:
    path: str
    title: str
    artist: str
    album: str
    seconds: int
    cover: bytes | None


def meta(path: str) -> Track:
    audio = MP3(path)
    tag = audio.tags
    title = str(tag.get("TIT2", ["No Title"])[0]) if tag else "No Title"
    artist = str(tag.get("TPE1", ["No Artist"])[0]) if tag else "No Artist"
    album = str(tag.get("TALB", [""])[0]) if tag else ""
    cover = next((v.data for k, v in (tag or {}).items() if k.startswith("APIC")), None)
    return Track(path, title or "No Title", artist or "No Artist", album, int(audio.info.length) + 3, cover)


def write_meta(path: str, title: str, artist: str, album: str = ""):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    tags.setall("TALB", [TALB(encoding=3, text=album)])
    tags.save(path)


def shazam_candidate(result: dict) -> dict[str, str]:
    track = result.get("track") if isinstance(result, dict) else {}
    if not isinstance(track, dict):
        return {}
    title = str(track.get("title") or "").strip()
    artist = str(track.get("subtitle") or "").strip()
    album = ""
    sections = track.get("sections")
    if isinstance(sections, list):
        for section in sections:
            metadata = section.get("metadata") if isinstance(section, dict) else None
            if not isinstance(metadata, list):
                continue
            for item in metadata:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("title") or "").lower()
                if label in {"album", "альбом"}:
                    album = str(item.get("text") or "").strip()
                    break
            if album:
                break
    return {"title": title, "artist": artist, "album": album} if title and artist else {}


async def recognize_with_shazam(path: str) -> dict[str, str]:
    from shazamio import Shazam

    shazam = Shazam()
    recognize = getattr(shazam, "recognize_song", None) or getattr(shazam, "recognize", None)
    if not recognize:
        raise RuntimeError("Installed shazamio does not provide a file recognition method.")
    result = await recognize(path)
    candidate = shazam_candidate(result)
    if not candidate:
        raise RuntimeError("Shazam did not recognize this track.")
    return candidate


class MetadataLookupWorker(QThread):
    found = Signal(dict)
    failed = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            self.found.emit(asyncio.run(recognize_with_shazam(self.path)))
        except Exception as exc:
            self.failed.emit(str(exc))


class MetadataDialog(QDialog):
    accepted_metadata = Signal(str, dict)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path
        self.candidate: dict[str, str] = {}
        self.dots = 0
        self.worker: MetadataLookupWorker | None = None
        self.setWindowTitle("Fill Metadata")
        self.setModal(True)
        self.setFixedWidth(360)
        self.setStyleSheet("""
            QDialog{background:#090513;color:#f7eaff}
            QLabel{background:transparent;color:#f7eaff;font-size:14px}
            QLineEdit{background:rgba(255,95,184,.08);color:#f7eaff;border:1px solid rgba(255,95,184,.35);border-radius:7px;padding:8px 10px;font-size:14px}
            QPushButton{background:rgba(255,95,184,.10);border:1px solid rgba(255,95,184,.35);border-radius:7px;color:#ff5fb8;font-size:14px;font-weight:700;padding:8px 12px}
            QPushButton:hover{background:rgba(255,95,184,.18)}
            QPushButton:disabled{color:rgba(247,234,255,.35);border-color:rgba(247,234,255,.18);background:rgba(247,234,255,.04)}
        """)
        layout = QVBoxLayout()
        self.setLayout(layout)
        current = meta(path)
        self.status = QLabel("Listening with Shazam")
        self.file_name = QLabel(Path(path).name)
        self.file_name.setWordWrap(True)
        self.title_field = QLineEdit(current.title)
        self.artist_field = QLineEdit(current.artist)
        self.album_field = QLineEdit(current.album)
        self.title_field.setPlaceholderText("Title")
        self.artist_field.setPlaceholderText("Artist")
        self.album_field.setPlaceholderText("Album")
        buttons = QHBoxLayout()
        self.accept_btn = QPushButton("Accept", clicked=self.accept_candidate)
        self.cancel_btn = QPushButton("Cancel", clicked=self.reject)
        buttons.addStretch()
        buttons.addWidget(self.accept_btn)
        buttons.addWidget(self.cancel_btn)
        layout.addWidget(self.status)
        layout.addWidget(self.file_name)
        layout.addWidget(QLabel("Title"))
        layout.addWidget(self.title_field)
        layout.addWidget(QLabel("Artist"))
        layout.addWidget(self.artist_field)
        layout.addWidget(QLabel("Album"))
        layout.addWidget(self.album_field)
        layout.addLayout(buttons)
        self.loading = QTimer(self, interval=320, timeout=self.tick_loading)

    def start(self):
        self.loading.start()
        self.worker = MetadataLookupWorker(self.path, self)
        self.worker.found.connect(self.show_candidate)
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.loading.stop)
        self.worker.start()

    def tick_loading(self):
        self.dots = (self.dots + 1) % 4
        self.status.setText(f"Listening with Shazam{'.' * self.dots}")

    def show_candidate(self, candidate: dict):
        self.candidate = candidate
        self.status.setText("Metadata found")
        self.title_field.setText(candidate.get("title", ""))
        self.artist_field.setText(candidate.get("artist", ""))
        self.album_field.setText(candidate.get("album", ""))
        self.accept_btn.setEnabled(True)

    def show_error(self, message: str):
        self.status.setText("Metadata not found")
        self.file_name.setText(f"{Path(self.path).name}\n{message}")

    def accept_candidate(self):
        candidate = {
            "title": self.title_field.text().strip() or "No Title",
            "artist": self.artist_field.text().strip() or "No Artist",
            "album": self.album_field.text().strip(),
        }
        self.accepted_metadata.emit(self.path, candidate)
        self.accept()


class YoutubeDownloadWorker(QThread):
    progress = Signal(str)
    downloaded = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg is required to extract YouTube audio to MP3.")
            from yt_dlp import YoutubeDL

            DOWNLOADS.mkdir(exist_ok=True)

            def hook(status):
                if self.cancelled:
                    raise RuntimeError("Download canceled.")
                if status.get("status") == "downloading":
                    percent = str(status.get("_percent_str") or "").strip()
                    speed = str(status.get("_speed_str") or "").strip()
                    self.progress.emit(f"Downloading {percent} {speed}".strip())
                elif status.get("status") == "finished":
                    self.progress.emit("Extracting audio")

            options = {
                "format": "bestaudio/best",
                "noplaylist": True,
                "outtmpl": str(DOWNLOADS / "%(title).180B.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [hook],
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            }
            before_mp3s = set(DOWNLOADS.glob("*.mp3"))
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(self.url, download=True)
                source_path = Path(ydl.prepare_filename(info))
            path = self.downloaded_mp3_path(info, source_path, before_mp3s)
            if not path:
                raise RuntimeError("Downloaded audio file was not found.")
            title = str(info.get("track") or info.get("title") or path.stem).strip()
            artist = str(info.get("artist") or info.get("uploader") or "YouTube").strip()
            album = str(info.get("album") or "").strip()
            write_meta(str(path), title, artist, album)
            self.downloaded.emit(str(path))
        except Exception as exc:
            self.failed.emit(str(exc))

    def downloaded_mp3_path(self, info: dict, source_path: Path, before_mp3s: set[Path]) -> Path | None:
        requested = info.get("requested_downloads")
        if isinstance(requested, list):
            for item in requested:
                filepath = item.get("filepath") if isinstance(item, dict) else None
                if filepath and Path(filepath).with_suffix(".mp3").exists():
                    return Path(filepath).with_suffix(".mp3")
        converted = source_path.with_suffix(".mp3")
        if converted.exists():
            return converted
        files = sorted(set(DOWNLOADS.glob("*.mp3")) - before_mp3s, key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None


class YoutubeDownloadDialog(QDialog):
    downloaded = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url
        self.dots = 0
        self.worker: YoutubeDownloadWorker | None = None
        self.setWindowTitle("Add From YouTube")
        self.setModal(True)
        self.setFixedWidth(380)
        self.setStyleSheet("""
            QDialog{background:#090513;color:#f7eaff}
            QLabel{background:transparent;color:#f7eaff;font-size:14px}
            QPushButton{background:rgba(255,95,184,.10);border:1px solid rgba(255,95,184,.35);border-radius:7px;color:#ff5fb8;font-size:14px;font-weight:700;padding:8px 12px}
            QPushButton:hover{background:rgba(255,95,184,.18)}
        """)
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.status = QLabel("Preparing download")
        self.detail = QLabel(url)
        self.detail.setWordWrap(True)
        buttons = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel", clicked=self.reject)
        buttons.addStretch()
        buttons.addWidget(self.cancel_btn)
        layout.addWidget(self.status)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)
        self.loading = QTimer(self, interval=320, timeout=self.tick_loading)

    def start(self):
        self.loading.start()
        self.worker = YoutubeDownloadWorker(self.url, self)
        self.worker.progress.connect(self.show_progress)
        self.worker.downloaded.connect(self.finish_download)
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.loading.stop)
        self.worker.start()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText("Canceling")
        super().reject()

    def tick_loading(self):
        self.dots = (self.dots + 1) % 4
        if not self.status.text().endswith("done") and not self.status.text().startswith("Could not"):
            self.status.setText(f"{self.status.text().rstrip('.')}{'.' * self.dots}")

    def show_progress(self, message: str):
        self.status.setText(message)

    def finish_download(self, path: str):
        self.status.setText("Download done")
        self.detail.setText(Path(path).name)
        self.cancel_btn.setText("Close")
        self.downloaded.emit(path)

    def show_error(self, message: str):
        self.status.setText("Could not add song")
        self.detail.setText(message)
        self.cancel_btn.setText("Close")


def fmt(seconds: int) -> str:
    s = max(0, seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_ms(ms: int) -> str:
    return fmt(ms // 1000)


class Analyzer:
    def __init__(self):
        self.fft_size, self.win_size, self.rate = 16384, 4096, 44100
        self.min_f, self.max_f, self.size = 32, 16384, 64
        self.samples, self.spectrum, self.smooth = deque(maxlen=44100 * 8), np.zeros(64), [deque(maxlen=32) for _ in range(64)]
        base = (self.max_f / self.min_f) ** (1 / (self.size - 1))
        self.bins = np.r_[0, [self.min_f * base**i for i in range(self.size)]]
        self.window = np.hamming(self.win_size)

    def push(self, buffer):
        fmt_ = buffer.format()
        self.rate = fmt_.sampleRate() or self.rate
        data = bytes(buffer.data())
        if not data:
            return
        sample_format = fmt_.sampleFormat()
        if sample_format == QAudioFormat.Float:
            a = np.frombuffer(data, np.float32) * 32768
        elif sample_format == QAudioFormat.Int16:
            a = np.frombuffer(data, np.int16).astype(np.float32)
        elif sample_format == QAudioFormat.Int32:
            a = np.frombuffer(data, np.int32).astype(np.float32) / 65536
        elif sample_format == QAudioFormat.UInt8:
            a = (np.frombuffer(data, np.uint8).astype(np.float32) - 128) * 256
        else:
            return
        ch = max(1, fmt_.channelCount())
        self.samples.extend(a[::ch])

    def update(self) -> tuple[np.ndarray, float]:
        a = np.zeros(self.fft_size, np.float32)
        n = min(self.win_size, len(self.samples))
        if n:
            a[:n] = np.fromiter(list(self.samples)[-n:], np.float32) * self.window[:n]
        mag = np.abs(np.fft.rfft(a))
        freqs = np.arange(mag.size) / self.fft_size * self.rate
        spec = np.zeros(self.size)
        for i in range(self.size):
            part = mag[(self.bins[i] <= freqs) & (freqs <= self.bins[i + 1])]
            if part.size:
                self.smooth[i].append(float(part.max()))
            spec[i] = sum(v * ((j + 1) / len(self.smooth[i])) ** 4 for j, v in enumerate(self.smooth[i])) if self.smooth[i] else 0
        self.spectrum = spec
        return spec, float(spec.mean())


class Visualizer(QOpenGLWidget):
    VERTEX = """#version 330 core
layout(location=0) in vec2 vertex_position;
void main(){gl_Position=vec4(vertex_position,0,1);}"""

    def __init__(self, player: QMediaPlayer, analyzer: Analyzer, background: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Visualizer")
        self.player, self.analyzer, self.program, self.vao, self.vbo, self.texture, self.bg_size, self.t0 = player, analyzer, None, None, None, None, (0, 0), time.monotonic()
        self.pulse_strength, self.background_strength, self.vignette_strength, self.visual_scale = 1.0, 1.0, 0.78, 1.0
        self.background = background
        self.error = ""
        self.watcher = QFileSystemWatcher([str(SHADER)], self)
        self.watcher.fileChanged.connect(self.schedule_shader_reload)
        self.reload_timer = QTimer(self, singleShot=True, interval=120, timeout=self.reload_shader)
        self.timer = QTimer(self, timeout=self.update)
        self.timer.start(1000 // 120)

    def initializeGL(self):
        try:
            GL.glClearColor(0, 0, 0, 1)
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            self.program = self.compile_shader()
            self.vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(self.vao)
            self.vbo = GL.glGenBuffers(1)
            vertices = np.array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1], dtype=np.float32)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, vertices.nbytes, vertices, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(0)
            GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, False, 0, c_void_p(0))
            self.load_background()
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            self.setWindowTitle(f"Visualizer error: {self.error[:120]}")
            self.program = None

    def compile_shader(self) -> QOpenGLShaderProgram:
        program = QOpenGLShaderProgram(self)
        if not program.addShaderFromSourceCode(QOpenGLShader.Vertex, self.VERTEX):
            raise RuntimeError(program.log())
        if not program.addShaderFromSourceFile(QOpenGLShader.Fragment, str(SHADER)):
            raise RuntimeError(program.log())
        if not program.link():
            raise RuntimeError(program.log())
        return program

    def schedule_shader_reload(self):
        if str(SHADER) not in self.watcher.files() and SHADER.exists():
            self.watcher.addPath(str(SHADER))
        self.reload_timer.start()

    def reload_shader(self):
        try:
            self.makeCurrent()
            program = self.compile_shader()
            old = self.program
            self.program = program
            if old:
                old.deleteLater()
            self.error = ""
            self.setWindowTitle("Visualizer")
            self.update()
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            self.setWindowTitle(f"Visualizer shader error: {self.error[:120]}")
        finally:
            if str(SHADER) not in self.watcher.files() and SHADER.exists():
                self.watcher.addPath(str(SHADER))

    def load_background(self):
        if not self.background:
            return
        image = QImage(self.background)
        if image.isNull():
            self.setWindowTitle(f"Visualizer: background not found: {self.background}")
            return
        image = image.convertToFormat(QImage.Format_RGBA8888).mirrored(False, True)
        self.bg_size = (image.width(), image.height())
        self.texture = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, image.width(), image.height(), 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, bytes(image.bits()))

    def resizeGL(self, width: int, height: int):
        dpr = self.devicePixelRatioF()
        GL.glViewport(0, 0, max(1, int(width * dpr)), max(1, int(height * dpr)))

    def paintGL(self):
        if not self.program:
            GL.glClearColor(0.35, 0.02, 0.05, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            return
        spec, max_mag = self.analyzer.update() if self.player.playbackState() == QMediaPlayer.PlayingState else (self.analyzer.spectrum, float(self.analyzer.spectrum.mean()))
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        self.program.bind()
        GL.glBindVertexArray(self.vao)
        pid = self.program.programId()
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_time"), time.monotonic() - self.t0)
        dpr = self.devicePixelRatioF()
        GL.glUniform2f(GL.glGetUniformLocation(pid, "u_resolution"), max(1, self.width() * dpr), max(1, self.height() * dpr))
        GL.glUniform1fv(GL.glGetUniformLocation(pid, "u_spectrum"), 64, spec.astype(np.float32))
        GL.glUniform1i(GL.glGetUniformLocation(pid, "u_spectrum_size"), 64)
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_max_magnitude"), max_mag)
        dur = max(1, self.player.duration())
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_audio_position"), self.player.position() / dur)
        GL.glUniform1i(GL.glGetUniformLocation(pid, "u_has_background"), bool(self.texture))
        GL.glUniform2f(GL.glGetUniformLocation(pid, "u_background_resolution"), max(1, self.bg_size[0]), max(1, self.bg_size[1]))
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_pulse_strength"), self.pulse_strength)
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_background_strength"), self.background_strength)
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_vignette_strength"), self.vignette_strength)
        GL.glUniform1f(GL.glGetUniformLocation(pid, "u_visual_scale"), self.visual_scale)
        if self.texture:
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.texture)
            GL.glUniform1i(GL.glGetUniformLocation(pid, "u_background"), 0)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 6)
        self.program.release()


class MelTake(QMainWindow):
    def __init__(self, background: str = ""):
        super().__init__()
        self.setWindowTitle("MelTake")
        self.state = self.load_state()
        self.playlists = self.state["playlists"]
        self.current = self.state["current"]
        self.mode = self.state["mode"]
        self.animation = self.state["animation"]
        self.player, self.audio = QMediaPlayer(self), QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(self.state["volume"] / 100)
        self.audio.setMuted(self.state["muted"])
        self.analyzer = Analyzer()
        self.buffer_output = QAudioBufferOutput(self)
        self.buffer_output.audioBufferReceived.connect(self.analyzer.push)
        self.player.setAudioBufferOutput(self.buffer_output)
        self.build_ui(background)
        self.refresh_playlists()
        self.show_playlist(self.current)
        if self.playlists["All tracks"]:
            self.display(meta(self.playlists["All tracks"][0]))
        self.player.positionChanged.connect(self.tick)
        self.player.durationChanged.connect(self.progress.setMaximum)
        self.player.mediaStatusChanged.connect(self.autonext)
        self.shortcuts()

    def load_state(self):
        state = {"playlists": {"All tracks": []}, "current": "All tracks", "mode": "loop", "volume": 60, "muted": False, "sidebar_width_percent": 25, "animation": {"pulse": 100, "background": 100, "vignette": 78, "size": 100}}
        if STATE.exists():
            try:
                data = json.loads(STATE.read_text())
                if isinstance(data, dict):
                    state.update({k: data[k] for k in state.keys() & data.keys()})
                    if "sidebar_width" in data and "sidebar_width_percent" not in data:
                        state["sidebar_width_percent"] = round(100 * int(data["sidebar_width"]) / 1400)
                    if isinstance(data.get("animation"), dict):
                        state["animation"].update(data["animation"])
            except Exception:
                pass
        playlists = state["playlists"] if isinstance(state["playlists"], dict) else {}
        state["animation"] = state["animation"] if isinstance(state["animation"], dict) else {}
        state["playlists"] = {"All tracks": []}
        for name, paths in playlists.items():
            clean = [p for p in paths if isinstance(p, str) and Path(p).exists()]
            state["playlists"][name or "All tracks"] = list(dict.fromkeys(clean))
        for paths in state["playlists"].values():
            for path in paths:
                if path not in state["playlists"]["All tracks"]:
                    state["playlists"]["All tracks"].append(path)
        if state["current"] not in state["playlists"]:
            state["current"] = "All tracks"
        try:
            state["volume"] = max(0, min(100, int(state["volume"])))
        except (TypeError, ValueError):
            state["volume"] = 60
        state["muted"] = bool(state["muted"])
        state["mode"] = state["mode"] if state["mode"] in {"loop", "random", "one"} else "loop"
        try:
            state["sidebar_width_percent"] = max(18, min(40, float(state["sidebar_width_percent"])))
        except (TypeError, ValueError):
            state["sidebar_width_percent"] = 25
        for key, default in {"pulse": 100, "background": 100, "vignette": 78, "size": 100}.items():
            try:
                state["animation"][key] = int(state["animation"].get(key, default))
            except (TypeError, ValueError):
                state["animation"][key] = default
        return state

    def save_state(self):
        data = {"playlists": self.playlists, "current": self.current, "mode": self.mode, "volume": self.volume.value() if hasattr(self, "volume") else self.state["volume"], "muted": self.audio.isMuted(), "sidebar_width_percent": getattr(self, "library_width_percent", self.state["sidebar_width_percent"]), "animation": self.animation}
        STATE.write_text(json.dumps(data, indent=2))

    def build_ui(self, background: str):
        w, stack, overlay, root = QWidget(), QGridLayout(), QWidget(), QHBoxLayout()
        stack.setContentsMargins(0, 0, 0, 0)
        w.setLayout(stack)
        overlay.setLayout(root)
        self.setCentralWidget(w)
        self.visualizer = Visualizer(self.player, self.analyzer, background, w)
        self.apply_animation()
        stack.addWidget(self.visualizer, 0, 0)
        stack.addWidget(overlay, 0, 0)
        overlay.setAttribute(Qt.WA_TranslucentBackground)
        w.setStyleSheet("QWidget{background:transparent;color:#f7eaff} QPushButton{color:#ff5fb8;background:rgba(0,0,0,.14);border:0;border-radius:25px;font-weight:bold;font-size:22px} QPushButton:hover,QPushButton[hot=true]{color:#71ffff;background:rgba(255,95,184,.10)} QPushButton:pressed{background:rgba(255,95,184,.22)} QPushButton:checked{color:white;background:rgba(255,95,184,.24)} QLabel{background:transparent} QLineEdit{background:rgba(255,95,184,.08);color:#f7eaff;border:1px solid rgba(255,95,184,.35);border-radius:8px;padding:8px 10px;font-size:14px} QTableWidget{background:transparent;color:#f7eaff;selection-background-color:rgba(255,95,184,.22);border:0;gridline-color:rgba(255,255,255,.12)} QTableWidget::item{border-bottom:1px solid rgba(255,255,255,.10);padding:8px} QHeaderView::section{background:transparent;color:#ff5fb8;border:0;padding:4px} QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,.28);border-radius:2px} QSlider::handle:horizontal{background:#ff5fb8;width:18px;height:18px;margin:-7px 0;border-radius:9px} QSlider::handle:horizontal:hover{background:#71ffff} QSlider::sub-page:horizontal{background:#ff5fb8;border-radius:2px}")
        root.setContentsMargins(36, 32, 36, 28)
        hud, top, bottom = QVBoxLayout(), QHBoxLayout(), QHBoxLayout()
        root.addLayout(hud)
        self.load_btn = NeonButton("Load", clicked=self.load_tracks).visual(0, 0, 0)
        self.cover, self.title, self.artist = QLabel(), QLabel("No Title"), QLabel("No Artist")
        self.cover.hide()
        self.title.setStyleSheet("font-size:26px;font-weight:500;color:white")
        self.artist.setStyleSheet("font-size:16px;color:rgba(255,255,255,.62)")
        title_box = QVBoxLayout()
        title_box.addWidget(self.title)
        title_box.addWidget(self.artist)
        top.addLayout(title_box)
        top.addStretch()
        controls = QHBoxLayout()
        self.prev = NeonButton(icon=icon("previous"), clicked=self.prev_track)
        self.play = NeonButton(icon=icon("play"), checkable=True, clicked=self.play_pause).visual(46, 54, 40)
        self.next = NeonButton(icon=icon("next"), clicked=self.next_track)
        self.random = NeonButton(icon=icon("random"), checkable=True, clicked=self.toggle_random)
        self.repeat = NeonButton(icon=icon("loop"), checkable=True, clicked=self.toggle_repeat)
        self.mute = NeonButton(icon=icon("volume_on"), checkable=True, clicked=self.toggle_mute)
        self.lock = NeonButton(icon=icon("lock"), checkable=True)
        self.menu = NeonButton(icon=icon("menu"))
        if self.lock.icon().isNull():
            self.lock.setText("Lock")
        if self.menu.icon().isNull():
            self.menu.setText("Menu")
        self.menu.clicked.connect(self.toggle_menu)
        self.lock.clicked.connect(self.toggle_bottom_panel)
        for b in (self.prev, self.play, self.next, self.random, self.repeat, self.mute, self.lock, self.menu):
            b.setFixedSize(62, 62)
        for b in (self.random, self.prev, self.play, self.next, self.repeat, self.lock, self.menu):
            controls.addWidget(b)
        self.volume = QSlider(Qt.Horizontal, value=self.state["volume"], minimum=0, maximum=100, valueChanged=self.set_volume)
        self.progress = QSlider(Qt.Horizontal, sliderMoved=self.player.setPosition)
        self.time = QLabel("00:00 / 00:00")
        top.addLayout(controls)
        hud.addLayout(top)
        hud.addStretch()
        self.bottom_panel = QWidget()
        self.bottom_panel.setLayout(bottom)
        bottom.addWidget(self.time)
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.mute)
        bottom.addWidget(self.volume)
        hud.addWidget(self.bottom_panel)

        self.library = QWidget()
        library = QVBoxLayout()
        self.library.setLayout(library)
        self.library.setObjectName("sidebar")
        self.library.setStyleSheet("""
            QWidget#sidebar{background:rgba(4,3,14,.76);border:1px solid rgba(255,95,184,.42);border-radius:10px}
            QLabel#sideTitle{font-size:17px;font-weight:700;color:#ff5fb8;letter-spacing:0}
            QLabel#sideSection{font-size:15px;font-weight:700;color:#ff5fb8}
            QPushButton#sideIcon{background:transparent;border:0;border-radius:8px;padding:0}
            QPushButton#sideIcon:hover{background:rgba(255,95,184,.10)}
            QPushButton#sideTab{background:transparent;border:0;border-bottom:2px solid transparent;border-radius:0;color:rgba(247,234,255,.62);font-size:14px;font-weight:600;padding:8px 0}
            QPushButton#sideTab:checked{color:#ff5fb8;border-bottom:2px solid #ff5fb8;background:transparent}
            QPushButton#panelButton{background:rgba(255,95,184,.06);border:1px solid rgba(255,95,184,.35);border-radius:7px;color:#ff5fb8;font-size:15px;font-weight:700;padding:8px}
            QPushButton#panelButton:hover{background:rgba(255,95,184,.13)}
            QLineEdit#sideSearch{background:rgba(255,95,184,.07);color:#f7eaff;border:1px solid rgba(255,95,184,.35);border-radius:7px;padding:8px 10px;font-size:14px}
            QTableWidget#sideTable{background:transparent;border:0;color:#f7eaff;selection-background-color:rgba(255,95,184,.14);selection-color:#ff8fcc}
            QTableWidget#sideTable::item{border-bottom:1px solid rgba(255,255,255,.10);padding:8px}
            QTableWidget#sideTable::item:selected{color:#ff8fcc}
            QScrollBar{background:transparent;width:0;height:0}
        """)
        library.setContentsMargins(22, 20, 22, 20)
        library.setSpacing(12)
        head = QHBoxLayout()
        self.sidebar_title = QLabel("PLAYLIST")
        self.sidebar_title.setObjectName("sideTitle")
        self.new_pl_icon = icon("add")
        self.find_icon = icon("find")
        self.new_pl = NeonButton(icon=self.new_pl_icon, clicked=self.new_playlist).visual(24, 30, 20)
        self.find_btn = NeonButton(icon=self.find_icon, clicked=self.toggle_search).visual(24, 30, 20)
        for b in (self.new_pl, self.find_btn):
            b.setObjectName("sideIcon")
            b.setFixedSize(42, 42)
        head.addWidget(self.sidebar_title)
        head.addStretch()
        head.addWidget(self.new_pl)
        head.addWidget(self.find_btn)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find track")
        self.search.setObjectName("sideSearch")
        self.search.textChanged.connect(self.filter_tracks)
        self.search.hide()
        tabs = QHBoxLayout()
        self.playlist_tab = NeonButton("Playlist", checkable=True, clicked=lambda: self.switch_sidebar_tab(0)).visual(0, 0, 0)
        self.animation_tab = NeonButton("Animation", checkable=True, clicked=lambda: self.switch_sidebar_tab(1)).visual(0, 0, 0)
        for b in (self.playlist_tab, self.animation_tab):
            b.setObjectName("sideTab")
            b.setFixedHeight(40)
        self.playlist_tab.setChecked(True)
        tabs.addWidget(self.playlist_tab)
        tabs.addWidget(self.animation_tab)
        self.sidebar_pages = QStackedWidget()

        playlist_page, playlist_layout = QWidget(), QVBoxLayout()
        playlist_page.setLayout(playlist_layout)
        playlist_layout.setContentsMargins(0, 0, 0, 0)
        playlist_layout.setSpacing(12)
        self.tracks = QTableWidget(0, 4)
        self.tracks.setHorizontalHeaderLabels(["track_name", "path", "time", ""])
        self.tracks.hideColumn(1)
        self.tracks.horizontalHeader().hide()
        self.tracks.verticalHeader().hide()
        self.tracks.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tracks.doubleClicked.connect(lambda i: self.play_index(i.row()))
        self.tracks.setObjectName("sideTable")
        self.tracks.setShowGrid(False)
        self.tracks.setFrameShape(QFrame.NoFrame)
        self.tracks.setFocusPolicy(Qt.NoFocus)
        self.tracks.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tracks.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tracks.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tracks.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.tracks.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.tracks.setColumnWidth(2, 62)
        self.tracks.setColumnWidth(3, 96)
        self.pl_label = QLabel("All tracks", alignment=Qt.AlignCenter)
        self.pl_label.setObjectName("sideSection")
        self.del_pl = NeonButton("Delete Playlist", clicked=self.delete_playlist).visual(0, 0, 0)
        self.del_pl.setObjectName("panelButton")
        self.pls = QTableWidget(0, 1)
        self.pls.setHorizontalHeaderLabels(["playlists"])
        self.pls.horizontalHeader().hide()
        self.pls.verticalHeader().hide()
        self.pls.clicked.connect(lambda i: self.show_playlist(self.pls.item(i.row(), 0).text()))
        self.pls.setObjectName("sideTable")
        self.pls.setShowGrid(False)
        self.pls.setFrameShape(QFrame.NoFrame)
        self.pls.setFocusPolicy(Qt.NoFocus)
        self.pls.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pls.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pls.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.pls.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.pls.setMaximumHeight(96)
        self.load_btn.setObjectName("panelButton")
        self.youtube_btn = NeonButton("YouTube", clicked=self.add_youtube_song).visual(0, 0, 0)
        self.youtube_btn.setObjectName("panelButton")
        self.del_pl.setFixedHeight(38)
        playlist_layout.addWidget(self.load_btn)
        playlist_layout.addWidget(self.youtube_btn)
        playlist_layout.addWidget(self.pl_label)
        playlist_layout.addWidget(self.pls)
        playlist_layout.addWidget(self.del_pl)
        playlist_layout.addWidget(self.tracks)

        animation_page, animation_layout = QWidget(), QVBoxLayout()
        animation_page.setLayout(animation_layout)
        animation_layout.setContentsMargins(0, 0, 0, 0)
        animation_layout.setSpacing(18)
        settings = QLabel("ANIMATION SETTINGS", alignment=Qt.AlignCenter)
        settings.setObjectName("sideSection")
        animation_layout.addWidget(settings)
        for row in (
            self.setting_row("Pulse", "pulse", 0, 200),
            self.setting_row("Background", "background", 0, 160),
            self.setting_row("Vignette", "vignette", 0, 100),
            self.setting_row("Size", "size", 70, 130),
        ):
            animation_layout.addLayout(row)
        animation_layout.addStretch()
        self.sidebar_pages.addWidget(playlist_page)
        self.sidebar_pages.addWidget(animation_page)
        library.addLayout(head)
        library.addWidget(self.search)
        library.addLayout(tabs)
        library.addWidget(self.sidebar_pages)
        self.library_width_percent = self.state["sidebar_width_percent"]
        self.library_width = self.sidebar_width_px()
        self.library.setMaximumWidth(0)
        self.library.hide()
        self.library.installEventFilter(self)
        root.addWidget(self.library)
        self.random.setChecked(self.mode == "random")
        self.repeat.setChecked(self.mode == "one")
        self.mute.setChecked(self.state["muted"])
        self.mute.setIcon(icon(f"volume_{'off' if self.mute.isChecked() else 'on'}"))
        self.display(None)

    def setting_row(self, name: str, key: str, minimum: int, maximum: int):
        value = int(self.animation[key])
        row, label, slider = QHBoxLayout(), QLabel(f"{name}: {value}%"), QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        slider.valueChanged.connect(lambda v: (label.setText(f"{name}: {v}%"), self.set_animation(key, v)))
        row.addWidget(label)
        row.addWidget(slider)
        return row

    def switch_sidebar_tab(self, index: int):
        self.sidebar_pages.setCurrentIndex(index)
        self.playlist_tab.setChecked(index == 0)
        self.animation_tab.setChecked(index == 1)
        self.sidebar_title.setText("PLAYLIST" if index == 0 else "ANIMATION")
        playlist_visible = index == 0
        self.new_pl.setIcon(self.new_pl_icon if playlist_visible else QIcon())
        self.find_btn.setIcon(self.find_icon if playlist_visible else QIcon())
        for button in (self.new_pl, self.find_btn):
            button.setEnabled(playlist_visible)
            button.setAttribute(Qt.WA_TransparentForMouseEvents, not playlist_visible)
        if not playlist_visible:
            self.search.clear()
            self.search.hide()

    def toggle_search(self):
        self.search.setVisible(not self.search.isVisible())
        if self.search.isVisible():
            self.search.setFocus()
        else:
            self.search.clear()

    def filter_tracks(self, text: str):
        text = text.lower().strip()
        for row in range(self.tracks.rowCount()):
            item = self.tracks.item(row, 0)
            self.tracks.setRowHidden(row, bool(text and item and text not in item.text().lower()))

    def sidebar_width_px(self):
        return max(320, int(self.width() * min(self.library_width_percent, 40) / 100))

    def set_sidebar_width_percent(self, percent: float, save: bool = True):
        self.library_width_percent = max(18, min(40, percent))
        self.library_width = self.sidebar_width_px()
        if self.library.isVisible():
            self.library.setMinimumWidth(self.library_width)
            self.library.setMaximumWidth(self.library_width)
        if save:
            self.save_state()

    def eventFilter(self, obj, event):
        if obj is self.library:
            x = event.position().x() if hasattr(event, "position") else -1
            if event.type() == QEvent.MouseButtonPress and x <= 8:
                self.resizing_sidebar = (event.globalPosition().x(), self.library_width)
                self.library.setCursor(Qt.SizeHorCursor)
                return True
            if event.type() == QEvent.MouseMove:
                if hasattr(self, "resizing_sidebar"):
                    start_x, start_w = self.resizing_sidebar
                    self.set_sidebar_width_percent(100 * (start_w + start_x - event.globalPosition().x()) / max(1, self.width()), save=False)
                    return True
                self.library.setCursor(Qt.SizeHorCursor if x <= 8 else Qt.ArrowCursor)
            if event.type() == QEvent.MouseButtonRelease and hasattr(self, "resizing_sidebar"):
                del self.resizing_sidebar
                self.library.setCursor(Qt.ArrowCursor)
                self.save_state()
                return True
        return super().eventFilter(obj, event)

    def apply_animation(self):
        self.visualizer.pulse_strength = self.animation["pulse"] / 100
        self.visualizer.background_strength = self.animation["background"] / 100
        self.visualizer.vignette_strength = self.animation["vignette"] / 100
        self.visualizer.visual_scale = self.animation["size"] / 100

    def set_animation(self, key: str, value: int):
        self.animation[key] = value
        self.apply_animation()
        self.visualizer.update()
        self.save_state()

    def shortcuts(self):
        for key, fn in [(Qt.Key_Space, self.play_pause), (Qt.Key_Right, self.next_track), (Qt.Key_Left, self.prev_track), (Qt.Key_F4, self.toggle_mute), (Qt.Key_F5, lambda: self.volume.setValue(self.volume.value() - 5)), (Qt.Key_F6, lambda: self.volume.setValue(self.volume.value() + 5)), (Qt.Key_F11, lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen()), (Qt.Key_F12, lambda: self.showNormal() if self.isMinimized() else self.showMinimized())]:
            QShortcut(QKeySequence(key), self, activated=fn)

    def toggle_menu(self):
        opening = not self.library.isVisible()
        self.library.setVisible(True)
        self.library.setMinimumWidth(0)
        self.library_width = self.sidebar_width_px()
        self.menu_anim = QPropertyAnimation(self.library, b"maximumWidth", self)
        self.menu_anim.setDuration(190)
        self.menu_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.menu_anim.setStartValue(0 if opening else self.library_width)
        self.menu_anim.setEndValue(self.library_width if opening else 0)
        if opening:
            self.menu_anim.finished.connect(lambda: self.library.setMinimumWidth(self.library_width))
        else:
            self.menu_anim.finished.connect(self.library.hide)
        self.menu_anim.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "library") and self.library.isVisible():
            self.library_width = self.sidebar_width_px()
            self.library.setMinimumWidth(self.library_width)
            self.library.setMaximumWidth(self.library_width)

    def toggle_bottom_panel(self, locked: bool):
        h = max(48, self.bottom_panel.sizeHint().height())
        self.bottom_panel.setVisible(True)
        self.panel_anim = QPropertyAnimation(self.bottom_panel, b"maximumHeight", self)
        self.panel_anim.setDuration(180)
        self.panel_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.panel_anim.setStartValue(self.bottom_panel.height() if locked else 0)
        self.panel_anim.setEndValue(0 if locked else h)
        if locked:
            self.panel_anim.finished.connect(self.bottom_panel.hide)
        self.panel_anim.start()

    def refresh_playlists(self):
        self.pls.setRowCount(0)
        for name in self.playlists:
            r = self.pls.rowCount()
            self.pls.insertRow(r)
            self.pls.setRowHeight(r, 34)
            self.pls.setItem(r, 0, QTableWidgetItem(name))

    def show_playlist(self, name: str):
        self.current = name if name in self.playlists else "All tracks"
        self.pl_label.setText(f"{self.current}")
        self.tracks.setRowCount(0)
        for path in self.playlists[self.current]:
            self.add_row(path)
        self.filter_tracks(self.search.text() if hasattr(self, "search") else "")
        self.save_state()

    def add_row(self, path: str):
        m, r = meta(path), self.tracks.rowCount()
        self.tracks.insertRow(r)
        self.tracks.setRowHeight(r, 46)
        for c, text in enumerate((f" {m.title} - {m.artist}", path, fmt(m.seconds))):
            self.tracks.setItem(r, c, QTableWidgetItem(text))
        actions, actions_layout = QWidget(), QHBoxLayout()
        actions.setLayout(actions_layout)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(2)
        identify = NeonButton("ID", clicked=lambda _=False, p=path: self.fill_metadata(p)).visual(0, 0, 0)
        fav = NeonButton(icon=icon("add_to_favourites"), clicked=lambda _=False, p=path: self.add_to_favourites(p)).visual(20, 26, 18)
        remove = NeonButton(icon=icon("cross_clicked"), clicked=lambda _=False, p=path: self.delete_track(p)).visual(20, 26, 18)
        for btn in (identify, fav, remove):
            btn.setObjectName("sideIcon")
            btn.setFixedSize(28, 28)
            actions_layout.addWidget(btn)
        self.tracks.setCellWidget(r, 3, actions)

    def fill_metadata(self, path: str):
        if not hasattr(self, "metadata_dialogs"):
            self.metadata_dialogs = []
        dialog = MetadataDialog(path, self)
        self.metadata_dialogs.append(dialog)
        dialog.accepted_metadata.connect(self.apply_metadata)
        dialog.show()
        dialog.start()
        if dialog.worker:
            dialog.worker.finished.connect(lambda d=dialog: self.metadata_dialogs.remove(d) if d in self.metadata_dialogs else None)

    def apply_metadata(self, path: str, candidate: dict):
        write_meta(path, candidate.get("title", ""), candidate.get("artist", ""), candidate.get("album", ""))
        self.show_playlist(self.current)
        if self.player.source().toLocalFile() == path:
            self.display(meta(path))

    def add_youtube_song(self):
        url, ok = QInputDialog.getText(self, "Add from YouTube", "YouTube URL:")
        if not ok:
            return
        url = url.strip()
        if not url:
            return
        if not hasattr(self, "youtube_dialogs"):
            self.youtube_dialogs = []
        dialog = YoutubeDownloadDialog(url, self)
        self.youtube_dialogs.append(dialog)
        dialog.downloaded.connect(self.add_downloaded_song)
        dialog.show()
        dialog.start()
        if dialog.worker:
            dialog.worker.finished.connect(lambda d=dialog: self.youtube_dialogs.remove(d) if d in self.youtube_dialogs else None)

    def add_downloaded_song(self, path: str):
        if path not in self.playlists["All tracks"]:
            self.playlists["All tracks"].append(path)
        if self.current != "All tracks" and path not in self.playlists[self.current]:
            self.playlists[self.current].append(path)
        self.show_playlist(self.current)
        self.display(meta(path))
        self.save_state()

    def add_to_favourites(self, path: str):
        self.playlists.setdefault("Favorites", [])
        if path not in self.playlists["Favorites"]:
            self.playlists["Favorites"].append(path)
            self.refresh_playlists()
            self.save_state()

    def load_tracks(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Open files", "", "MPEG Files (*.mp3 *.mpeg *.mpg)")
        for path in files:
            if path not in self.playlists["All tracks"]:
                self.playlists["All tracks"].append(path)
            if self.current != "All tracks" and path not in self.playlists[self.current]:
                self.playlists[self.current].append(path)
        self.show_playlist(self.current)
        self.display(meta(self.playlists["All tracks"][0]) if self.playlists["All tracks"] else None)
        self.save_state()

    def new_playlist(self):
        name, ok = QInputDialog.getText(self, "New Playlist", "Enter playlist name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            name = f"Playlist {len(self.playlists)}"
        if name not in self.playlists:
            self.playlists[name] = []
            self.refresh_playlists()
            self.save_state()

    def delete_playlist(self):
        if self.current == "All tracks":
            return
        self.playlists.pop(self.current, None)
        self.refresh_playlists()
        self.show_playlist("All tracks")
        self.save_state()

    def delete_track(self, path: str):
        targets = self.playlists.values() if self.current == "All tracks" else (self.playlists[self.current],)
        for paths in targets:
            while path in paths:
                paths.remove(path)
        self.show_playlist(self.current)
        self.save_state()

    def play_index(self, i: int):
        paths = self.playlists[self.current]
        if not paths:
            return
        path = paths[i % len(paths)]
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.play.setChecked(True)
        self.play.setIcon(icon("pause"))
        self.display(meta(path))

    def play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play.setChecked(False)
            self.play.setIcon(icon("play"))
        else:
            if self.player.source().isEmpty() and self.playlists[self.current]:
                self.play_index(max(0, self.current_index()))
            else:
                self.player.play()
            self.play.setChecked(True)
            self.play.setIcon(icon("pause"))

    def current_index(self):
        src = self.player.source().toLocalFile()
        if src and src in self.playlists[self.current]:
            return self.playlists[self.current].index(src)
        return -1

    def next_track(self):
        paths = self.playlists[self.current]
        if not paths:
            return
        i = self.current_index()
        self.play_index(random.randrange(len(paths)) if self.mode == "random" else (i + 1) % len(paths))

    def prev_track(self):
        paths = self.playlists[self.current]
        if paths:
            self.play_index((self.current_index() - 1) % len(paths))

    def autonext(self, status):
        if status == QMediaPlayer.EndOfMedia:
            self.player.setPosition(0)
            (self.player.play() if self.mode == "one" else self.next_track())

    def toggle_random(self):
        self.mode = "random" if self.random.isChecked() else "loop"
        self.repeat.setChecked(False)
        self.save_state()

    def toggle_repeat(self):
        self.mode = "one" if self.repeat.isChecked() else "loop"
        self.random.setChecked(False)
        self.save_state()

    def toggle_mute(self):
        self.audio.setMuted(self.mute.isChecked())
        self.mute.setIcon(icon(f"volume_{'off' if self.mute.isChecked() else 'on'}"))
        self.save_state()

    def set_volume(self, v):
        self.audio.setVolume(v / 100)
        self.mute.setChecked(v == 0)
        self.audio.setMuted(v == 0)
        self.save_state()

    def tick(self, pos):
        self.progress.setValue(pos)
        self.time.setText(f"{fmt_ms(pos)} / {fmt_ms(self.player.duration())}")

    def display(self, m: Track | None):
        if not m:
            self.title.setText("No Title")
            self.artist.setText("No Artist")
            self.cover.setPixmap(QPixmap(str(ASSETS / "cover.png")))
            return
        self.title.setText(m.title)
        self.artist.setText(m.artist)
        pix = QPixmap()
        if m.cover:
            img = QImage.fromData(m.cover)
            pix = QPixmap.fromImage(img)
        self.cover.setPixmap((pix if not pix.isNull() else QPixmap(str(ASSETS / "cover.png"))).scaled(300, 300, Qt.KeepAspectRatio))


def main():
    fmt_ = QSurfaceFormat()
    fmt_.setVersion(3, 3)
    fmt_.setProfile(QSurfaceFormat.CoreProfile)
    QSurfaceFormat.setDefaultFormat(fmt_)
    app = QApplication(sys.argv)
    win = MelTake(sys.argv[1] if len(sys.argv) > 1 else "")
    win.resize(1400, 900)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
