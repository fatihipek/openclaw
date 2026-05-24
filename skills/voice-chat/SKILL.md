---
name: voice-chat
description: >
  Atılgan için atölye sesli iletişim sistemi (V5).
  Streaming pipeline: 3-katman VAD → Deepgram Flux STT → OpenClaw WS Agent → Edge-TTS.
  Jetson Orin Nano 8GB'da çalışır, sıfır manuel müdahale. Üretime hazır.
---

# Atılgan Voice-Chat Skill V5 🚀

Atölye için Tesla benzeri gerçek zamanlı sesli iletişim sistemi.
Jetson Orin Nano 8GB'da OpenClaw ile entegre çalışır.

## 🎯 Pipeline

```
DMK003 Mikrofon
    → 3-katman VAD (Energy + Silero GPU + Duration Guard)
    → Deepgram Flux streaming STT (cloud, ~200ms)
    → OpenClaw WS Bridge → DeepSeek LLM (cloud, streaming)
    → Edge-TTS AhmetNeural (cloud, cümle bazlı streaming)
    → USB Hoparlör
```

## 📁 Skill Yapısı

```
voice-chat/
├── SKILL.md                    # Bu dosya (dokümantasyon)
├── scripts/
│   └── voice_chat.py           # Ana uygulama (V5, ~26KB)
├── service/
│   └── atolye-voice.service    # systemd servis dosyası
├── setup.sh                    # Tek tık kurulum
├── DERIN_ANALIZ.md             # Derinlemesine sistem analizi
└── SIMULASYON.md               # Olası hata simülasyon raporu
```

## 💻 Kullanım

### Servis Olarak (Önerilen)
```bash
sudo systemctl start atolye-voice.service
sudo systemctl enable atolye-voice.service
journalctl -u atolye-voice.service -f
```

### Debug Modu
```bash
sudo systemctl stop atolye-voice.service
python3 ~/.openclaw/plugin-skills/voice-chat/scripts/voice_chat.py --debug
```

## 🔧 Gereksinimler

- **Donanım:** Jetson Orin Nano 8GB, DMK003 mikrofon (USB), USB hoparlör
- **OpenClaw:** Çalışır durumda (port 18789, device auth)
- **Python:** 3.10 (Jetson)
- **Kütüphaneler:**
  - `deepgram-sdk>=7.0` — Deepgram Flux STT
  - `websockets` — OpenClaw WS bridge
  - `edge-tts` — Microsoft Edge TTS
  - `sounddevice` — Mikrofon erişimi
  - `numpy` — Ses işleme
  - `torch` — Silero VAD GPU
  - `silero-vad` — Voice Activity Detection
  - `cryptography` — OpenClaw device auth

## 🧩 Mimari

```
┌──────────────────────────────────────────────────────────┐
│                   voice_chat.py (V5)                      │
│                                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │
│  │ VAD      │→ │ Flux     │→ │ OpenClaw │→ │ Edge     │ │
│  │ 3-katman │  │ Deepgram │  │ WS Agent │  │ TTS      │ │
│  │ GPU      │  │Streaming │  │Streaming │  │ Cümle    │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘ │
│       │              │              │            │      │
│  DMK003 Mic     Deepgram API    OpenClaw WS    Hoparlör │
│  AEC temiz      Flux model     DeepSeek LLM    USB      │
└──────────────────────────────────────────────────────────┘
```

## ⚙️ Konfigürasyon

Tüm sabitler `voice_chat.py` başında tanımlıdır:

| Değişken | Varsayılan | Açıklama |
|----------|-----------|----------|
| `VAD_ENERGY_THRESHOLD` | 0.005 | -45dBFS altı sessizlik |
| `VAD_SILERO_THRESHOLD` | 0.15 | Silero VAD eşiği |
| `SPEECH_START_FRAMES` | 3 | 96ms konuşma = başladı |
| `SILENCE_END_FRAMES` | 15 | 500ms sessizlik = bitti |
| `MAX_RECORD_SEC` | 8 | Maksimum kayıt |
| `DEEPGRAM_MODEL` | flux-general-multi | Deepgram STT modeli |
| `TTS_VOICE` | tr-TR-AhmetNeural | Edge-TTS sesi |

## ✅ V5'te Düzeltilen Sorunlar

| # | Sorun | Çözüm |
|---|-------|-------|
| 1 | VAD çok hassas, gürültü tetikliyor | 3 katman: energy gate (-45dBFS) + Silero (0.15) + duration guard (96ms) |
| 2 | `time.sleep(0.5)` gecikme | Kaldırıldı → UtteranceEnd event |
| 3 | Event loop deadlock riski | Ayrı thread → `run_coroutine_threadsafe` |
| 4 | Gürültü döngüsü | Aynı transkript 3+/30sn → atla |
| 5 | AEC yoksa echo döngüsü | `_ensure_aec()` — otomatik yükle |
| 6 | Session "atolye" çakışması | `sessions.get` → var mı kontrol et |
| 7 | Sonsuz hata döngüsü | 5 hata → sistem durur |
| 8 | TMP dosya birikmesi | 24 saat temizlik |

## 🧪 Atölyede Test (İlk Kullanım)

```bash
# 1. DMK003 mikrofonu tak
# 2. Servis durumu kontrol
systemctl status atolye-voice.service

# 3. Canlı log
journalctl -u atolye-voice.service -f

# 4. "Atılgan merhaba" de
#    → VAD algılasın
#    → Flux transkript
#    → Agent cevap
#    → TTS ses çalsın
```

## 🔄 Servis Yönetimi

```bash
sudo systemctl restart atolye-voice.service   # Yeniden başlat
sudo systemctl stop atolye-voice.service      # Durdur
sudo systemctl start atolye-voice.service     # Başlat
journalctl -u atolye-voice.service -f -n 50   # Canlı log
```

## 🚀 Yol Haritası

- [ ] Wake word "Atılgan" (openWakeWord)
- [ ] Barge-in (AEC test edildikten sonra)
- [ ] Paralel TTS (cümle 1 oynarken cümle 2 hazırlansın)
- [ ] Toplam gecikme < 2sn
- [ ] Gürültü bastırma (RNNoise)
