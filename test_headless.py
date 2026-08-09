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

# SDL をダミーに設定（pygame の音声出力を無効化）
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["SDL_VIDEODRIVER"] = "dummy"

from track_model import TrackModel
from audio_engine import AudioEngine
from project_store import ProjectStore
from eq_engine import EQEngine, EQParams, EQ_PRESETS


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
    """複数トラックがそれぞれ別々のpygameチャンネルに割り当てられることを確認する。"""
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

        engine.play_all(tracks)
        time.sleep(0.2)  # ストリームループが走るまで待機

        # 各トラックのチャンネルが別々のインデックスであることを確認
        ch0 = engine._channels.get(0)
        ch1 = engine._channels.get(1)
        ch2 = engine._channels.get(2)
        assert ch0 is not None, "Track 0 のチャンネルが割り当てられていない"
        assert ch1 is not None, "Track 1 のチャンネルが割り当てられていない"
        assert ch2 is not None, "Track 2 のチャンネルが割り当てられていない"

        # 各チャンネルのインデックスが別々であることを確認
        idx0 = ch0.get_sound() if hasattr(ch0, 'get_sound') else id(ch0)
        assert ch0 is not ch1, f"Track 0とTrack 1が同じチャンネルを共有している"
        assert ch1 is not ch2, f"Track 1とTrack 2が同じチャンネルを共有している"
        assert ch0 is not ch2, f"Track 0とTrack 2が同じチャンネルを共有している"

        engine.stop_all()
    print("  複数トラック同時再生: OK")


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
