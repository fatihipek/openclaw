---
name: voice-telegram
description: >
  Atılgan için Telegram sesli iletişim sistemi.
  Voice mesajları: STT (Deepgram Nova-3 cloud API, birincil) + Whisper GPU (yedek).
  TTS: Edge-TTS AhmetNeural (cloud, doğal ses). Tüm pipeline Jetson Orin Nano
  üzerinde OpenClaw session içinde çalışır. Sıfır manuel müdahale.
---

# Voice-Telegram Skill v2 🚀

Atılgan'ın Telegram sesli iletişim sistemi. Jetson Orin Nano 8GB'da
çalışır. STT'de Deepgram Nova-3 (cloud, ~2sn) birincil, Whisper GPU (yedek).
TTS'de Edge-TTS AhmetNeural (Microsoft Cloud) birincil, Piper Fahrettin (yedek).

## 🎯 Pipeline

```
Telegram Voice
    → OpenClaw inbound'a kaydeder (otomatik)
    → Deepgram Nova-3 STT (cloud, ~2sn)
      ↳ Yedek: Whisper GPU (base, ~3sn)
    → AI cevap üretimi (DeepSeek)
    → Edge-TTS AhmetNeural (cloud, ~1sn)
      ↳ Yedek: Piper Fahrettin (GPU)
    → Telegram sendVoice (sesli) + Yazılı cevap
    → Inbound media temizliği (otomatik)
```

## 📁 Skill Yapısı

```
voice-telegram/
├── SKILL.md                   # Bu dosya (dokümantasyon)
├── scripts/
│   ├── tg_voice_handler.py    # Ana CLI: STT, TTS, auto-process, temizlik
│   ├── tg_voice.py            # Altyapı: Telegram download, TTS motoru
│   ├── tg_voice_service.py    # (devre dışı) Legacy polling servisi
│   └── piper_tr_fix.py        # Piper fonem düzeltme (yedek TTS için)
├── piper-models/
│   └── ...                    # Piper TTS modelleri (yedek)
└── setup.sh                   # Tek tık kurulum
```

## 💻 Kullanım

### OpenClaw Session İçinde (Otomatik Pipeline)

Voice mesajı geldiğinde Atılgan şu adımları otomatik çalıştırır:

1. Inbound'daki en son ses dosyasını al
2. Deepgram STT ile transkript et:
   ```bash
   python3 scripts/tg_voice_handler.py auto-process
   ```
3. Transkripti oku, AI cevabını üret
4. Sesli cevap gönder:
   ```bash
   python3 scripts/tg_voice_handler.py tts-send <chat_id> "<cevap>"
   ```
5. Yazılı cevabı da aynı anda yaz

### Manuel Komutlar

```bash
# En son inbound sesi Deepgram ile transkript et
python3 scripts/tg_voice_handler.py auto-process

# Belirli bir ses dosyasını Deepgram ile transkript et
python3 scripts/tg_voice_handler.py deepgram-stt /path/to/ses.ogg

# Metni seslendir + Telegram'a gönder
python3 scripts/tg_voice_handler.py tts-send 1833986772 "Merhaba Fatih"

# Inbound media temizliği
python3 scripts/tg_voice_handler.py clean-all 0
```

## ⚙️ Konfigürasyon

### Deepgram API Key
`~/.openclaw/.env` dosyasına yazılır:
```
DEEPGRAM_API_KEY=xxx...
```
Handler bu dosyayı otomatik okur. Yoksa whisper yedeğe geçer.

### OpenClaw AGENTS.md
Voice mesajı geldiğinde pipeline'ı çalıştırmak için AGENTS.md'de
talimatlar mevcuttur. Session otomatik olarak:
1. Inbound'dan son ses dosyasını alır
2. Deepgram STT ile transkript eder
3. Sesli + yazılı cevap verir

## 🔄 Kalıcılık

- **OpenClaw Gateway**: systemd boot'ta otomatik başlar
- **Skill dosyaları**: Disk'te kalıcı, `~/.openclaw/plugin-skills/voice-telegram/`
- **Deepgram API key**: `.env` dosyasında
- **Hiçbir background servis gerekmez** — her şey OpenClaw session'ı içinde çalışır

## 📝 Notlar

- Deepgram Nova-3 Türkçe'de Whisper base'den belirgin şekilde daha iyidir
- Deepgram API'nin $200 ücretsiz kredisi vardır (atölye sistemi ile ortak)
- İnternet gidince otomatik Whisper GPU yedeğine geçer
- Tüm temp dosyalar her işlem sonrası temizlenir (Talimat #8)
- dmPolicy=allowlist ile sadece yetkili kullanıcı (Fatih, 1833986772) erişebilir
