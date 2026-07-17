"""mic_engine の基本動作テスト"""
import time
import mic_engine

devs = mic_engine.get_input_devices()
print("デバイス一覧:", devs)

ok = mic_engine.assign_mic(0, 0, "test_mic")
print("assign_mic(0):", ok)

time.sleep(0.3)

stream = mic_engine.get_mic_stream(0)
if stream:
    chunk = stream.read_chunk(1024)
    print("read_chunk shape:", chunk.shape, "dtype:", chunk.dtype)
    print("has_mic(0):", mic_engine.has_mic(0))
else:
    print("ERROR: stream is None")

mic_engine.release_mic(0)
print("release_mic OK")
print("has_mic(0) after release:", mic_engine.has_mic(0))
print("=== mic_engine テスト完了 ===")
