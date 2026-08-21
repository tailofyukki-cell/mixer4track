"""
test_headless.py
GUI なしで動作確認するヘッドレステスト。
Phase 4: 16トラック・バンク切り替え・自動保存（ProjectStore current_bank）テストを追加。
"""

import os
import sys
import math
import struct
import wave
import tempfile
import threading
import numpy as np

# SDL をダミーに設定（pygame の音声出力を無効化）
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["SDL_VIDEODRIVER"] = "dummy"

from track_model import TrackModel
from audio_engine import AudioEngine, CHUNK_SAMPLES, _TrackStreamer
from audio_param_broker import AudioParamBroker, ParamBatch, ParamPatch
from project_store import ProjectStore
from eq_engine import EQEngine, EQParams, EQ_PRESETS
from geq_engine import GEQParams
from automation_engine import AutomationManager


# ===========================================================================
# TrackModel テスト
# ===========================================================================
def test_track_model():
    print("=== TrackModel テスト ===")

    t = TrackModel(track_id=0)
    assert t.volume == 0.80
    assert t.pan == 0.0
    assert not t.muted
    assert not t.solo

    assert t.get_volume_percent() == 80
    db_str = t.get_volume_db()
    db_val = float(db_str.replace(" dB", ""))
    expected_db = 20 * math.log10(0.80)
    assert abs(db_val - expected_db) < 0.1

    t.pan = 0.5
    assert t.get_pan_display() == "R50"
    t.pan = -1.0
    assert t.get_pan_display() == "L100"
    t.pan = 0.0
    assert t.get_pan_display() == "C"

    t.muted = True
    assert not t.is_audible(False)
    t.muted = False
    t.solo = False
    assert not t.is_audible(True)
    t.solo = True
    assert t.is_audible(True)

    # to_dict / from_dict ラウンドトリップ
    t2 = TrackModel(track_id=15, volume=0.6, pan=-0.3, muted=True, solo=False)
    d = t2.to_dict()
    t3 = TrackModel.from_dict(d)
    assert t3.track_id == 15
    assert abs(t3.volume - 0.6) < 1e-6
    assert abs(t3.pan - (-0.3)) < 1e-6
    assert t3.muted is True

    print("  TrackModel: OK")


# ===========================================================================
# AudioEngine 16トラックテスト
# ===========================================================================
def test_audio_engine_16tracks():
    print("=== AudioEngine 16トラックテスト ===")

    engine = AudioEngine(num_tracks=16)
    assert engine.is_playing() is False

    # 存在しないファイルは False を返す
    ok = engine.load_file(0, "/nonexistent/file.wav")
    assert not ok

    # track_id 15 まで操作できる
    ok2 = engine.load_file(15, "/nonexistent/file.wav")
    assert not ok2

    # マスター音量
    engine.set_master_volume(1.2)
    assert abs(engine.get_master_volume() - 1.2) < 1e-6
    engine.set_master_volume(2.0)
    assert engine.get_master_volume() <= 1.5

    engine.cleanup()
    print("  AudioEngine 16トラック: OK")


# ===========================================================================
# ミックス書き出しテスト（Phase 2 / 16トラック）
# ===========================================================================
def _make_wav(path: str, freq: float = 440.0, duration: float = 1.5,
              amplitude: float = 0.3, sample_rate: int = 44100):
    n_samples = int(sample_rate * duration)
    with wave.open(path, 'w') as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = []
        for i in range(n_samples):
            val = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / sample_rate))
            frames.append(struct.pack('<hh', val, val))
        wf.writeframes(b''.join(frames))


def test_export_mix():
    print("=== ミックス書き出しテスト ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        wav1 = os.path.join(tmpdir, "track1.wav")
        wav2 = os.path.join(tmpdir, "track2.wav")
        _make_wav(wav1, freq=440.0, amplitude=0.2)
        _make_wav(wav2, freq=660.0, amplitude=0.2)

        engine = AudioEngine(num_tracks=16)
        tracks = [TrackModel(track_id=i) for i in range(16)]

        engine.load_file(0, wav1)
        engine.load_file(8, wav2)   # バンクBのトラック9（track_id=8）
        tracks[0].file_path = wav1
        tracks[8].file_path = wav2

        output = os.path.join(tmpdir, "mix.wav")
        result = engine.export_mix(tracks, output)
        assert result.success, f"書き出し失敗: {result.error_message}"
        assert result.duration_sec > 0
        assert os.path.isfile(output)
        print(f"    duration={result.duration_sec:.2f}s  peak={result.peak_level:.3f}")

        # 空トラック
        engine2 = AudioEngine(num_tracks=16)
        empty_tracks = [TrackModel(track_id=i) for i in range(16)]
        result_empty = engine2.export_mix(empty_tracks, "/tmp/empty_16.wav")
        assert not result_empty.success
        print(f"    empty: error='{result_empty.error_message}'")
        engine2.cleanup()

        engine.cleanup()
    print("  ミックス書き出し: OK")


# ===========================================================================
# ProjectStore 16トラック・バンクテスト（Phase 4）
# ===========================================================================
def test_project_store_16tracks():
    print("=== ProjectStore 16トラック・バンクテスト ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test16.m4t")
        store = ProjectStore(project_path=path)

        # 16トラック作成
        tracks = [
            TrackModel(track_id=i, volume=0.5 + i * 0.02, pan=(i - 8) * 0.05)
            for i in range(16)
        ]
        tracks[0].muted = True
        tracks[8].solo = True
        master_vol = 1.1
        current_bank = 1  # バンクB

        ok = store.save(tracks, master_volume=master_vol, current_bank=current_bank)
        assert ok, "保存失敗"
        assert os.path.isfile(path)
        print(f"    saved: {path}")

        # 読み込み
        result = store.load()
        assert result is not None, "読み込み失敗"
        loaded_tracks, loaded_master, loaded_bank, _marker_data = result

        assert len(loaded_tracks) == 16, f"トラック数不一致: {len(loaded_tracks)}"
        assert abs(loaded_master - master_vol) < 1e-4
        assert loaded_bank == current_bank, f"バンク不一致: {loaded_bank}"
        assert loaded_tracks[0].muted is True
        assert loaded_tracks[8].solo is True
        assert loaded_tracks[15].track_id == 15
        print(f"    loaded: {len(loaded_tracks)} tracks, master={loaded_master:.2f}, bank={loaded_bank}")

        # バンクA保存
        path2 = os.path.join(tmpdir, "bankA.m4t")
        store2 = ProjectStore(project_path=path2)
        store2.save(tracks, master_volume=1.0, current_bank=0)
        r2 = store2.load()
        assert r2 is not None
        _, _, bank2, _m = r2
        assert bank2 == 0, f"バンクA不一致: {bank2}"
        print(f"    bank A: OK (bank={bank2})")

        # 存在しないファイル
        store_missing = ProjectStore(project_path=os.path.join(tmpdir, "nope.m4t"))
        assert store_missing.load() is None
        print("    missing file: OK")

    print("  ProjectStore 16トラック・バンク: OK")


# ===========================================================================
# EQカーブ表示テスト（Phase 5修正）
# ===========================================================================
def test_eq_curve():
    print("=== EQカーブ表示テスト ===")
    from eq_engine import get_response_db

    # フラット時は全点 0.0dB
    p_flat = EQParams()
    r_flat = get_response_db(p_flat, n_points=100)
    assert len(r_flat) == 100
    assert all(abs(g) < 0.01 for _, g in r_flat), "フラット時は全て 0dB 期待"
    print("    flat: all 0dB - OK")

    # Low +10dB: 20Hz仙8近は +10dB近くなるはず
    p_low = EQParams(low_gain_db=10.0)
    r_low = get_response_db(p_low, n_points=200)
    # 最低周波数のゲインは +10dB近い値のはず
    low_gain = r_low[0][1]
    assert low_gain > 8.0, f"Low +10dB: 20Hz gain={low_gain:.1f}dB"
    print(f"    Low +10dB: 20Hz={low_gain:.1f}dB - OK")

    # High +8dB: 20kHz仙8近は +8dB近くなるはず
    p_high = EQParams(high_gain_db=8.0)
    r_high = get_response_db(p_high, n_points=200)
    high_gain = r_high[-1][1]
    assert high_gain > 6.0, f"High +8dB: 20kHz gain={high_gain:.1f}dB"
    print(f"    High +8dB: 20kHz={high_gain:.1f}dB - OK")

    # Mid peak: 1kHz仙8近にピークが出るはず
    p_mid = EQParams(mid_gain_db=8.0, mid_freq_hz=1000.0)
    r_mid = get_response_db(p_mid, n_points=200)
    # 1kHz仙8近のインデックスを探す
    mid_idx = min(range(len(r_mid)), key=lambda i: abs(r_mid[i][0] - 1000.0))
    mid_gain = r_mid[mid_idx][1]
    assert mid_gain > 5.0, f"Mid +8dB@1kHz: gain={mid_gain:.1f}dB"
    print(f"    Mid +8dB@1kHz: gain={mid_gain:.1f}dB - OK")

    print("  EQカーブ表示: OK")


# ===========================================================================
# EQEngine テスト（Phase 5）
# ===========================================================================
def test_eq_engine():
    print("=== EQEngine テスト ===")
    import numpy as np

    sr = 44100
    engine = EQEngine(sr)
    t = np.linspace(0, 1, sr, dtype=np.float32)
    sig = (np.sin(2 * math.pi * 440 * t) * 0.5).astype(np.float32)
    stereo = np.stack([sig, sig], axis=1)

    # フラット = バイパス
    p = EQParams()
    engine.set_params(p)
    out = engine.apply_eq(stereo)
    assert np.array_equal(out, stereo), "Flat bypass failed"
    print("  flat bypass: OK")

    # Low shelf +6dB
    p = EQParams(low_gain_db=6.0)
    engine.set_params(p)
    out = engine.apply_eq(stereo)
    assert out.max() > sig.max(), "Low +6dB should boost"
    print(f"  Low +6dB: max={out.max():.4f} OK")

    # is_flat
    assert EQParams().is_flat()
    assert not EQParams(low_gain_db=1.0).is_flat()
    print("  is_flat: OK")

    # clamp
    p2 = EQParams(low_gain_db=100.0, mid_gain_db=-100.0, mid_freq_hz=50.0)
    p2.clamp()
    assert p2.low_gain_db == 15.0
    assert p2.mid_gain_db == -15.0
    assert p2.mid_freq_hz == 250.0
    print("  clamp: OK")

    # 全プリセット動作確認
    for name, preset in EQ_PRESETS.items():
        engine.set_params(preset)
        out = engine.apply_eq(stereo)
        assert out is not None
    print(f"  All {len(EQ_PRESETS)} presets: OK")

    print("  EQEngine: OK")


# ===========================================================================
# AudioEngine EQ テスト（Phase 5）
# ===========================================================================
def test_audio_engine_eq():
    print("=== AudioEngine EQ テスト ===")
    import numpy as np

    engine = AudioEngine(num_tracks=16)

    # 未ロードトラックの EQ 更新は例外なし
    params = EQParams(low_gain_db=6.0)
    engine.update_eq(0, params)
    print("  update_eq (no file): OK")

    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "test_eq.wav")
        _make_wav(wav_path, freq=440.0, amplitude=0.3, duration=0.5)

        ok = engine.load_file(0, wav_path)
        assert ok, "load_file failed"
        print("  load_file: OK")

        # PCMデータが保持されているか確認（新方式では_pcm_dataに格納）
        engine.update_eq(0, EQParams(low_gain_db=6.0))
        pcm = engine._pcm_data.get(0)
        assert pcm is not None, "pcm_data should be stored after load_file"
        assert pcm.shape[1] == 2, "pcm should be stereo"
        print("  update_eq (with file): OK")

        # EQパラメータが保持されているか
        engine.update_eq(0, EQParams())
        stored_params = engine._eq_params.get(0)
        assert stored_params is not None, "eq_params should be stored"
        assert stored_params.is_flat(), "flat EQ should be stored"
        print("  flat EQ bypass: OK")

        # 音声長の確認
        dur = engine.get_sound_duration(0)
        assert dur > 0.4, f"duration should be ~0.5s, got {dur}"
        print(f"  get_sound_duration: {dur:.2f}s OK")

    engine.cleanup()
    print("  AudioEngine EQ: OK")


# ===========================================================================
# ProjectStore EQパラメータ保存/読み込みテスト（Phase 5）
# ===========================================================================
def test_project_store_eq():
    print("=== ProjectStore EQ保存/読み込みテスト ===")

    tracks = [TrackModel(track_id=i) for i in range(16)]
    tracks[0].eq_low_gain  = 6.0
    tracks[0].eq_mid_gain  = -3.0
    tracks[0].eq_mid_freq  = 2000.0
    tracks[0].eq_mid_q     = 2.0
    tracks[0].eq_high_gain = 4.0

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_eq.m4t")
        store = ProjectStore(project_path=path)

        ok = store.save(tracks, master_volume=1.2, current_bank=1)
        assert ok, "save failed"
        print("  save: OK")

        result = store.load()
        assert result is not None, "load failed"
        loaded_tracks, master_vol, bank, _markers = result
        assert abs(master_vol - 1.2) < 0.001
        assert bank == 1
        assert len(loaded_tracks) == 16
        t0 = loaded_tracks[0]
        assert abs(t0.eq_low_gain  - 6.0)  < 0.001, f"low: {t0.eq_low_gain}"
        assert abs(t0.eq_mid_gain  - (-3.0)) < 0.001, f"mid: {t0.eq_mid_gain}"
        assert abs(t0.eq_mid_freq  - 2000.0) < 0.001, f"freq: {t0.eq_mid_freq}"
        assert abs(t0.eq_mid_q     - 2.0)  < 0.001, f"q: {t0.eq_mid_q}"
        assert abs(t0.eq_high_gain - 4.0)  < 0.001, f"high: {t0.eq_high_gain}"
        print("  load EQ params: OK")

    print("  ProjectStore EQ: OK")


# ===========================================================================
# Phase 6: EffectEngineテスト
# ===========================================================================
def test_effect_engine():
    print("=== EffectEngineテスト ===")
    import numpy as np
    from effect_engine import EffectEngine, EFFECT_PRESETS, EFFECT_CATEGORIES

    # プリセットカタログの確認
    expected_types = {"reverb", "delay", "compressor", "distortion", "chorus", "limiter", "bypass"}
    for name, preset in EFFECT_PRESETS.items():
        assert preset.effect_type in expected_types, f"{name}: unknown type {preset.effect_type}"
    print(f"  プリセット数: {len(EFFECT_PRESETS)} - OK")

    engine = EffectEngine(sample_rate=44100)
    # 1秒分のサイン波テスト信号
    t = np.linspace(0, 1.0, 44100, dtype=np.float32)
    sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    pcm = np.stack([sine, sine], axis=1)  # shape (44100, 2)

    # None (バイパス): 入力と同じはず
    out_none = engine.apply(pcm, "None")
    assert np.allclose(out_none, pcm, atol=1e-5), "bypass should return original"
    print("  bypass: OK")

    # 各エフェクトがクラッシュせず実行できるか確認
    test_presets = [
        "Reverb: Room", "Delay: Short", "Comp: Gentle",
        "Dist: Soft", "Chorus: Light", "Limiter: -3dB"
    ]
    for preset_name in test_presets:
        out = engine.apply(pcm.copy(), preset_name)
        assert out.shape == pcm.shape, f"{preset_name}: shape mismatch"
        assert out.dtype == np.float32, f"{preset_name}: dtype mismatch"
        assert np.all(np.isfinite(out)), f"{preset_name}: NaN or Inf detected"
        print(f"  {preset_name}: OK")

    # Limiter: 出力が天井以内に収まるか
    import math
    ceiling_lin = 10 ** (-3.0 / 20.0)
    out_lim = engine.apply(pcm.copy(), "Limiter: -3dB")
    assert float(np.max(np.abs(out_lim))) <= ceiling_lin + 1e-5, "limiter ceiling exceeded"
    print("  Limiter ceiling: OK")

    print("  EffectEngine: OK")


# ===========================================================================
# Phase 6: ProjectStoreエフェクト保存/読み込みテスト
# ===========================================================================
def test_project_store_effect():
    print("=== ProjectStoreエフェクト保存/読み込みテスト ===")
    import tempfile, os
    from track_model import TrackModel
    from project_store import ProjectStore

    tracks = [TrackModel(track_id=i) for i in range(16)]
    # 全トラックにエフェクトを設定
    for t in tracks:
        t.effect_preset  = "Reverb: Hall"
        t.effect_enabled = True

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_fx.m4t")
        store = ProjectStore(project_path=path)

        ok = store.save(tracks, master_volume=1.0, current_bank=0)
        assert ok, "save failed"
        print("  save: OK")

        result = store.load()
        assert result is not None, "load failed"
        loaded_tracks, _, _, _m = result
        assert len(loaded_tracks) == 16
        for i, t in enumerate(loaded_tracks):
            assert t.effect_preset  == "Reverb: Hall",  f"track {i}: preset mismatch"
            assert t.effect_enabled == True,             f"track {i}: enabled mismatch"
        print("  load effect params: OK")

    print("  ProjectStore Effect: OK")


# ===========================================================================
# Phase 7: ゲイン機能テスト
# ===========================================================================
def test_gain_model():
    print("=== Phase 7: ゲイン機能テスト ===")
    from track_model import TrackModel
    from project_store import ProjectStore
    import tempfile, os

    # gain_dbフィールドの初期値確認
    t = TrackModel(track_id=0)
    assert t.gain_db == 0.0, f"gain_db初期値エラー: {t.gain_db}"
    print("  gain_db初期値: OK")

    # 範囲内の値を設定
    t.gain_db = 12.0
    assert t.gain_db == 12.0
    t.gain_db = -24.0
    assert t.gain_db == -24.0
    print("  範囲設定: OK")

    # to_dict/from_dictの往復
    d = t.to_dict()
    assert "gain_db" in d, "gain_dbがto_dictにない"
    t2 = TrackModel.from_dict(d)
    assert t2.gain_db == -24.0, f"from_dict後のgain_dbエラー: {t2.gain_db}"
    print("  to_dict/from_dict: OK")

    # ProjectStoreでの保存/読み込み
    tracks = [TrackModel(track_id=i) for i in range(16)]
    tracks[3].gain_db = 6.0
    tracks[7].gain_db = -12.0
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test_gain.m4t")
        store = ProjectStore(path)
        store.save(tracks)
        result = store.load()
        assert result is not None
        loaded_tracks, _, _, _m = result
        assert abs(loaded_tracks[3].gain_db - 6.0) < 0.001, f"ゲイン保存エラー: {loaded_tracks[3].gain_db}"
        assert abs(loaded_tracks[7].gain_db - (-12.0)) < 0.001, f"ゲイン保存エラー: {loaded_tracks[7].gain_db}"
        assert loaded_tracks[0].gain_db == 0.0, f"デフォルトゲインエラー: {loaded_tracks[0].gain_db}"
    print("  ProjectStore gain保存/読み込み: OK")

    print("  GainModel: OK")


# ===========================================================================
# 複数トラック同時再生テスト
# ===========================================================================
def test_multi_track_playback():
    """複数トラックが1本のマスター合成チャンネルで再生されることを確認する。"""
    import time
    print("=== 複数トラック同時再生テスト ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        # 3トラック分のWAVを作成
        wav_paths = []
        for i in range(3):
            path = os.path.join(tmpdir, f"track{i}.wav")
            _make_wav(path, freq=440 + i * 110, duration=1.0)
            wav_paths.append(path)

        engine = AudioEngine(num_tracks=16)
        if not engine._initialized:
            print("  pygame未初期化のためskip")
            return

        tracks = [TrackModel(track_id=i) for i in range(3)]
        for i, path in enumerate(wav_paths):
            ok = engine.load_file(i, path)
            assert ok, f"Track {i} 読み込み失敗"

        try:
            engine.play_all(tracks)
            time.sleep(0.2)  # マスター合成ストリームが走るまで待機

            # Phase 23: すべてのファイルトラックは同じ最終ミックスチャンネルへ合成される。
            ch0 = engine._channels.get(0)
            ch1 = engine._channels.get(1)
            ch2 = engine._channels.get(2)
            assert ch0 is not None, "Track 0 のマスターチャンネルが割り当てられていない"
            assert ch1 is not None, "Track 1 のマスターチャンネルが割り当てられていない"
            assert ch2 is not None, "Track 2 のマスターチャンネルが割り当てられていない"
            assert ch0 is ch1 is ch2, "トラックが単一マスター合成チャンネルを共有していない"
            assert engine._master_channel is ch0, "マスターチャンネル参照が一致しない"
        finally:
            engine.stop_all()
    print("  複数トラック・マスター合成再生: OK")


# ===========================================================================
# Phase 23: マスター・リミッターテスト
# ===========================================================================
def test_master_limiter():
    """ステレオリンク・リミッターのceiling、GR、EXPORT WAV反映を検証する。"""
    from effect_engine import MasterLimiter
    print("=== Phase 23: マスター・リミッター テスト ===")

    ceiling_db = -1.0
    ceiling = 10 ** (ceiling_db / 20.0)
    limiter = MasterLimiter(sample_rate=44100, release_ms=120.0)
    hot_pcm = np.full((2048, 2), 1.35, dtype=np.float32)
    limited, reduction = limiter.process(hot_pcm, ceiling_db)
    assert float(np.max(np.abs(limited))) <= ceiling + 1e-5, "ceilingを超える出力が残った"
    assert reduction > 0.1, "過大入力でゲインリダクションが発生しない"
    print(f"  DSP ceiling: peak={np.max(np.abs(limited)):.4f} <= {ceiling:.4f}, GR={reduction:.2f}dB - OK")

    with tempfile.TemporaryDirectory() as tmpdir:
        path_a = os.path.join(tmpdir, "hot_a.wav")
        path_b = os.path.join(tmpdir, "hot_b.wav")
        output = os.path.join(tmpdir, "limited_master.wav")
        _make_wav(path_a, freq=440, duration=0.5, amplitude=0.90)
        _make_wav(path_b, freq=440, duration=0.5, amplitude=0.90)
        engine = AudioEngine(num_tracks=2)
        if not engine._initialized:
            print("  pygame未初期化のためexport部分skip")
            return
        try:
            assert engine.load_file(0, path_a)
            assert engine.load_file(1, path_b)
            engine.set_master_limiter(True, ceiling_db)
            result = engine.export_mix([
                TrackModel(track_id=0, volume=1.0),
                TrackModel(track_id=1, volume=1.0),
            ], output)
            assert result.success and os.path.isfile(output), "リミッター有効のWAV書き出しに失敗"
            with wave.open(output, "rb") as wf:
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            output_peak = float(np.max(np.abs(data))) / 32767.0
            assert output_peak <= ceiling + 1e-3, f"EXPORT WAVがceilingを超えた: {output_peak:.4f}"
            print(f"  EXPORT WAV ceiling: peak={output_peak:.4f} <= {ceiling:.4f} - OK")
        finally:
            engine.cleanup()

        project_path = os.path.join(tmpdir, "limiter_settings.m4t")
        store = ProjectStore(project_path=project_path)
        assert store.save([], master_limiter={
            "enabled": False, "ceiling_db": -3.0, "release_ms": 250.0
        }), "リミッター設定のプロジェクト保存に失敗"
        assert store.load() is not None, "リミッター設定のプロジェクト読み込みに失敗"
        stored_state = store.get_master_limiter_state()
        assert stored_state["enabled"] is False
        assert abs(stored_state["ceiling_db"] - (-3.0)) < 0.001
        assert abs(stored_state["release_ms"] - 250.0) < 0.001
        print("  ProjectStore limiter settings: OK")
    print("  マスター・リミッター: OK")


# ===========================================================================
# Phase 22: ループ再生テスト
# ===========================================================================
def test_loop_playback():
    """全体/範囲ループの状態管理と再生継続を検証する。"""
    import time
    print("=== Phase 22: ループ再生テスト ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "loop_source.wav")
        _make_wav(wav_path, duration=0.35, amplitude=0.25)

        engine = AudioEngine(num_tracks=1)
        if not engine._initialized:
            print("  pygame未初期化のためskip")
            return
        assert engine.load_file(0, wav_path), "ループテスト音源の読み込み失敗"
        assert 0.34 <= engine.get_timeline_duration_sec() <= 0.36

        # 不正な範囲は拒否される
        assert engine.set_loop_range(0.20, 0.10) is False

        # 指定範囲ループ（再生前に設定しても有効）
        assert engine.set_loop_range(0.05, 0.15) is True
        active, start, end = engine.get_loop_range()
        assert active is True
        assert abs(start - 0.05) < 0.001
        assert abs(end - 0.15) < 0.001

        engine.play_all([TrackModel(track_id=0)])
        time.sleep(0.45)  # 元音源の長さより長く待ち、繰り返しを確認
        assert engine.is_playing(), "範囲ループ中に再生が終了した"
        pos = engine.get_track_position_sec(0)
        assert 0.05 <= pos <= 0.15, f"再生位置がループ範囲外: {pos:.3f}s"

        # 共通シークはループ範囲内へ丸められる
        engine.seek_all_tracks(0.12)
        time.sleep(0.05)
        pos = engine.get_track_position_sec(0)
        assert 0.05 <= pos <= 0.15, f"共通シーク後の位置が範囲外: {pos:.3f}s"

        engine.clear_loop_range()
        assert engine.is_loop_enabled() is False
        engine.stop_all()
    print("  ループ再生: OK")


# ===========================================================================
# Phase 24A: AudioParamBroker テスト
# ===========================================================================
def test_audio_param_broker():
    print("=== AudioParamBroker テスト ===")

    broker = AudioParamBroker(num_tracks=2)
    initial = broker.snapshot()
    assert initial.generation == 0
    assert initial.track_for(0).volume == 0.80

    # 2パラメータを同一generationに原子的に反映する。
    generation = broker.submit_batch(ParamBatch((
        ParamPatch(("track", 0, "volume"), 0.25),
        ParamPatch(("track", 0, "pan"), 0.75),
    )))
    updated = broker.take_snapshot(initial.generation)
    assert updated is not None
    assert updated.generation == generation
    assert abs(updated.track_for(0).volume - 0.25) < 1e-6
    assert abs(updated.track_for(0).pan - 0.75) < 1e-6

    # 同一キーへの高頻度更新は最新値へ圧縮する。
    for volume in (0.10, 0.30, 0.55, 0.90):
        broker.submit_track_mix(0, volume=volume)
    compressed = broker.snapshot()
    assert abs(compressed.track_for(0).volume - 0.90) < 1e-6

    # ManualはAutomationより優先し、低優先の古い意図で値が戻らない。
    broker.submit_track_mix(1, pan=-0.80, source="automation")
    broker.submit_track_mix(1, pan=0.20, source="manual")
    broker.submit_track_mix(1, pan=-0.50, source="automation")
    assert abs(broker.snapshot().track_for(1).pan - 0.20) < 1e-6

    # 異なる操作を別スレッドから同時に送っても、最後の意図が両方残る。
    start = threading.Event()
    failures = []

    def update_volume():
        try:
            start.wait()
            for value in np.linspace(0.05, 0.95, 120):
                broker.submit_track_mix(0, volume=float(value))
        except Exception as exc:
            failures.append(exc)

    def update_pan():
        try:
            start.wait()
            for value in np.linspace(-0.95, 0.75, 120):
                broker.submit_track_mix(0, pan=float(value))
        except Exception as exc:
            failures.append(exc)

    volume_thread = threading.Thread(target=update_volume)
    pan_thread = threading.Thread(target=update_pan)
    volume_thread.start()
    pan_thread.start()
    start.set()
    volume_thread.join(timeout=2.0)
    pan_thread.join(timeout=2.0)
    assert not volume_thread.is_alive() and not pan_thread.is_alive()
    assert not failures
    concurrent = broker.snapshot().track_for(0)
    assert abs(concurrent.volume - 0.95) < 1e-6
    assert abs(concurrent.pan - 0.75) < 1e-6

    # 世代番号により通知を失っても更新を検出できる。
    last_generation = broker.current_generation()
    broker.submit_master_volume(0.65)
    assert broker.wait_for_generation(last_generation, 0.0)
    assert abs(broker.snapshot().master.volume - 0.65) < 1e-6

    # Transport Epoch前のPatchは破棄する。
    stale_epoch = broker.current_transport_epoch()
    broker.begin_transport_epoch()
    current = broker.snapshot().track_for(0).muted
    broker.submit_track_mix(0, muted=not current, transport_epoch=stale_epoch)
    assert broker.snapshot().track_for(0).muted is current

    # DSP Patchは複合状態としてSnapshotに格納される。
    eq = EQParams(low_gain_db=6.0, mid_gain_db=-2.0, mid_freq_hz=1800.0,
                  mid_q=1.5, high_gain_db=3.0)
    geq = GEQParams()
    geq.set_gain(1000.0, 4.0)
    broker.submit_track_dsp(
        0, gain_db=8.0, eq_params=eq, effect_preset="Reverb: Room",
        effect_enabled=True, aux_enabled=True,
    )
    broker.submit_master_geq(geq)
    dsp_snapshot = broker.snapshot()
    track_dsp = dsp_snapshot.track_for(0).dsp
    assert abs(track_dsp.gain_db - 8.0) < 1e-6
    assert abs(track_dsp.eq.low_gain_db - 6.0) < 1e-6
    assert track_dsp.effect_preset == "Reverb: Room"
    assert track_dsp.effect_enabled and track_dsp.aux_enabled
    assert abs(dsp_snapshot.master.dsp.geq.to_params().get_gain(1000.0) - 4.0) < 1e-6

    # Phase 25: X-FADER状態はA/B/THRU割当とともに不変Snapshotへ格納される。
    broker.submit_track_xfade_assign(0, "A")
    broker.submit_track_xfade_assign(1, "B")
    broker.submit_master_xfade(position=0.0, curve="equal_power", cut_a=False, cut_b=False)
    xfade_snapshot = broker.snapshot()
    assert xfade_snapshot.track_for(0).xfade_assign == "A"
    assert xfade_snapshot.track_for(1).xfade_assign == "B"
    assert abs(xfade_snapshot.master.xfade.position - 0.0) < 1e-6
    assert xfade_snapshot.master.xfade.curve == "equal_power"

    # DSP系の複数操作も、キー別の最新値へ圧縮され一つのSnapshotに収束する。
    def update_track_dsp():
        for index in range(40):
            broker.submit_track_dsp(
                0,
                gain_db=-12.0 + index * 0.5,
                eq_params=EQParams(mid_gain_db=float(index % 12)),
                effect_preset="Delay: Short" if index % 2 else "Reverb: Room",
                effect_enabled=True,
                aux_enabled=True,
            )

    def update_master_geq():
        for index in range(40):
            params = GEQParams()
            params.set_gain(1000.0, -8.0 + index * 0.25)
            broker.submit_master_geq(params)

    dsp_threads = [threading.Thread(target=update_track_dsp),
                   threading.Thread(target=update_master_geq)]
    for worker in dsp_threads:
        worker.start()
    for worker in dsp_threads:
        worker.join()
    concurrent_dsp = broker.snapshot()
    assert abs(concurrent_dsp.track_for(0).dsp.gain_db - 7.5) < 1e-6
    assert concurrent_dsp.track_for(0).dsp.effect_preset == "Delay: Short"
    assert abs(concurrent_dsp.master.dsp.geq.to_params().get_gain(1000.0) - 1.75) < 1e-6
    print("  AudioParamBroker: OK")


def test_audio_engine_broker_integration():
    print("=== AudioEngine Broker 統合テスト ===")
    engine = AudioEngine(num_tracks=2)
    try:
        tracks = [TrackModel(track_id=0, volume=0.80, pan=0.0)]
        pcm = np.full((CHUNK_SAMPLES * 3, 2), 0.5, dtype=np.float32)
        streamers = {0: _TrackStreamer(0, pcm, engine.SAMPLE_RATE)}
        engine._param_broker.reset_from_tracks(tracks, 1.0)
        initial = engine._param_broker.snapshot()

        mix, has_data = engine._render_master_mix_chunk(tracks, streamers, initial)
        assert has_data and mix is not None
        left, right = engine._calc_pan_volumes(0.80, 0.0)
        assert np.allclose(mix[0], [0.5 * left, 0.5 * right], atol=1e-6)

        # UIモデル変更はBrokerを通り、次Snapshotから音声ミックスへ反映される。
        tracks[0].volume = 0.25
        tracks[0].pan = 1.0
        engine.update_track(tracks[0], False)
        changed = engine._param_broker.take_snapshot(initial.generation)
        assert changed is not None
        assert abs(changed.track_for(0).volume - 0.25) < 1e-6
        assert abs(changed.track_for(0).pan - 1.0) < 1e-6

        mix, has_data = engine._render_master_mix_chunk(tracks, streamers, changed)
        assert has_data and mix is not None
        left, right = engine._calc_pan_volumes(0.25, 1.0)
        assert np.allclose(mix[0], [0.5 * left, 0.5 * right], atol=1e-6)

        engine.set_master_volume(0.40)
        master_changed = engine._param_broker.take_snapshot(changed.generation)
        assert master_changed is not None
        assert abs(master_changed.master.volume - 0.40) < 1e-6

        # Phase 25: Aは左端、Bは右端で無音化し、THRUは常に通過する。
        tracks[0].xfade_assign = "A"
        engine.set_track_xfade_assign(0, "A")
        engine.set_master_xfade(position=1.0, curve="equal_power", cut_a=False, cut_b=False)
        xfade_changed = engine._param_broker.take_snapshot(master_changed.generation)
        assert xfade_changed is not None
        xfade_streamer = {0: _TrackStreamer(0, pcm, engine.SAMPLE_RATE)}
        mix, has_data = engine._render_master_mix_chunk(tracks, xfade_streamer, xfade_changed)
        assert has_data and mix is not None
        assert float(np.max(np.abs(mix))) < 1e-5

        engine.set_master_xfade(position=0.5, curve="linear", cut_a=False, cut_b=False)
        linear_changed = engine._param_broker.take_snapshot(xfade_changed.generation)
        assert linear_changed is not None
        linear_streamer = {0: _TrackStreamer(0, pcm, engine.SAMPLE_RATE)}
        mix, has_data = engine._render_master_mix_chunk(tracks, linear_streamer, linear_changed)
        assert has_data and mix is not None
        left, right = engine._calc_pan_volumes(0.25, 1.0)
        assert np.allclose(mix[0], [0.5 * left * 0.5, 0.5 * right * 0.5], atol=1e-6)

        # Phase 24B: GAIN・EQ・FX・MASTER GEQは音声スレッド側のDSP適用で反映される。
        eq = EQParams(low_gain_db=4.0)
        geq = GEQParams()
        geq.set_gain(1000.0, 3.0)
        engine.update_gain(0, 6.0)
        engine.update_eq(0, eq)
        engine.update_effect(0, "Reverb: Room", True)
        engine.set_aux_track(0, True)
        engine.update_master_geq(geq)
        dsp_changed = engine._param_broker.take_snapshot(master_changed.generation)
        assert dsp_changed is not None
        dsp = dsp_changed.track_for(0).dsp
        assert abs(dsp.gain_db - 6.0) < 1e-6
        assert abs(dsp.eq.low_gain_db - 4.0) < 1e-6
        assert dsp.effect_enabled and dsp.aux_enabled

        dsp_streamer = _TrackStreamer(0, pcm, engine.SAMPLE_RATE)
        dsp_streamer.apply_dsp_params(dsp)
        dsp_chunk = dsp_streamer.next_chunk()
        assert dsp_chunk is not None
        # +6dB GAINとEQ/FX設定が適用済みで、無音ではないことを確認する。
        assert float(np.max(np.abs(dsp_chunk))) > 0.5

        engine._apply_master_dsp_snapshot(dsp_changed)
        assert engine._master_geq_crossfade_remaining > 0
        master_geq_out = engine._apply_master_geq_chunk(
            np.full((CHUNK_SAMPLES, 2), 0.25, dtype=np.float32)
        )
        assert master_geq_out.shape == (CHUNK_SAMPLES, 2)
        assert engine._master_geq_crossfade_remaining == 0
    finally:
        engine.cleanup()
    print("  AudioEngine Broker統合: OK")


# ===========================================================================
# Phase 25: X-FADER 保存・復元テスト
# ===========================================================================
def test_xfade_project_store():
    print("=== Phase 25: X-FADER保存/読み込みテスト ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "xfade_project.m4t")
        tracks = [TrackModel(track_id=0), TrackModel(track_id=1)]
        tracks[0].xfade_assign = "A"
        tracks[1].xfade_assign = "B"
        xfade = {
            "position": 0.73,
            "curve": "linear",
            "cut_a": True,
            "cut_b": False,
        }
        store = ProjectStore(project_path=path)
        assert store.save(tracks, master_xfade=xfade)
        loaded = store.load()
        assert loaded is not None
        loaded_tracks, _master, _bank, _markers = loaded
        assert loaded_tracks[0].xfade_assign == "A"
        assert loaded_tracks[1].xfade_assign == "B"
        loaded_xfade = store.get_master_xfade_state()
        assert abs(loaded_xfade["position"] - 0.73) < 1e-6
        assert loaded_xfade["curve"] == "linear"
        assert loaded_xfade["cut_a"] is True
        assert loaded_xfade["cut_b"] is False
    print("  X-FADER保存/読み込み: OK")


# ===========================================================================
# Phase 26: EQ Snapshot A/B・Morph テスト
# ===========================================================================
def test_eq_snap_morph():
    print("=== Phase 26: EQ Snapshot A/B・Morphテスト ===")
    snap_a = EQParams(low_gain_db=-6.0, mid_gain_db=-3.0, mid_freq_hz=500.0, mid_q=0.7, high_gain_db=-4.0)
    snap_b = EQParams(low_gain_db=6.0, mid_gain_db=9.0, mid_freq_hz=2000.0, mid_q=2.8, high_gain_db=8.0)
    track = TrackModel(track_id=0)
    track.eq_snap_a = {
        "low_gain_db": -6.0, "mid_gain_db": -3.0, "mid_freq_hz": 500.0,
        "mid_q": 0.7, "high_gain_db": -4.0,
    }
    track.eq_snap_b = {
        "low_gain_db": 6.0, "mid_gain_db": 9.0, "mid_freq_hz": 2000.0,
        "mid_q": 2.8, "high_gain_db": 8.0,
    }
    track.eq_morph_position = 0.5
    track.eq_morph_enabled = True

    broker = AudioParamBroker(num_tracks=1)
    broker.reset_from_tracks([track], 1.0, eq_by_track={0: snap_a})
    initial = broker.snapshot().track_for(0).dsp
    assert initial.eq_morph_enabled
    assert abs(initial.eq_snap_a.low_gain_db + 6.0) < 1e-6
    assert abs(initial.eq_snap_b.high_gain_db - 8.0) < 1e-6

    broker.submit_track_dsp(
        0, eq_snap_a=snap_a, eq_snap_b=snap_b,
        eq_morph_position=0.5, eq_morph_enabled=True,
    )
    dsp = broker.snapshot().track_for(0).dsp
    morphed = dsp.eq_snap_a.interpolate(dsp.eq_snap_b, dsp.eq_morph_position)
    assert abs(morphed.low_gain_db - 0.0) < 1e-6
    assert abs(morphed.high_gain_db - 2.0) < 1e-6
    assert abs(morphed.mid_freq_hz - 1000.0) < 1e-6  # log補間: sqrt(500 * 2000)
    assert abs(morphed.mid_q - 1.4) < 1e-6

    streamer = _TrackStreamer(0, np.full((CHUNK_SAMPLES, 2), 0.2, dtype=np.float32), 44100)
    streamer.apply_dsp_params(dsp)
    assert abs(streamer._eq_params.mid_freq_hz - 1000.0) < 1e-6
    assert abs(streamer._eq_params.high_gain_db - 2.0) < 1e-6

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "eq_morph_project.m4t")
        store = ProjectStore(project_path=path)
        assert store.save([track])
        loaded = store.load()
        assert loaded is not None
        loaded_track = loaded[0][0]
        assert loaded_track.eq_snap_a["mid_freq_hz"] == 500.0
        assert loaded_track.eq_snap_b["mid_freq_hz"] == 2000.0
        assert abs(loaded_track.eq_morph_position - 0.5) < 1e-6
        assert loaded_track.eq_morph_enabled
    print("  EQ Snapshot A/B・Morph: OK")


# ===========================================================================
# Phase 27: オートメーション テスト
# ===========================================================================
def test_automation():
    print("=== Phase 27: オートメーションテスト ===")
    manager = AutomationManager()
    manager.record_track(0, "volume", 0.0, 0.20)
    manager.record_track(0, "volume", 2.0, 0.80)
    manager.record_track(0, "pan", 0.0, -1.0)
    manager.record_track(0, "pan", 2.0, 1.0)
    manager.record_master("xfade_position", 0.0, 0.10)
    manager.record_master("xfade_position", 2.0, 0.90)
    assert manager.values_at(1.0) == ({}, {})  # AUTO PLAYがOFFの間は適用しない
    manager.enabled = True
    values, master_values = manager.values_at(1.0)
    assert abs(values[0]["volume"] - 0.50) < 1e-6
    assert abs(values[0]["pan"] - 0.0) < 1e-6
    assert abs(master_values["xfade_position"] - 0.50) < 1e-6

    track = TrackModel(track_id=0, volume=0.20, pan=-1.0, automation=manager.get_track_data(0))
    master_data = manager.get_master_data()
    engine = AudioEngine(num_tracks=1)
    try:
        engine._param_broker.reset_from_tracks([track], 1.0)
        initial = engine._param_broker.snapshot()
        engine.configure_automation([track], master_data, enabled=True, recording=True)
        engine._apply_automation_at(1.0)
        applied = engine._param_broker.take_snapshot(initial.generation)
        assert applied is not None
        assert abs(applied.track_for(0).volume - 0.50) < 1e-6
        assert abs(applied.track_for(0).pan - 0.0) < 1e-6
        assert abs(applied.master.xfade.position - 0.50) < 1e-6
        engine._playing = True  # 録音条件（再生中）をヘッドレスで満たす。
        engine.record_track_automation(0, "volume", 0.65)
        engine.record_master_automation("xfade_position", 0.35)
        assert engine.get_automation_track_data(0)["volume"][0]["time_sec"] == 0.0
        assert abs(engine.get_automation_track_data(0)["volume"][0]["value"] - 0.65) < 1e-6
    finally:
        engine.cleanup()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "automation_project.m4t")
        store = ProjectStore(project_path=path)
        assert store.save([track], master_automation=master_data)
        loaded = store.load()
        assert loaded is not None
        loaded_track = loaded[0][0]
        assert len(loaded_track.automation["volume"]) == 2
        assert abs(loaded_track.automation["volume"][1]["value"] - 0.80) < 1e-6
        loaded_master = store.get_master_automation_state()
        assert len(loaded_master["xfade_position"]) == 2
        assert abs(loaded_master["xfade_position"][1]["value"] - 0.90) < 1e-6
    print("  オートメーション: OK")


# ===========================================================================
# build_windows.bat ASCII チェック
# ===========================================================================
def test_bat_ascii():
    print("=== build_windows.bat ASCII チェック ===")
    bat_path = os.path.join(os.path.dirname(__file__), "build_windows.bat")
    if not os.path.isfile(bat_path):
        print("  build_windows.bat: NOT FOUND (skip)")
        return
    with open(bat_path, "rb") as f:
        data = f.read()
    non_ascii = [hex(b) for b in data if b > 0x7F]
    assert not non_ascii, f"非ASCII文字が含まれています: {non_ascii}"
    print("  build_windows.bat: ASCII only - OK")


# ===========================================================================
# メイン
# ===========================================================================
if __name__ == "__main__":
    try:
        test_track_model()
        test_audio_engine_16tracks()
        test_export_mix()
        test_project_store_16tracks()
        test_eq_curve()
        test_eq_engine()
        test_audio_engine_eq()
        test_project_store_eq()
        test_effect_engine()
        test_project_store_effect()
        test_gain_model()
        test_multi_track_playback()
        test_master_limiter()
        test_loop_playback()
        test_audio_param_broker()
        test_audio_engine_broker_integration()
        test_xfade_project_store()
        test_eq_snap_morph()
        test_automation()
        test_bat_ascii()
        print("\n=== 全テスト合格 ===")
        sys.exit(0)
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] {e}")
        traceback.print_exc()
        sys.exit(2)
