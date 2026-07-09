    def update_effect(self, track_id: int, preset_name: str, enabled: bool):
        """
        指定トラックのエフェクトプリセットを更新し、エフェクト適用済み Sound を再生成する。
        再生中の場合は現在位置から即座に再スタートしてリアルタイム反映する。
        """
        self._effect_presets[track_id] = preset_name
        self._effect_enabled[track_id] = enabled
        with self._lock:
            eq_sound = self._eq_sounds.get(track_id) or self._sounds.get(track_id)
            if eq_sound is None:
                return
            new_sound = self._apply_effect_to_sound(eq_sound, preset_name, enabled)
            self._effect_sounds[track_id] = new_sound

            # 再生中なら現在位置から即座に再スタート
            if self._playing:
                channel = self._channels.get(track_id)
                if channel is not None and channel.get_busy():
                    try:
                        vol_l, vol_r = channel.get_volume()
                    except Exception:
                        vol_l, vol_r = 1.0, 1.0
                    channel.stop()
                    channel.play(new_sound, loops=0)
                    channel.set_volume(vol_l, vol_r)
                    self._play_start_time[track_id] = time.monotonic()

    # ------------------------------------------------------------------
    # 将来拡張用スタブ
    # ------------------------------------------------------------------

    # apply_eq スタブは Phase 5 で update_eq に実装済み
