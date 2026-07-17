"""
mic_engine.py
マイクリアルタイム入力エンジン。
PyAudioを使ってマイクデバイスの列挙とリアルタイムストリームを管理する。

- デバイス列挙: get_input_devices() → [(index, name), ...]
- ストリーム管理: MicStream クラス（スレッドセーフなリングバッファ）
- 各トラックは独立した MicStream インスタンスを持つ
"""
import threading
import numpy as np
from typing import Optional, List, Tuple

# PyAudioはWindowsでのみ実マイクが使えるが、
# インポートエラー時はダミーモードで動作させる
try:
    import pyaudio
    # デバイスが存在するか確認（サンドボックス等ではデバイスなし）
    _pa_test = pyaudio.PyAudio()
    _PYAUDIO_DEVICE_COUNT = _pa_test.get_device_count()
    _pa_test.terminate()
    _PYAUDIO_AVAILABLE = _PYAUDIO_DEVICE_COUNT > 0
except Exception:
    _PYAUDIO_AVAILABLE = False
    _PYAUDIO_DEVICE_COUNT = 0

# ============================
# 定数
# ============================
SAMPLE_RATE = 44100
CHANNELS = 1          # マイクはモノラル取得（ミキサー内でステレオ変換）
CHUNK_FRAMES = 1024   # PyAudioのコールバックチャンクサイズ
BUFFER_SECONDS = 0.5  # リングバッファの長さ（秒）
BUFFER_FRAMES = int(SAMPLE_RATE * BUFFER_SECONDS)


def get_input_devices() -> List[Tuple[int, str]]:
    """
    入力デバイスの一覧を返す。
    Returns:
        [(device_index, device_name), ...]
    """
    if not _PYAUDIO_AVAILABLE:
        return [(0, "(PyAudio not available)")]
    try:
        p = pyaudio.PyAudio()
        devices = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                devices.append((i, info["name"]))
        p.terminate()
        if not devices:
            devices = [(0, "(No input devices found)")]
        return devices
    except Exception as e:
        return [(0, f"(Error: {e})")]


class MicStream:
    """
    1トラック分のマイク入力ストリームを管理するクラス。
    バックグラウンドスレッドでマイクから音声を取得し、
    リングバッファに書き込む。
    audio_engine側は read_chunk() でバッファから読み出す。
    """

    def __init__(self, device_index: int, device_name: str):
        self.device_index = device_index
        self.device_name = device_name
        self._lock = threading.Lock()
        # リングバッファ（float32, モノラル）
        self._buffer = np.zeros(BUFFER_FRAMES, dtype=np.float32)
        self._write_pos = 0   # 書き込み位置
        self._read_pos = 0    # 読み出し位置
        self._active = False
        self._stream = None
        self._pa = None

    def start(self) -> bool:
        """
        マイクストリームを開始する。
        Returns:
            True: 成功, False: 失敗
        """
        if self._active:
            return True
        if not _PYAUDIO_AVAILABLE:
            # ダミーモード（サンドボックス/デバイスなし環境）: 無音を生成
            self._active = True
            self._dummy_thread = threading.Thread(
                target=self._dummy_loop, daemon=True
            )
            self._dummy_thread.start()
            return True
        try:
            self._pa = pyaudio.PyAudio()
            # 指定デバイスが入力対応か確認
            info = self._pa.get_device_info_by_index(self.device_index)
            if info.get("maxInputChannels", 0) == 0:
                raise ValueError(f"Device {self.device_index} has no input channels")
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                input_device_index=self.device_index,
                frames_per_buffer=CHUNK_FRAMES,
                stream_callback=self._callback,
            )
            self._stream.start_stream()
            self._active = True
            return True
        except Exception as e:
            print(f"[MicStream] start error: {e}")
            if self._pa:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
            # フォールバック: ダミーモードで起動
            self._active = True
            self._dummy_thread = threading.Thread(
                target=self._dummy_loop, daemon=True
            )
            self._dummy_thread.start()
            return True

    def stop(self):
        """マイクストリームを停止する。"""
        self._active = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def _callback(self, in_data, frame_count, time_info, status):
        """PyAudioコールバック: マイクデータをリングバッファに書き込む。"""
        if not self._active:
            return (None, pyaudio.paComplete)
        samples = np.frombuffer(in_data, dtype=np.float32).copy()
        self._write_samples(samples)
        return (None, pyaudio.paContinue)

    def _dummy_loop(self):
        """ダミーモード: 無音（ゼロ）をバッファに書き込み続ける。"""
        import time
        chunk = np.zeros(CHUNK_FRAMES, dtype=np.float32)
        interval = CHUNK_FRAMES / SAMPLE_RATE
        while self._active:
            self._write_samples(chunk)
            time.sleep(interval)

    def _write_samples(self, samples: np.ndarray):
        """サンプルをリングバッファに書き込む（スレッドセーフ）。"""
        n = len(samples)
        with self._lock:
            end = self._write_pos + n
            if end <= BUFFER_FRAMES:
                self._buffer[self._write_pos:end] = samples
            else:
                first = BUFFER_FRAMES - self._write_pos
                self._buffer[self._write_pos:] = samples[:first]
                self._buffer[:n - first] = samples[first:]
            self._write_pos = end % BUFFER_FRAMES

    def read_chunk(self, n_frames: int) -> np.ndarray:
        """
        リングバッファから n_frames サンプルを読み出す（モノラル float32）。
        バッファが不足している場合はゼロパディング。
        Returns:
            shape=(n_frames,) の float32 配列
        """
        out = np.zeros(n_frames, dtype=np.float32)
        with self._lock:
            available = (self._write_pos - self._read_pos) % BUFFER_FRAMES
            read_n = min(n_frames, available)
            if read_n > 0:
                end = self._read_pos + read_n
                if end <= BUFFER_FRAMES:
                    out[:read_n] = self._buffer[self._read_pos:end]
                else:
                    first = BUFFER_FRAMES - self._read_pos
                    out[:first] = self._buffer[self._read_pos:]
                    out[first:read_n] = self._buffer[:read_n - first]
                self._read_pos = end % BUFFER_FRAMES
        return out

    @property
    def is_active(self) -> bool:
        return self._active


# ============================
# グローバルマイクストリーム管理
# ============================
# track_id -> MicStream のマッピング
_mic_streams: dict = {}
_streams_lock = threading.Lock()


def assign_mic(track_id: int, device_index: int, device_name: str) -> bool:
    """
    指定トラックにマイクを割り当て、ストリームを開始する。
    既存のストリームがあれば停止してから新しいストリームを開始する。
    Returns:
        True: 成功, False: 失敗
    """
    with _streams_lock:
        # 既存ストリームを停止
        if track_id in _mic_streams:
            _mic_streams[track_id].stop()
            del _mic_streams[track_id]
        # 新しいストリームを開始
        stream = MicStream(device_index, device_name)
        ok = stream.start()
        if ok:
            _mic_streams[track_id] = stream
        return ok


def release_mic(track_id: int):
    """指定トラックのマイク割り当てを解除する。"""
    with _streams_lock:
        if track_id in _mic_streams:
            _mic_streams[track_id].stop()
            del _mic_streams[track_id]


def get_mic_stream(track_id: int) -> Optional[MicStream]:
    """指定トラックのMicStreamを返す（なければNone）。"""
    with _streams_lock:
        return _mic_streams.get(track_id)


def has_mic(track_id: int) -> bool:
    """指定トラックにマイクが割り当てられているか。"""
    with _streams_lock:
        return track_id in _mic_streams and _mic_streams[track_id].is_active


def stop_all():
    """全マイクストリームを停止する（アプリ終了時に呼ぶ）。"""
    with _streams_lock:
        for stream in _mic_streams.values():
            stream.stop()
        _mic_streams.clear()
