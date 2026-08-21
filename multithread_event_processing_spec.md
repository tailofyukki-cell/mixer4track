# Mixer4Track 並行操作基盤 — マルチスレッド・イベント処理 詳細設計

**対象フェーズ**: Phase 24（AudioParamBroker）以降  
**更新日**: 2026-08-19  
**状態**: Phase 26 実装済み（Fader / PAN / MUTE / SOLO / MASTER Volume / GAIN / Track EQ / Track FX / AUX / MASTER GEQ / X-FADER / EQ Snapshot A/B / EQ Morph）
**前提**: 現在の `AudioEngine._master_mix_loop()`、44.1kHz、80msチャンク、`QUEUE_AHEAD=2` を維持する。

---

## 1. 目的と結論

この設計の目的は、再生中にフェーダー、PAN、MUTE/SOLO、AUX、FX、マスターGEQ、クロスフェーダー、EQ Morphを**同一の音声チャンク境界で矛盾なく反映**することである。UIイベントをそのままDSPオブジェクトへ渡すのではなく、`AudioParamBroker` を唯一の受け渡し点とする。

> UIスレッドは「意図」をBrokerへ登録するだけとし、音声スレッドだけがDSP状態を変更する。DSP計算中はBrokerのロックを保持しない。

この構成により、複数パラメータを一つの操作として原子的に適用できる。マウスで同時に二つのスライダーを掴めないというUI上の制約は残るが、X-FADER、EQ Morph、オートメーション、将来のMIDI入力が同時に異なるパラメータを更新しても、安全に合成できる。

| 設計判断 | 採用内容 | 理由 |
|---|---|---|
| 音声処理の所有者 | `MasterMixStreamLoop` のみ | DSP状態の更新元を一つに固定し、競合を除去する。 |
| UI→音声の受け渡し | Latest-value mailbox + 世代番号 | 高頻度スライダー操作でイベントが蓄積しない。 |
| 適用時点 | 次のチャンク境界 | チャンク内の状態を不変にし、処理順を決定的にする。 |
| 連続値の変化 | レンダリング状態内のランプ | フェーダー・PAN・X-FADERの急変によるクリックを防ぐ。 |
| 同一値への連続操作 | Last Writer Wins | 最終位置へ迅速に追従し、古い中間値へ戻らない。 |
| 重大操作 | Transport Epochで全保留操作を無効化 | STOP、LOAD、BANK切替、プロジェクト読込後の古いイベント適用を防ぐ。 |

---

## 2. 現行構成の監査結果

現在はPyQt UIスレッドから `AudioEngine.update_gain()`、`update_eq()`、`update_effect()`、`update_track()` 等を直接呼び出し、個々の`_TrackStreamer`のロックで更新している。マスター合成は`MasterMixStreamLoop`が行い、ミックス済みPCMはMASTER GEQ、マスター・リミッター、REC、pygame出力へ進む。

| 現行要素 | 現行の動作 | 並行操作時の課題 | Broker導入後の扱い |
|---|---|---|---|
| `TrackWidget`のスライダー | UIイベントごとにBroker更新 | 高頻度イベントでも中間値を蓄積しない | 操作中は最新値だけをBrokerへ上書きする。 |
| `_TrackStreamer` | 音声スレッドがSnapshotからEQ/FX状態を変更 | UIスレッドがDSPオブジェクトへ直接触れない | 音声スレッドだけがDSPへパラメータを投入する。 |
| `_master_mix_loop()` | `QUEUE_AHEAD=2`で先読み | 既にキュー済みの最大160msは旧パラメータの音が残る | 仕様として最大反映遅延を定義し、Transport時のみキューを破棄する。 |
| `_param_changed_event` | `Event.set()`/`clear()`で即時起床を促す | clear直前の更新を取りこぼす可能性がある | 世代番号付き`Condition`へ置換する。 |
| `TrackModel`一覧 | UIスレッドと音声スレッドが同じ可変オブジェクトを参照 | 同一チャンクでMUTEとPANが異なる状態になる余地がある | 不変`AudioParamSnapshot`へ変換してから音声スレッドに渡す。 |
| MICループ | ファイルミックスと独立して最終化処理を呼ぶ | MICとファイル再生を一つのマスター合成として扱えない | Phase 24ではBrokerを共有し、完全なMIC統合バスは後続の明示的スコープとする。 |

現行の80msチャンクと2チャンク先読みでは、通常の連続操作の可聴反映は概ね80〜160ms後になる。これは小規模ミキサーの初期実装として許容するが、X-FADERやEQ MorphをDJ的に扱う場合は、Broker導入後に`CHUNK_SEC=0.04`、`QUEUE_AHEAD=2`へ段階的に短縮できるよう設計する。

---

## 3. スレッドと責務

```text
┌───────────────────────────────────────────────────────────┐
│ PyQt UI thread                                             │
│ TrackWidget / MasterWidget / X-FADER / EQ Morph / Undo     │
│   └─ broker.submit_*()                                     │
└───────────────────────┬───────────────────────────────────┘
                        │ short lock / copy only
                        ▼
┌───────────────────────────────────────────────────────────┐
│ AudioParamBroker                                           │
│ pending patch map + generation + transport epoch + signal  │
└───────────────────────┬───────────────────────────────────┘
                        │ immutable snapshot at chunk boundary
                        ▼
┌───────────────────────────────────────────────────────────┐
│ MasterMixStreamLoop (single audio-DSP writer)              │
│ render state → track DSP → xfade → master mix → limiter    │
│ → pygame master channel / REC buffer / VU                  │
└───────────────────────────────────────────────────────────┘

MIC input thread ── raw PCM only ──► future unified input bus
Offline export worker ── frozen snapshot only ──► offline render
```

| 実行主体 | 禁止事項 | 許可される処理 |
|---|---|---|
| PyQt UIスレッド | `EQEngine`、`EffectEngine`、`MasterLimiter`を直接変更しない。音声ロックを待たない。 | 入力値の正規化、BrokerへのPatch送信、UI表示、UNDO用の操作確定。 |
| AudioParamBroker | DSP計算、Qt Widget更新をしない。 | Patch併合、世代更新、スナップショット生成、起床通知。 |
| MasterMixStreamLoop | QWidgetへ直接アクセスしない。 | スナップショット取得、DSP状態更新、ランプ、合成、VU/REC更新。 |
| MICスレッド | Broker/DSP状態を直接変更しない。 | 生PCMを入力バスへ安全に渡す。 |
| ExportWorker | ライブBrokerを参照しない。 | 開始時に取得したFrozen Snapshotで決定的に書き出す。 |

---

## 4. データモデル

### 4.1 不変スナップショット

`dataclass(frozen=True)`で定義し、音声スレッドが1チャンクを処理する間は一切変更しない。

```python
@dataclass(frozen=True)
class CrossfaderState:
    position: float                 # 0.0=A / 0.5=center / 1.0=B
    curve: Literal["equal_power", "linear"]
    cut_a: bool = False
    cut_b: bool = False

@dataclass(frozen=True)
class TrackRenderParams:
    track_id: int
    volume: float                   # 0.0..1.5
    pan: float                      # -1.0..+1.0
    muted: bool
    solo: bool
    gain_db: float
    aux_enabled: bool
    effect_enabled: bool
    effect_preset: str
    eq: EQParams
    xfade_assign: Literal["A", "B", "THRU"]
    eq_morph_mode: Literal["direct", "morph"]
    eq_morph_position: float        # 0.0..1.0
    eq_snap_a: Optional[EQParams]
    eq_snap_b: Optional[EQParams]

@dataclass(frozen=True)
class MasterRenderParams:
    volume: float
    geq: GEQParams
    limiter_enabled: bool
    limiter_ceiling_db: float
    limiter_release_ms: float
    crossfader: CrossfaderState

@dataclass(frozen=True)
class AudioParamSnapshot:
    generation: int
    transport_epoch: int
    tracks: tuple[TrackRenderParams, ...]
    master: MasterRenderParams
```

`TrackModel`はプロジェクト保存・UI表示用の可変モデルとして残す。音声スレッドは`TrackModel`を読むのではなく、Brokerが作った`TrackRenderParams`だけを読む。

### 4.2 Patchと操作グループ

```python
@dataclass(frozen=True)
class ParamPatch:
    key: tuple[str, int | None, str]  # 例: ("track", 3, "pan")
    value: object
    source: Literal["manual", "automation", "system", "transport"]
    priority: int
    gesture_id: str | None

@dataclass(frozen=True)
class ParamBatch:
    patches: tuple[ParamPatch, ...]
    atomic: bool = True
```

クロスフェードの位置変更は1個のPatchでよい。EQ Morphは`morph_position`だけを更新する。プリセット読込、UNDO/REDO、複数トラックのA/B割当変更は`ParamBatch`として送信し、必ず同じ世代のスナップショットへまとめて反映する。

---

## 5. AudioParamBroker APIと同期規則

### 5.1 API

```python
class AudioParamBroker:
    def submit(self, patch: ParamPatch) -> int: ...
    def submit_batch(self, batch: ParamBatch) -> int: ...
    def begin_gesture(self, gesture_id: str, keys: tuple[ParamKey, ...]) -> None: ...
    def end_gesture(self, gesture_id: str) -> GestureResult: ...
    def take_snapshot(self, last_generation: int) -> AudioParamSnapshot | None: ...
    def begin_transport_epoch(self, command: str) -> int: ...
    def wait_for_generation(self, last_generation: int, timeout_sec: float) -> bool: ...
```

### 5.2 内部状態

```text
lock: threading.RLock
condition: threading.Condition(lock)
generation: int
transport_epoch: int
latest_by_key: dict[ParamKey, ParamPatch]
base_state: mutable canonical parameter state
last_snapshot: AudioParamSnapshot
active_gestures: dict[str, GestureState]
```

`submit()`はロックを取得して、同一`key`のPatchを置換する。PatchをFIFOで無制限に積まない。変更後に`generation += 1`し、`condition.notify_all()`を呼ぶ。ロック内で行う作業は辞書の更新とスナップショット用データのコピーだけであり、DSP・numpy配列処理・Qt呼び出しを含めない。

### 5.3 世代番号でEvent取りこぼしを防ぐ

現行の`Event.set()`/`clear()`は、音声スレッドがclearする瞬間に新規更新が来ると通知を失う可能性がある。Brokerでは音声スレッドが`last_generation`を持ち、次の条件で待機する。

```python
with broker.condition:
    while broker.generation == last_generation and not stop_event.is_set():
        broker.condition.wait(timeout=0.004)
```

通知が失われても世代番号が変わっていれば次回のループで検出できる。音声スレッドは各チャンク開始時に`take_snapshot()`を1回だけ呼び、世代が変わっていない場合は直前スナップショットを再利用する。

### 5.4 優先順位と競合

| 優先順位 | Source | 代表操作 | 規則 |
|---:|---|---|---|
| 400 | transport | STOP、LOAD、プロジェクト読込、BANK切替 | Transport Epochを増やし、古いPatchを無効化する。 |
| 300 | system | 安全ミュート、デバイス喪失、エラー停止 | ユーザー操作より優先してフェードアウトする。 |
| 200 | manual | UI、MIDI、OSCの直接操作 | 同一キーでは最終操作優先。 |
| 100 | automation | 再生中オートメーション | 手動操作があるキーは手動が優先する。 |
| 0 | default | 再生開始時の保存値 | 他の値がなければ使用する。 |

同じ`key`に同一世代で競合する場合は`priority`、次に単調増加`sequence`で決定する。異なる`key`は同一Snapshotへ共存する。たとえばX-FADER位置、Track 2のPAN、Track 5のEQ Morphは同じチャンクに同時適用される。

---

## 6. 音声スレッドのチャンク境界処理

`_master_mix_loop()`は、次の順に処理する。DSP状態を更新するのはStep 2のみである。

```text
1. Brokerから最新のAudioParamSnapshotを取得する。
2. generationが変わった場合、AudioRenderStateへtarget値を設定する。
3. 各連続値について、直前の実効値からtarget値までのランプを生成する。
4. 各TrackStreamerは音声スレッドだけがEQ/FXのtargetをDSPへ投入する。
5. Track DSP出力へFader/PAN/MUTE/SOLO/X-FADERのサンプルゲインを適用する。
6. 全トラックをMaster Mix Busへ加算する。
7. MASTER GEQ、Limiter、VU、REC、pygame queueを処理する。
```

`AudioRenderState`は音声スレッドのローカル状態であり、ロック不要である。

```python
class AudioRenderState:
    applied_generation: int
    track_current: dict[int, RuntimeTrackState]
    master_current: RuntimeMasterState
    eq_engines: dict[int, EQEngine]
    effect_engines: dict[int, EffectEngine]
```

`RuntimeTrackState`は`current`と`target`を分ける。新しいSnapshotが来たときは、前回ランプ途中の`current`から新targetへ再ランプする。前回targetからやり直さないため、操作を高速に往復しても音量が跳ねない。

### 6.1 ランプ基準

| パラメータ | ランプ | 時間 | 備考 |
|---|---|---:|---|
| Fader / MUTE / SOLO / X-FADER CUT | linear gain | 10ms | 0へ落とす操作も必ずランプする。 |
| PAN | equal-power L/R gain | 10ms | PAN値そのものではなくL/Rゲインを補間する。 |
| X-FADER position | curve後のA/B gain | 10ms | A/Bの相対音圧が連続する。 |
| Master volume | linear gain | 15ms | MASTER操作時の段差を防ぐ。 |
| EQ Gain / Freq / Q | EQEngineの二重処理クロスフェード | 20〜40ms | `set_params()`は音声スレッドだけが呼ぶ。 |
| FX ON/OFF | dry/wet crossfade | 20ms | 直ちにバッファを破棄しない。 |
| Limiter Ceiling | dB→linearのランプ | 10ms | リミッター状態は継続する。 |

ランプの長さは、`min(指定サンプル数, CHUNK_SAMPLES)`とする。80msチャンクを維持する間も、ランプそのものはチャンク内で10〜40msに完了する。次のSnapshotは前回の実効値を始点にする。

---

## 7. クロスフェーダー詳細仕様

### 7.1 保存状態

各`TrackModel`へ`xfade_assign: str = "THRU"`を追加する。プロジェクト保存対象である。マスター状態として`xfade_position: float = 0.5`、`xfade_curve: str = "equal_power"`、`xfade_cut_a/b: bool = False`を保存する。

### 7.2 ゲイン計算

X-FADER位置を`x`（0.0〜1.0）とする。

```text
Equal Power:
  A = cos(x × π / 2)
  B = sin(x × π / 2)

Linear:
  A = 1 - x
  B = x

Track final gain = TrackFader × PanGain × MuteSoloGain × XFadeAssignGain
```

`THRU`は常に1.0、`CUT A`または`CUT B`が有効なら当該側は10msで0.0へランプする。X-FADER割当変更も10msランプを通すため、再生中にA/B/THRUを変更してもクリックを出さない。

### 7.3 UIイベント

| UIイベント | Brokerへ送るPatch | 備考 |
|---|---|---|
| X-FADERドラッグ | `("master", None, "xfade_position")` | ドラッグ中は最新値のみ送信する。 |
| ASSIGN A/B/THRU | `("track", track_id, "xfade_assign")` | ボタン操作で即時ターゲット更新。 |
| Curve切替 | `("master", None, "xfade_curve")` | 現在のA/B実効ゲインから20msで新カーブへ移行。 |
| CUT A/B | `("master", None, "xfade_cut_a/b")` | 0/1の切替も10msフェード。 |
| SWAP | 全Trackの割当を`ParamBatch`として送る | 1世代で原子的に反映。 |

---

## 8. EQ Morph詳細仕様

### 8.1 EQスナップショット

各トラックに以下を追加する。

```python
eq_snap_a: EQParams | None
eq_snap_b: EQParams | None
eq_morph_position: float = 0.0
eq_control_mode: Literal["direct", "morph"] = "direct"
```

スナップショットの保存・読込は`.m4t`に含める。Morph中の現在EQは保存値ではなく、A/Bとpositionから毎回決定する派生状態である。

### 8.2 補間

| EQ項目 | 補間法 | 理由 |
|---|---|---|
| Low / Mid / High gain | dB値の線形補間 | 表示と音量変化の関係が分かりやすい。 |
| Mid Frequency | log周波数補間 | 250Hz→4kHzのような広い範囲でも自然に移動する。 |
| Mid Q | log補間 | 帯域幅の変化を滑らかにする。 |

Morph Patchは`eq_morph_position`のみを更新する。音声スレッドは最新positionから派生EQParamsを作り、`EQEngine.set_params()`を最大1回／チャンク呼ぶ。`EQEngine`の内部クロスフェードは音声スレッドのみが所有する。

### 8.3 通常EQとの競合

| 最後の操作 | 結果 |
|---|---|
| Morphノブ | `eq_control_mode="morph"`。A/Bとpositionを採用。 |
| 個別EQノブ | 現在のMorph派生値を直接EQ値として確定し、`eq_control_mode="direct"`へ戻る。 |
| EQプリセット | directモードへ移行し、全EQ値を単一`ParamBatch`で更新する。 |
| UNDO / REDO | 保存された論理状態（directまたはmorph）をBatchで復元する。 |

---

## 9. 操作履歴・オートメーション・UIイベント圧縮

UNDO/REDOは、現在のようにスライダー値のたびに履歴を積まない。`sliderPressed`で`begin_gesture()`、`sliderReleased`で`end_gesture()`を呼び、一連のドラッグを1コマンドへ圧縮する。音声用Brokerへの最新値送信と、UNDO履歴の確定は別責務である。

| 入力種別 | Broker送信 | UNDO履歴 |
|---|---|---|
| スライダードラッグ | `valueChanged`ごとに最新値を上書き | release時に1件だけ確定 |
| ボタン | クリック時に1 Patch | クリックごとに1件 |
| SWAP / プリセット | 1 Batch | 1 Batchで1件 |
| オートメーション | フレーム単位の内部Patch | 通常はUNDO対象外、録音停止時にまとめて扱う |

UIはBrokerの状態をポーリングしない。表示済みのローカル値を更新し、音声スレッドからはVU、GR、再生位置などの観測値だけをスレッドセーフなメーター用スナップショット経由で読む。

### 9.1 Phase 27 オートメーション実装仕様

`automation_engine.py`の`AutomationManager`が、トラックごとの`volume`・`pan`レーンとMASTERの`xfade_position`レーンを保持する。各レーンは`{"time_sec": float, "value": float}`の昇順ポイント列であり、25ms以内の連続ポイントは最新値へ統合する。ポイント間は線形補間し、最初のポイント以前および最終ポイント以後は端点値を維持する。

| 操作 | 条件 | 実装上の扱い |
|---|---|---|
| AUTO REC | ONかつ再生中 | UI操作のフェーダー、PAN、X-FADERを共通タイムライン時刻で記録する。停止中の操作は記録しない。 |
| AUTO PLAY | ON | 音声スレッドが各チャンク生成直前にレーンを評価し、`source="automation"`としてBrokerへ送る。 |
| AUTO CLR | 確認後 | 全トラック・MASTERのレーンを消去し、AUTO REC/PLAYをOFFへ戻す。 |
| SAVE / OPEN | 常時 | トラックの`automation`と`master_automation`をschema 10.0へ保存する。OPEN後のAUTO PLAYは安全のためOFF。 |

`AutomationManager`はRLockで保護する。UIスレッドは記録・保存用データ取得だけを行い、音声スレッドは補間評価だけを行う。手動Patchの優先度は200、automationは100であるため、手動操作が同じキーを更新した時点では手動値が優先される。

---

## 10. Transport Epochと例外時の安全動作

STOP、LOAD、BANK切替、プロジェクトOPEN、再生開始／終了は連続パラメータ更新とは別の`TransportCommand`である。BrokerはTransport処理の開始時に`transport_epoch += 1`を行い、旧EpochのPatchを破棄する。

| 事象 | Broker | 音声スレッド | UI |
|---|---|---|---|
| STOP | 保留Patchをフラッシュ、Epoch更新 | 10msフェードアウト後にキュー停止 | 再生ボタン状態を停止へ戻す。 |
| LOAD | Epoch更新、対象TrackのDSP状態を無効化 | 該当トラックの次チャンクから新PCMへ切替。再生中読込は明示的に「停止後に反映」とする。 | 読込完了通知はUIスレッドへ戻す。 |
| LOOP範囲変更 | 連続パラメータは維持 | 旧キューを停止し、範囲先頭のSnapshotから再開 | LOOP表示だけを更新。 |
| 例外 | `system`優先のSafe Mute Patch | マスターを10msで0へ、エラー状態を記録 | QMessageBoxはUIスレッドだけで表示。 |
| 負荷超過 | 新規DSP更新を間引かず、古いPatchだけを圧縮 | Queue aheadを維持できない場合は安全ミュートしログ | 「Audio underrun」状態を表示。 |

---

## 11. 実装単位と変更箇所

| 実装単位 | 主要ファイル | 内容 |
|---|---|---|
| P24-1 Broker | `audio_param_broker.py`（新規） | Patch併合、世代管理、Snapshot、Condition。DSP依存なし。 |
| P24-2 Model | `track_model.py` / `project_store.py` | X-FADER割当、EQ A/B、Morph、保存schema更新。 |
| P24-3 Engine接続 | `audio_engine.py` | UI直更新APIをBroker送信APIへ置換。音声スレッドでSnapshotを適用。 |
| P24-4 ランプ | `audio_ramp.py`（新規） | Linear、Equal-Power、dB→gain、途中再ターゲット。 |
| P25 X-FADER UI | `mixer_ui.py` | X-FADER、A/B/THRU、Curve、CUT、SWAP。 |
| P26 EQ Morph UI | `mixer_ui.py` | Snap A/B、Morph、参照カーブ、状態表示。 |
| P27 Automation | `automation_engine.py` / `audio_engine.py` / `mixer_ui.py` | Read/Writeイベント列、25ms統合、線形補間、Broker送信、AUTO操作UI、保存・復元。 |

`audio_param_broker.py`と`audio_ramp.py`はPyQt・pygameに依存させない。これによりヘッドレスユニットテストで決定性と競合を検証できる。

---

## 12. テスト設計と受入基準

### 12.1 Broker単体テスト

| ID | 入力 | 期待結果 |
|---|---|---|
| BKR-01 | 同一フェーダーへ1,000回連続Patch | メモリ使用量がPatch回数に比例せず、最終値のみがSnapshotへ入る。 |
| BKR-02 | PAN、EQ、X-FADERを別スレッドから同時送信 | 同一generationのSnapshotに全キーが矛盾なく含まれる。 |
| BKR-03 | `wait_for_generation()`待機中にPatch | 世代番号で必ず起床または次回ループで検出し、更新を失わない。 |
| BKR-04 | STOP後に旧Epoch Patch | 旧PatchがSnapshotへ入らない。 |
| BKR-05 | ManualとAutomationが同じキーを更新 | Manual（優先度200）がAutomation（100）より優先される。 |
| BKR-06 | AUTO PLAYで1秒時点の補間値を評価 | track volume/PANとMASTER X-FADERが期待する線形補間値のSnapshotになる。 |

### 12.2 DSP・統合テスト

| ID | 操作 | 合格基準 |
|---|---|---|
| DSP-01 | 8トラックでX-FADER A→B→Aを10回 | 出力PCMにNaN/Infがない。無音チャンクが意図せず挿入されない。 |
| DSP-02 | X-FADER、PAN、Faderを1秒間に100更新 | 最終Snapshot値と最終チャンクのゲインが一致する。 |
| DSP-03 | EQ Morph 0→1→0を10回 | ストリームが停止せず、EQ更新が音声スレッドのみで実行される。 |
| DSP-04 | 操作中にREC→EXPORT | 録音WAVにX-FADERとMorphの変化が反映される。 |
| DSP-05 | 操作中にLOOP/STOP/PLAY | 旧Epochの値が再生開始後に復活しない。 |
| DSP-06 | 8トラック＋EQ＋FX＋Limiter | 目標Windows環境でQueue underrunなし。処理時間のp95はチャンク長の70%未満を目標とする。 |
| DSP-07 | AUTO REC中にFader/PAN/X-FADERを操作 | 再生中の操作だけがポイント化され、保存後の読み込みで時刻・値が一致する。 |

### 12.3 人による実機評価

受入テストでは、ヘッドフォンでX-FADER中央通過時の定位・音圧、EQ Morph中のクリック、フェーダー操作とEQ操作の組合せ、短いLOOP境界、PAUSE/RESUME、REC WAVの聞き比べに加え、AUTO RECで記録したフェーダー・PAN・X-FADERがAUTO PLAYで意図したタイミングに再現され、クリックや意図しない無音がないことを確認する。自動テストは状態整合性を保証するが、クリック感・音楽的な音圧変化・操作感は実機評価を必須とする。

---

## 13. 実装順序と完了条件

| フェーズ | 実装内容 | 完了条件 |
|---|---|---|
| Phase 24A | Broker、Snapshot、generation、単体テスト | **実装済み**。UIがBrokerへPatchを送り、音声スレッドが不変Snapshotを読んでFader/PAN/MASTERを適用する。 |
| Phase 24B | Fader/PAN/MUTE/SOLO/MASTERのランプ移行 | 既存機能の回帰なし、ドラッグは1件のUNDOに圧縮される。 |
| Phase 25 | X-FADER | A/B/THRU、Curve、CUT、SWAP、保存・REC/WAV反映が完了する。 |
| Phase 26 | EQ Morph | A/B保存、Morph、個別EQ競合規則、EQカーブ表示が完了する。 |
| Phase 27 | オートメーション | **実装済み**。Volume/PAN/X-FADERのWrite/Read、25ms統合、線形補間、Broker適用、保存・復元、ヘッドレス・GUIスモークテストを完了。 |

**Phase 24の着手判定**は、Brokerを先にヘッドレステストで完成させ、既存の`AudioEngine` APIを一度に置き換えず、`update_gain()`と`set_master_volume()`から段階的に接続することとする。音声エンジンの大規模な一括変更は行わない。

---

*作成: Manus AI*  
*本書は既存の`audio_engine.py`と`mixer_ui.py`の監査結果を踏まえた実装詳細設計である。*
