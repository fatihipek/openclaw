# 🔍 Sistem Simülasyonu — Olası Hatalar ve Eksiklikler

## 1. VAD — Konuşma Algılama

### ❌ Sorun: Sürekli dinleme, gürültü tetikleme
- Wake word YOK → sistem her sesi algılar
- Atölyede matkap/öğütücü/klima sesi → VAD tetiklenir
- Deepgram'a boş/gürültülü ses gider → kredi israfı + gereksiz gecikme

**Çözüm:** Enerji eşiği ekle (şu an yok) + minimum ses süresi (200ms guard)

### ❌ Sorun: VAD threshold 0.05 çok düşük
- Rüzgar, tıkırtı gibi küçük sesler speech sanılır
- Echo (AEC tam temizlemezse) speech sanılır

**Çözüm:** VAD_THRESHOLD = 0.1 veya 0.15 yap

### ❌ Sorun: queue timeout 1sn
- Kimse konuşmazsa `q.get(timeout=1)` her saniye dener
- CPU boş yere çalışır

**Çözüm:** timeout 5sn yap veya sleep ekle

---

## 2. FluxSTT — Deepgram Streaming

### ❌ Sorun: `time.sleep(0.5)` sabit gecikme
- Her transkript için 500ms bekler
- Toplam gecikmeye 500ms ekler

**Çözüm:** sleep'i kaldır, UtteranceEnd event'ini bekle

### ❌ Sorun: Flux model Türkçe performansı
- `flux-general-multi` çok dilli, Türkçe için test edilmedi
- `nova-3` daha iyi Türkçe anlar ama batch-only

**Çözüm:** Test et, kötüyse `nova-3`'e dön

### ❌ Sorun: API key geçerliliği
- 200$ kredi var, sürekli kullanımda biter
- Kredi bitince STT çalışmaz

**Çözüm:** Bakiye takibi ekle

### ❌ Sorun: İnternet kesintisi
- Deepgram cloud API → internet yoksa STT çalışmaz
- Jetson'da Tor/P firewall sorunu

**Çözüm:** Hata yönetimi + yeniden dene

---

## 3. GatewayClient — OpenClaw WS

### ❌ Sorun: Event loop deadlock RISK
- `_loop.run_until_complete()` ana thread'de çalışır
- `call_stream()` queue'dan okur, `_run()` async generator'dan queue'ya yazar
- AYNI event loop'ta `run_coroutine_threadsafe` + `run_until_complete` → deadlock POTANSİYELİ

**Çözüm:** İlk testte mutlaka kontrol et. Deadlock olursa ayrı thread'de event loop çalıştır.

### ❌ Sorun: Session "atolye" çakışması
- OpenClaw'da zaten "atolye" session'ı varsa ne olur?
- `sessions.create` var olan session'da hata verir mi? Yoksa açıp aynı mı?

**Çözüm:** Test et, hata alınırsa `sessions.get` veya farklı key dene

### ❌ Sorun: `sessions.send` ack timeout
- `_wait_for(req_id, timeout=10)` — 10sn bekler
- Gateway cevap vermezse 10sn bekleme

**Çözüm:** Timeout'u 5sn yap

### ❌ Sorun: Token streaming bozuk olabilir
- `event: "agent"` → `stream: "assistant"` → `data.delta` formatı değişmiş olabilir
- Veya hiç delta gelmeyebilir (batch response)

**Çözüm:** İlk testte raw WS mesajlarını logla

---

## 4. TTS — Edge-TTS

### ❌ Sorun: Her cümle ayrı ffplay prosesi
- 3 cümlelik cevap = 3 ayrı Edge-TTS API çağrısı
- Her biri ~500ms → toplam ~1.5sn sadece TTS için
- Arada boşluklar/farklı ses tonu

**Çözüm:** TTS paralel çalıştır (cümle 1 oynarken cümle 2 hazırlansın)

### ❌ Sorun: ffplay bulunamayabilir
- `ffplay` ffmpeg'in bir parçası, yoksa TTS çalışmaz

**Kontrol:** `which ffplay`

### ❌ Sorun: TMP dosya birikmesi
- `finally` bloğunda siliniyor ama exception'da kalabilir
- SSD'de birikinti

**Çözüm:** Eski tmp dosyalarını temizle (24 saatten eski)

### ❌ Sorun: "AhmetNeural" sesi değişmiş olabilir
- Microsoft Edge-TTS ses adları değişebilir
- `tr-TR-AhmetNeural` hala geçerli mi?

**Çözüm:** Test et, hata alınırsa güncelle

---

## 5. AEC — Echo Cancellation

### ❌ Sorun: AEC hiç test edilmedi
- PulseAudio modülü yüklendi ama gerçek echo'yu temizlediği kanıtlanmadı
- WebRTC AEC 32kHz'de çalışıyor, voice_chat 16kHz → sample rate dönüşümü

**Çözüm:** TTS çal → mikrofonu kaydet → echo temiz mi kontrol et

### ❌ Sorun: AEC öğrenme süresi
- Adaptif filtrenin ortamı öğrenmesi için 1-2sn gerekir
- İlk TTS anında echo geçebilir

**Çözüm:** Servis başlangıcında kısa bir kalibrasyon sesi çal (opsiyonel)

### ❌ Sorun: Çift AEC
- module-echo-cancel zaten yüklüyse tekrar yüklenemez
- Servis restart'ında hata alınabilir

**Çözüm:** load öncesi kontrol et, varsa yükleme

---

## 6. Ana Döngü — Kritik Eksikler

### ❌ KRİTİK: Log yok
- Sadece print(), journalctl'a gider
- Test sırasında hata ayıklama çok zor

**Çözüm:** Hata ayıklama modu ekle (--debug)

### ❌ KRİTİK: Döngü sonsuz, çıkış yok
- `while True:` — sadece Ctrl+C ile çıkılır
- Hata durumunda sonsuz döngü

**Çözüm:** Hata sayacı ekle, 5 hata üst üste → yeniden başlat

### ❌ Gürültü koruması YOK
- Eski sistemde gürültü döngüsü koruması vardı
- Şimdi yok → aynı ses sürekli Deepgram'a gider, kredi biter

**Çözüm:** Aynı transkript 3+ kere gelince bekleme ekle

### ❌ Barge-in KAPALI
- TTS oynarken muted=True → mikrofon sessiz
- Kullanıcı araya giremez

**Çözüm:** AEC çalışıyorsa muted'i kaldır, VAD barge-in algılasın

---

## 7. Servis — Systemd

### ❌ Restart politikası
- `RestartSec=10` — çökünce 10sn sonra yeniden başlar
- AEC yüklenmemişse tekrar tekrar çöker

**Çözüm:** Hata sayısını sınırla (`StartLimitIntervalSec=60`, `StartLimitBurst=3`)

---

## Özet: Test Öncelik Sırası

| # | Test | Ne Bekliyoruz | Kritik mi? |
|---|------|---------------|------------|
| 1 | Servis başlıyor mu? | `[HAZIR] ✅` | 🔴 |
| 2 | AEC çalışıyor mu? | Echo temiz | 🔴 |
| 3 | VAD ses algılıyor mu? | Konuşunca tetiklenmeli | 🔴 |
| 4 | Flux STT transkript alıyor mu? | Metin dönmeli | 🔴 |
| 5 | OpenClaw WS bağlanıyor mu? | Cevap gelmeli | 🔴 |
| 6 | TTS ses çalıyor mu? | Hoparlörden ses | 🔴 |
| 7 | Agent streaming çalışıyor mu? | Cümle cümle gelmeli | 🟡 |
| 8 | Barge-in (elle test) | Konuşunca kesilmeli | 🟡 |
| 9 | Gecikme ölçümü | < 5sn | 🟢 |
