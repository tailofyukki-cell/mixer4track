"""
EQプリセット切り替え時の音声を生成して、実際の音途切れを確認する。
WAVファイルを生成して波形を確認する。
"""
import wave
import struct
import numpy as np
import sys
sys.path.insert(0, '/home/ubuntu/mixer4track_git')
from eq_engine import EQEngine, EQParams, EQ_PRESETS

sr = 44100
chunk_size = 3528  # 80ms

# 連続した正弦波（440Hz）
total_chunks = 20
total_samples = chunk_size * total_chunks
t_all = np.arange(total_samples, dtype=np.float32) / sr
sig_all = (np.sin(2*np.pi*440*t_all) * 0.5).reshape(-1,1).repeat(2, axis=1)

def get_chunk(n):
    return sig_all[n*chunk_size:(n+1)*chunk_size]

print("=== EQプリセット切り替え音声テスト ===")

# シナリオ: Flat で5チャンク -> Bass Boost に切り替え -> 5チャンク
engine = EQEngine(44100)
output_chunks = []

for i in range(5):
    out = engine.apply_eq(get_chunk(i))
    output_chunks.append(out)
    print(f"チャンク{i:2d} (Flat):      RMS={np.sqrt(np.mean(out[:,0]**2)):.4f}")

# Bass Boost に切り替え
print("\n--- Bass Boost に切り替え ---\n")
engine.set_params(EQ_PRESETS['Bass Boost'])

for i in range(5, 15):
    out = engine.apply_eq(get_chunk(i))
    output_chunks.append(out)
    cf_status = f"(CF残り:{engine._crossfade_pos})" if engine._crossfade_pos > 0 else "(定常)"
    print(f"チャンク{i:2d} (BassBoost): RMS={np.sqrt(np.mean(out[:,0]**2)):.4f} {cf_status}")

# WAVファイルに書き出し
output = np.concatenate(output_chunks, axis=0)
output_i16 = np.clip(output * 32767, -32768, 32767).astype(np.int16)

with wave.open('/tmp/eq_test_output.wav', 'w') as wf:
    wf.setnchannels(2)
    wf.setsampwidth(2)
    wf.setframerate(sr)
    # インターリーブ
    interleaved = output_i16.flatten()
    wf.writeframes(interleaved.tobytes())

print(f"\nWAVファイル生成: /tmp/eq_test_output.wav")

# 切り替え直後の詳細分析
print("\n=== 切り替え直後の詳細分析 ===")
engine2 = EQEngine(44100)
for i in range(5):
    engine2.apply_eq(get_chunk(i))

engine2.set_params(EQ_PRESETS['Bass Boost'])

# クロスフェード中のチャンクを分析
cf_chunk = engine2.apply_eq(get_chunk(5))
print(f"クロスフェードチャンク (882サンプル):")
print(f"  最初のサンプル: {float(cf_chunk[0,0]):.6f}")
print(f"  最後のサンプル（前チャンク）: {float(get_chunk(4)[-1,0]):.6f}")
print(f"  ジャンプ量: {abs(float(get_chunk(4)[-1,0]) - float(cf_chunk[0,0])):.6f}")

# 10ms窓でRMS包絡線を計算
window = 441
print(f"\n  RMS包絡線（10ms窓）:")
for pos in range(0, 882, 88):
    rms = np.sqrt(np.mean(cf_chunk[max(0,pos-window//2):pos+window//2, 0]**2))
    ms = pos / sr * 1000
    print(f"    {ms:.1f}ms: RMS={rms:.4f}")

print("\n  -> RMSが大きく下がっている箇所があれば音途切れの原因")
