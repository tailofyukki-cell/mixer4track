"""
ストリーミング再生中のEQ切り替えをシミュレートして音途切れを検出するテスト。
実際の _stream_loop の動作を再現する。
"""
import threading
import time
import queue
import numpy as np
from eq_engine import EQEngine, EQParams, EQ_PRESETS
from audio_engine import CHUNK_SAMPLES, SAMPLE_RATE, QUEUE_AHEAD

sr = SAMPLE_RATE
chunk_size = CHUNK_SAMPLES

# 実際の音楽に近い信号（10秒分）
total_samples = sr * 10
t = np.arange(total_samples, dtype=np.float32) / sr
sig = (np.sin(2*np.pi*440*t) * 0.3 + np.sin(2*np.pi*880*t) * 0.2).reshape(-1, 1).repeat(2, axis=1)
sig = sig.astype(np.float32)

print(f'=== ストリーミングEQ切り替えシミュレーション ===')
print(f'チャンク長: {chunk_size/sr*1000:.0f}ms')
print(f'QUEUE_AHEAD: {QUEUE_AHEAD}')
print()


class FakeChannel:
    """pygameのChannelを模倣するクラス"""
    def __init__(self):
        self._playing = None
        self._queued = None
        self._lock = threading.Lock()
        self._play_start = None
        self._chunk_duration = chunk_size / sr  # 80ms

    def play(self, sound):
        with self._lock:
            self._playing = sound
            self._play_start = time.perf_counter()

    def queue(self, sound):
        with self._lock:
            self._queued = sound

    def get_queue(self):
        with self._lock:
            self._advance_if_needed()
            return self._queued

    def get_busy(self):
        with self._lock:
            self._advance_if_needed()
            return self._playing is not None

    def _advance_if_needed(self):
        """現在のチャンクが終了したら次のチャンクに進む"""
        if self._playing is not None and self._play_start is not None:
            elapsed = time.perf_counter() - self._play_start
            if elapsed >= self._chunk_duration:
                # 現在のチャンクが終了
                if self._queued is not None:
                    self._playing = self._queued
                    self._queued = None
                    self._play_start = time.perf_counter()
                else:
                    # キューが空 → 無音（音途切れ）
                    self._playing = None
                    self._play_start = None

    def get_playing(self):
        with self._lock:
            return self._playing


class FakeStreamer:
    """_TrackStreamerを模倣するクラス"""
    def __init__(self):
        self._pos = 0
        self._lock = threading.Lock()
        self._eq_engine = EQEngine(sr)

    def set_eq(self, params):
        with self._lock:
            self._eq_engine.set_params(params)

    def next_chunk(self):
        with self._lock:
            start = self._pos
            end = min(start + chunk_size, len(sig))
            if start >= len(sig):
                return None
            chunk = sig[start:end].copy()
            self._pos = end
            if len(chunk) < chunk_size:
                pad = np.zeros((chunk_size - len(chunk), 2), dtype=np.float32)
                chunk = np.concatenate([chunk, pad], axis=0)
            chunk = self._eq_engine.apply_eq(chunk)
            return chunk

    def is_finished(self):
        return self._pos >= len(sig)


def simulate_streaming(eq_change_at_sec: float, polling_interval_sec: float):
    """
    ストリーミング再生をシミュレートして、EQ切り替え時の音途切れを検出する。
    eq_change_at_sec: EQ切り替えを行う時刻（秒）
    polling_interval_sec: ポーリング間隔（秒）
    """
    streamer = FakeStreamer()
    channel = FakeChannel()
    param_changed_event = threading.Event()
    stop_event = threading.Event()
    silence_detected = []

    # 先読みキューを積む
    for _ in range(QUEUE_AHEAD):
        chunk = streamer.next_chunk()
        if chunk is not None:
            if not channel.get_busy():
                channel.play(chunk)
            else:
                channel.queue(chunk)

    # ストリーミングスレッド
    def stream_loop():
        while not stop_event.is_set():
            if streamer.is_finished():
                break
            if channel.get_queue() is None:
                chunk = streamer.next_chunk()
                if chunk is not None:
                    if not channel.get_busy():
                        channel.play(chunk)
                    else:
                        channel.queue(chunk)
            param_changed_event.wait(timeout=polling_interval_sec)
            param_changed_event.clear()

    stream_thread = threading.Thread(target=stream_loop, daemon=True)
    stream_thread.start()

    # 監視スレッド：無音（キューが空かつ再生中でない）を検出
    def monitor_loop():
        start_time = time.perf_counter()
        while not stop_event.is_set():
            elapsed = time.perf_counter() - start_time
            if elapsed > eq_change_at_sec + 1.0:
                break
            busy = channel.get_busy()
            queued = channel.get_queue()
            if not busy and elapsed > 0.1:  # 開始直後は無視
                silence_detected.append(elapsed)
            time.sleep(0.001)  # 1msごとに監視

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    # EQ切り替えをシミュレート
    time.sleep(eq_change_at_sec)
    streamer.set_eq(EQ_PRESETS['Presence'])
    param_changed_event.set()

    # 少し待ってから停止
    time.sleep(1.0)
    stop_event.set()
    stream_thread.join(timeout=2.0)
    monitor_thread.join(timeout=2.0)

    return silence_detected


print('テスト1: ポーリング間隔4ms（現在の設定）')
silences = simulate_streaming(eq_change_at_sec=0.5, polling_interval_sec=0.004)
if silences:
    print(f'  音途切れ検出: {len(silences)}回 (最初: {silences[0]:.3f}s)')
else:
    print(f'  音途切れなし ✓')

print()
print('テスト2: ポーリング間隔20ms（修正前の設定）')
silences2 = simulate_streaming(eq_change_at_sec=0.5, polling_interval_sec=0.020)
if silences2:
    print(f'  音途切れ検出: {len(silences2)}回 (最初: {silences2[0]:.3f}s)')
else:
    print(f'  音途切れなし ✓')

print()
print('テスト3: param_changed_event使用（現在の実装）')
# 現在の実装では param_changed_event.set() でストリーミングスレッドを即起動
# これにより EQ切り替え直後にキューを補充できる
silences3 = simulate_streaming(eq_change_at_sec=0.5, polling_interval_sec=0.004)
if silences3:
    print(f'  音途切れ検出: {len(silences3)}回 (最初: {silences3[0]:.3f}s)')
else:
    print(f'  音途切れなし ✓')

print()
print('=== 結論 ===')
print(f'ポーリング4ms: {"音途切れあり" if silences else "音途切れなし"}')
print(f'ポーリング20ms: {"音途切れあり" if silences2 else "音途切れなし"}')
