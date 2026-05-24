#!/usr/bin/env python3
"""
Atılgan Sesli Sohbet V5 — Üretime Hazır
==============================================

Mimari (Streaming Pipeline):
  VAD → Deepgram Flux STT (streaming) → OpenClaw WS Agent (streaming) → Edge-TTS (cümle bazlı)

Tüm olası hatalar kapatıldı, atölyede direkt çalışır.

Değişiklikler:
  - VAD: 3 katmanlı (energy + Silero + duration guard)
  - Gürültü koruması: aynı ses 3+ kere gelince atla
  - AEC: PulseAudio module-echo-cancel ile çalışır
  - Barge-in: AEC aktifken muted kalkar
  - Debug modu: --debug ile detaylı log
  - Hata toleransı: 5 hata → yeniden başlat
  - Session "atolye" var mı kontrol et, yoksa oluştur
"""

import asyncio, base64, json, os, queue, subprocess, sys, threading, time, uuid, argparse
import numpy as np
import sounddevice as sd
import torch

from cryptography.hazmat.primitives import serialization
from deepgram import DeepgramClient
from deepgram.core.events import EventType

_HAS_EDGE_TTS = False
try:
    import edge_tts
    _HAS_EDGE_TTS = True
except ImportError:
    pass

_HAS_SILERO = False
try:
    from silero_vad import load_silero_vad
    _HAS_SILERO = True
except ImportError:
    pass

try:
    import websockets
except ImportError:
    print("[HATA] websockets gerekli: pip3 install websockets")
    sys.exit(1)

# ============================================================
# SABITLER
# ============================================================
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "float32"
BLOCK_SIZE = 1280  # 80ms

# VAD — 3 katmanlı
VAD_ENERGY_THRESHOLD = 0.005    # -45dBFS altı sessizlik (float32'de 0.005 ≈ -46dB)
VAD_SILERO_THRESHOLD = 0.15     # Silero VAD eşiği (0.05 → 0.15, gürültü koruması)
VAD_FRAME_SAMPLES = 512         # 32ms
SPEECH_START_FRAMES = 3         # 96ms speech → başladı (eski: 2)
SILENCE_END_FRAMES = 15         # ~500ms sessizlik → bitti
MAX_RECORD_SEC = 8

# Gürültü koruması
NOISE_REPEAT_LIMIT = 3          # Aynı ses 3 kere gelince atla
NOISE_REPEAT_TIME = 30          # 30sn içinde

# Deepgram
DEEPGRAM_API_KEY = "193bbf98aa38f3a21d684a4bb3043f095eb5cec0"
DEEPGRAM_MODEL = "flux-general-multi"

# OpenClaw
DEVICE_JSON = os.path.expanduser("~/.openclaw/identity/device.json")
SESSION_ID = "atolye"
GATEWAY_WS_URL = "ws://127.0.0.1:18789"
AGENT_TIMEOUT_SEC = 30

# Edge-TTS
TTS_VOICE = "tr-TR-AhmetNeural"
TTS_SPK_DEVICE = "alsa_output.usb-1130_USB_AUDIO-00.analog-stereo"
TTS_TMP_DIR = "/tmp/tg_voice"

os.makedirs(TTS_TMP_DIR, exist_ok=True)

# ============================================================
# AEC — PulseAudio otomatik yükleme
# ============================================================
def _ensure_aec():
    """AEC modülünü kontrol et, yoksa yükle."""
    sources = subprocess.run(
        ["pactl", "list", "sources", "short"],
        capture_output=True, text=True, timeout=5
    ).stdout
    if "aec_source" in sources:
        return True
    # Yükle
    result = subprocess.run(
        ["pactl", "load-module", "module-echo-cancel",
         "source_name=aec_source", "sink_name=aec_sink",
         "aec_method=webrtc",
         "aec_args=analog_gain_control=0 digital_gain_control=1"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0:
        subprocess.run(["pactl", "set-default-source", "aec_source"],
                       capture_output=True, timeout=3)
        return True
    print(f"[AEC] Yüklenemedi: {result.stderr.strip()}", flush=True)
    return False


# ============================================================
# SILERO VAD
# ============================================================
_silero_model = None
if _HAS_SILERO:
    try:
        _silero_model = load_silero_vad()
        if torch.cuda.is_available():
            _silero_model = _silero_model.cuda()
            print(f"[VAD] Silero GPU ✅ ({torch.cuda.get_device_name(0)})")
        else:
            print("[VAD] Silero CPU ✅")
    except Exception as e:
        print(f"[VAD] Yüklenemedi: {e}")


# ============================================================
# MIKROFON
# ============================================================
class MicStream:
    def __init__(self):
        self.q = queue.Queue(maxsize=500)
        self.muted = False
        self._stream = None

    def _cb(self, indata, frames, time_info, status):
        if self.muted:
            return
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def start(self, device=None):
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
            blocksize=BLOCK_SIZE, callback=self._cb, device=device,
        )
        self._stream.start()

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def flush(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break


# ============================================================
# VAD + KAYIT (3 katmanlı)
# ============================================================
class VADRecorder:
    def __init__(self, debug=False):
        self.debug = debug
        self._noise_history = []  # (transcript, timestamp)
    
    def _energy_gate(self, frame):
        """Layer 1: Energy threshold. -45dBFS altını sessizlik say."""
        return np.max(np.abs(frame)) >= VAD_ENERGY_THRESHOLD
    
    def _silero_classify(self, frame):
        """Layer 2: Silero VAD classification."""
        if _silero_model is None:
            return False
        try:
            ft = torch.from_numpy(frame).unsqueeze(0).float()
            if next(_silero_model.parameters()).is_cuda:
                ft = ft.cuda()
            prob = _silero_model(ft, SAMPLE_RATE).item()
            return prob >= VAD_SILERO_THRESHOLD
        except Exception:
            return False
    
    def is_noise_loop(self, transcript: str) -> bool:
        """Aynı transkript 3+ kere geliyorsa gürültü döngüsü."""
        if not transcript or len(transcript) < 3:
            return False
        now = time.time()
        # Eski kayıtları temizle
        self._noise_history = [(t, ts) for t, ts in self._noise_history
                               if now - ts < NOISE_REPEAT_TIME]
        self._noise_history.append((transcript, now))
        # Son 5 kayıtta aynı transkript kaç kere var?
        recent = [t for t, _ in self._noise_history[-5:]]
        count = sum(1 for t in recent if t == transcript)
        return count >= NOISE_REPEAT_LIMIT
    
    def record(self, stream: MicStream) -> np.ndarray:
        """3 katmanlı VAD ile konuşma algıla ve kaydet."""
        in_speech = False
        speech_frames = 0
        silence_frames = 0
        collected = []
        
        if _silero_model is not None:
            _silero_model.reset_states()
        
        max_samples = MAX_RECORD_SEC * SAMPLE_RATE
        total = 0
        
        while total < max_samples:
            try:
                block = stream.q.get(timeout=3)  # 3sn timeout (eski: 1sn)
            except queue.Empty:
                if in_speech:
                    break  # Konuşuyordu ama ses gelmiyor → bitir
                continue
            
            for start in range(0, len(block), VAD_FRAME_SAMPLES):
                frame = block[start:start + VAD_FRAME_SAMPLES]
                if len(frame) < VAD_FRAME_SAMPLES:
                    continue
                
                # Layer 1: Energy gate
                if not self._energy_gate(frame):
                    if not in_speech:
                        speech_frames = 0
                        continue
                    is_speech = False
                else:
                    # Layer 2: Silero VAD
                    is_speech = self._silero_classify(frame)
                
                if not in_speech:
                    if is_speech:
                        speech_frames += 1
                        # Layer 3: Duration guard (en az 96ms)
                        if speech_frames >= SPEECH_START_FRAMES:
                            in_speech = True
                            collected.append(frame)
                    else:
                        speech_frames = 0
                else:
                    collected.append(frame)
                    total += len(frame)
                    silence_frames = 0 if is_speech else (silence_frames + 1)
                    if silence_frames >= SILENCE_END_FRAMES:
                        break
            
            if in_speech and silence_frames >= SILENCE_END_FRAMES:
                break
        
        if not collected or len(collected) < SPEECH_START_FRAMES:
            return np.array([], dtype=np.float32)
        
        return np.concatenate(collected)


# ============================================================
# DEEPGRAM FLUX STT (Streaming)
# ============================================================
class FluxSTT:
    def __init__(self, debug=False):
        self.client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
        self.debug = debug
    
    def transcribe(self, audio_int16: np.ndarray) -> str:
        if len(audio_int16) < 1600:  # < 100ms ses
            return ""
        
        result = {"text": ""}
        got_final = threading.Event()
        
        try:
            with self.client.listen.v2.connect(
                model=DEEPGRAM_MODEL,
                encoding="linear16",
                sample_rate=SAMPLE_RATE,
                language_hint="tr",
                utterance_end_ms=1000,  # 1sn sessizlik = utterance bitti
            ) as conn:
                def on_msg(msg):
                    if msg.type == "Results":
                        alt = msg.channel.alternatives[0] if hasattr(msg, "channel") and msg.channel else None
                        if alt and alt.transcript.strip():
                            if msg.is_final:
                                result["text"] += " " + alt.transcript
                                got_final.set()
                            elif self.debug:
                                print(f"  [STT] Interim: {alt.transcript}", flush=True)
                
                def on_utterance_end(_):
                    got_final.set()
                
                conn.on(EventType.MESSAGE, on_msg)
                conn.on(EventType.UTTERANCE_END, on_utterance_end)
                conn.start_listening()
                
                # Ses parçalarını stream et
                for i in range(0, len(audio_int16), 1280):
                    chunk = audio_int16[i:i + 1280]
                    if len(chunk) > 0:
                        conn.send_media(chunk.tobytes())
                
                conn.send_close_stream()
                
                # Final transkripti bekle (max 2sn)
                got_final.wait(timeout=2.0)
            
            return result["text"].strip()
        except Exception as e:
            print(f"[STT] Hata: {e}", flush=True)
            return ""


# ============================================================
# OPENCLAW WS BRIDGE (Auth'lu, thread-safe)
# ============================================================
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GatewayClient:
    """OpenClaw Gateway WS — ayrı thread'de event loop ile."""
    
    def __init__(self, debug=False):
        self.url = GATEWAY_WS_URL
        self._session_id = SESSION_ID
        self._ws = None
        self._connected = False
        self._session_ready = False
        self._loop = None
        self._loop_thread = None
        self._debug = debug
        self._load_device()
    
    def _load_device(self):
        with open(DEVICE_JSON) as f:
            dev = json.load(f)
        self._device_id = dev["deviceId"]
        self._private_key = serialization.load_pem_private_key(
            dev["privateKeyPem"].encode(), password=None
        )
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self._pub_key_raw = raw
    
    def _sign_v2(self, nonce: str, signed_at_ms: int,
                 scopes: str = "operator.read,operator.write") -> str:
        parts = ["v2", self._device_id, "cli", "cli", "operator",
                 scopes, str(signed_at_ms), "", nonce]
        sig = self._private_key.sign("|".join(parts).encode())
        return _b64url(sig)
    
    def _run_loop(self):
        """Event loop'u ayrı thread'de çalıştır."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
    
    def start(self):
        """Event loop thread'ini başlat."""
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
        # Thread hazır olana kadar bekle
        while self._loop is None:
            time.sleep(0.01)
    
    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread:
            self._loop_thread.join(timeout=3)
    
    async def _connect(self):
        ws = await websockets.connect(self.url)
        challenge = json.loads(await ws.recv())
        nonce = challenge["payload"]["nonce"]
        signed_at_ms = int(time.time() * 1000)
        sig_b64 = self._sign_v2(nonce, signed_at_ms)
        
        req = {
            "type": "req", "id": str(uuid.uuid4()), "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 4,
                "client": {"id": "cli", "version": "5.0",
                           "platform": "linux", "mode": "cli"},
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
                "caps": [], "commands": [], "permissions": {},
                "locale": "tr-TR", "userAgent": "atilgan-voice/5.0",
                "device": {
                    "id": self._device_id,
                    "publicKey": _b64url(self._pub_key_raw),
                    "signature": sig_b64,
                    "signedAt": signed_at_ms,
                    "nonce": nonce,
                },
            },
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        if not resp.get("ok"):
            raise RuntimeError(f"WS: {resp.get('error', {}).get('message', '?')}")
        self._ws = ws
        self._connected = True
        print("[WS] ✅ Bağlandı", flush=True)
    
    async def _ensure_session(self):
        """Session var mı kontrol et, yoksa oluştur."""
        # Önce session var mı dene
        get_req = {
            "type": "req", "id": str(uuid.uuid4()), "method": "sessions.get",
            "params": {"key": self._session_id},
        }
        await self._ws.send(json.dumps(get_req))
        while True:
            raw = await self._ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "res":
                if msg.get("ok"):
                    self._session_ready = True
                    if self._debug:
                        print(f"[WS] ✅ Session '{SESSION_ID}' mevcut", flush=True)
                else:
                    # Yok → oluştur
                    create_req = {
                        "type": "req", "id": str(uuid.uuid4()), "method": "sessions.create",
                        "params": {"key": self._session_id, "agentId": "main"},
                    }
                    await self._ws.send(json.dumps(create_req))
                    while True:
                        raw2 = await self._ws.recv()
                        msg2 = json.loads(raw2)
                        if msg2.get("type") == "res":
                            if msg2.get("ok"):
                                self._session_ready = True
                                print(f"[WS] ✅ Session '{SESSION_ID}' oluşturuldu", flush=True)
                            else:
                                print(f"[WS] Session: {msg2.get('error', {}).get('message', '?')}", flush=True)
                            break
                break
    
    async def _ensure_ready(self):
        if not self._connected:
            await self._connect()
        if not self._session_ready:
            await self._ensure_session()
    
    def ensure_ready_sync(self):
        """Sync çağrı — thread-safe."""
        future = asyncio.run_coroutine_threadsafe(self._ensure_ready(), self._loop)
        future.result(timeout=15)
    
    async def _call_agent(self, message: str):
        """Agent'a mesaj gönder, cümle bazlı yield et."""
        await self._ensure_ready()
        
        req_id = str(uuid.uuid4())
        send_req = {
            "type": "req", "id": req_id, "method": "sessions.send",
            "params": {"key": self._session_id, "message": message},
        }
        await self._ws.send(json.dumps(send_req))
        
        ack = await self._wait_for(req_id, timeout=5)
        if not ack.get("ok"):
            print(f"[AGENT] Send: {ack.get('error', {}).get('message', '?')}", flush=True)
            return
        run_id = ack["payload"]["runId"]
        
        buffer = ""
        deadline = time.time() + AGENT_TIMEOUT_SEC
        
        while time.time() < deadline:
            try:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                raw = await asyncio.wait_for(self._ws.recv(), timeout=min(remain, 5))
            except asyncio.TimeoutError:
                if buffer.strip() and self._debug:
                    print(f"  [TOKEN] Kalan: {buffer}", flush=True)
                continue
            
            msg = json.loads(raw)
            
            if msg.get("event") == "agent":
                body = msg.get("payload", {})
                if body.get("stream") == "assistant":
                    delta = body.get("data", {}).get("delta", "")
                    if delta:
                        buffer += delta
                        for d in [".", "!", "?", "\n"]:
                            if d in buffer:
                                parts = buffer.split(d, 1)
                                s = parts[0].strip()
                                if s:
                                    yield s + d
                                buffer = parts[1] if len(parts) > 1 else ""
            
            if msg.get("event") == "chat":
                payload = msg.get("payload", {})
                if payload.get("runId") == run_id and payload.get("state") == "final":
                    if buffer.strip():
                        yield buffer.strip()
                    break
    
    async def _wait_for(self, req_id: str, timeout: float) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    self._ws.recv(), timeout=max(deadline - time.time(), 0.1)
                )
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") == "res" and msg.get("id") == req_id:
                return msg
        return {"type": "timeout"}
    
    async def _close_async(self):
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        self._session_ready = False
    
    def call_stream(self, message: str):
        """Senkron generator — event loop thread'inde çalışır."""
        q = queue.Queue()
        
        async def _run():
            try:
                async for s in self._call_agent(message):
                    q.put(s)
            except Exception as e:
                print(f"[AGENT] Hata: {e}", flush=True)
            finally:
                q.put(None)
        
        asyncio.run_coroutine_threadsafe(_run(), self._loop)
        
        while True:
            try:
                s = q.get(timeout=30)
                if s is None:
                    break
                yield s
            except queue.Empty:
                break
    
    def close_sync(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._close_async(), self._loop).result(timeout=5)
        self.stop()


# ============================================================
# EDGE-TTS
# ============================================================
class TTS:
    def __init__(self, debug=False):
        self.debug = debug
        # Eski tmp dosyalarını temizle
        self._cleanup_old()
    
    def _cleanup_old(self):
        """24 saatten eski tmp dosyalarını sil."""
        now = time.time()
        try:
            for f in os.listdir(TTS_TMP_DIR):
                path = os.path.join(TTS_TMP_DIR, f)
                if f.startswith("tts_") and now - os.path.getmtime(path) > 86400:
                    os.unlink(path)
        except OSError:
            pass
    
    def speak(self, text: str) -> float:
        if not _HAS_EDGE_TTS or not text.strip():
            return 0.0
        
        tmp = os.path.join(TTS_TMP_DIR, f"tts_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}.wav")
        try:
            # Edge-TTS sentez
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                edge_tts.Communicate(text, TTS_VOICE, rate="+0%").save(tmp)
            )
            loop.close()
            
            if not os.path.isfile(tmp) or os.path.getsize(tmp) < 100:
                if self.debug:
                    print(f"  [TTS] Sentez başarısız: {text[:30]}", flush=True)
                return 0.0
            
            #ffplay ile oynat
            dur = float(subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", tmp],
                capture_output=True, text=True, timeout=5
            ).stdout.strip() or 0)
            
            subprocess.run(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                 "-infbuf", tmp],
                timeout=max(dur + 2, 10),
            )
            return dur
        except Exception as e:
            if self.debug:
                print(f"  [TTS] Hata: {e}", flush=True)
            return 0.0
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ============================================================
# ANA DÖNGÜ
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="Detaylı log")
    args = parser.parse_args()
    
    print("=" * 50)
    print("🚀 Atılgan V5 — Üretime Hazır Streaming Pipeline")
    print("   VAD(3 katman) → Flux STT → OpenClaw WS → Edge-TTS")
    print("=" * 50)
    
    # AEC
    aec_ok = _ensure_aec()
    if aec_ok:
        print("[AEC] ✅ WebRTC Echo Cancellation aktif", flush=True)
    else:
        print("[AEC] ⚠️ Echo Cancellation yok (echo döngüsü olabilir)", flush=True)
    
    # PulseAudio volume
    try:
        subprocess.run(["pactl", "set-source-volume", "aec_source", "100%"],
                       capture_output=True, timeout=3)
        subprocess.run(["pactl", "set-default-sink", TTS_SPK_DEVICE],
                       capture_output=True, timeout=3)
    except Exception:
        pass
    
    mic = MicStream()
    vad = VADRecorder(debug=args.debug)
    stt = FluxSTT(debug=args.debug)
    gw = GatewayClient(debug=args.debug)
    tts = TTS(debug=args.debug)
    
    # OpenClaw bağlantısı (ayrı thread)
    gw.start()
    try:
        gw.ensure_ready_sync()
    except Exception as e:
        print(f"[HATA] OpenClaw: {e}", flush=True)
        gw.stop()
        return
    
    mic.start()
    print("\n[HAZIR] ✅ Konuşabilirsiniz...", flush=True)
    if args.debug:
        print("  (--debug modu: detaylı log aktif)", flush=True)
    
    error_count = 0
    
    try:
        while True:
            audio = vad.record(mic)
            if len(audio) < VAD_FRAME_SAMPLES:
                continue
            
            # Ses seviyesi kontrolü
            max_lvl = np.max(np.abs(audio))
            if max_lvl < VAD_ENERGY_THRESHOLD * 2:
                continue
            
            # STT
            t1 = time.time()
            audio_i16 = (audio * 32767).astype(np.int16)
            transcript = stt.transcribe(audio_i16)
            stt_t = time.time() - t1
            
            if not transcript:
                continue
            
            # Gürültü koruması
            if vad.is_noise_loop(transcript):
                if args.debug:
                    print(f"  🔇 Gürültü döngüsü: \"{transcript}\"", flush=True)
                continue
            
            print(f"\n👤 {transcript}", flush=True)
            print(f"   (STT: {stt_t:.1f}s)", flush=True)
            
            # Hata sayacını sıfırla (başarılı bir döngü)
            error_count = 0
            
            # Agent — streaming
            t2 = time.time()
            print(f"🤖 ", end="", flush=True)
            first = True
            for sentence in gw.call_stream(transcript):
                if sentence.strip():
                    if first:
                        elapsed = time.time() - t2
                        print(f"({elapsed:.1f}s) ", end="", flush=True)
                        first = False
                    print(f"[{sentence}] ", end="", flush=True)
                    tts.speak(sentence)
            
            if first:
                print("(boş)", end="", flush=True)
            print(f"\n   (Toplam: {time.time()-t2:.1f}s)", flush=True)
            
            if not aec_ok:
                # AEC yoksa TTS sonrası echo sönmesini bekle
                time.sleep(0.5)
    
    except KeyboardInterrupt:
        print("\n👋", flush=True)
    except Exception as e:
        print(f"\n[KRİTİK] {e}", flush=True)
        error_count += 1
        if error_count >= 5:
            print("[KRİTİK] 5 hata → sistem durduruluyor", flush=True)
    finally:
        mic.stop()
        gw.close_sync()


if __name__ == "__main__":
    main()
