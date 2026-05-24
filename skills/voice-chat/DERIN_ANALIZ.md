# 🚗 Derinlemesine Sesli İletişim Analizi — Mayıs 2026

## Giriş: Neden "Basit STT→LLM→TTS" Yetmez?

Basit anlatımda sistem VAD → STT → LLM → TTS olarak çalışır.
**Gerçek prodüksiyon sistemleri** bunun çok ötesinde:

1. **Full-duplex** (çift yönlü, aynı anda konuşma/dinleme)
2. **Barge-in** (araya girme, kesinti yönetimi)
3. **Turn-taking** (konuşma sırası belirleme)
4. **Prosody/Emotion** (ses tonu, duygu koruma)
5. **Context preservation** (kesinti sonrası bağlam koruma)
6. **Function calling** (sesle işlem yapma)
7. **Echo cancellation + Noise suppression**

---

## 1. Üç Mimari Seviye

### Seviye 1: Chained Pipeline (Cascaded STT→LLM→TTS) ⭐ BİZ BURADAYIZ
```
Voice → STT → [TEXT] → LLM → [TEXT] → TTS → Voice
```
- Her aşama bir öncekinin bitmesini bekler
- Kolay, modüler, ucuz
- **Gecikme: 1-3sn**
- Pros: esnek, debug kolay, her bileşen değiştirilebilir
- Cons: yüksek gecikme, duygu kaybı, sınırlı interrupt
- **Örnek:** Deepgram + DeepSeek + Edge-TTS → **Bizim sistemimiz**

### Seviye 2: Speech-to-Speech (Half-Cascade) ⭐ TESLA/GROK, OPENAI, GOOGLE
```
Voice → Audio Encoder → [TOKEN] → Text LLM → [TOKEN] → TTS → Voice
                       ↕ stream                     ↕ stream
```
- Ses direkt encoder'a gider, LLM text alanında düşünür
- Ses tonu/prosodi korunur
- **TTFT (Time To First Token): 0.78-0.82sn**
- Pros: düşük gecikme, doğal akış, interrupt edilebilir
- Cons: pahalı (~10x chained), TTS kalitesi düşük
- **Kullananlar:** xAI Grok (0.78sn), OpenAI gpt-realtime-1.5 (0.82sn), Google Gemini 3.1 Flash

### Seviye 3: Native Audio (End-to-End S2S) ⭐ AMAZON, MOSHI
```
Voice → [Tek Model: reasoning + audio generation] → Voice
```
- Tek sinir ağı, ses uzayında düşünür
- **En düşük gecikme: ~200-300ms**
- Pros: çok hızlı, duygu mükemmel, full-duplex
- Cons: zor kontrol, opak, pahalı, sınırlı tool calling
- **Örnek:** Amazon Nova 2 Sonic (1.14sn), Moshi (~200ms), Kimi-Audio

---

## 2. Prodüksiyon Sistemlerinin 7 Temel Bileşeni

### 2.1 VAD Pipeline (3 Katmanlı)

| Katman | Ne Yapar | Bizde Var mı? |
|--------|----------|---------------|
| Energy threshold | Sessizliği filtrele (-45dBFS altı) | ❌ YOK |
| Voice classifier | Ses mi gürültü mü? (Silero) | ✅ Silero GPU |
| Min-duration guard | 200ms altı sesleri atla (öksürük) | ❌ YOK |

**Gerçek sistemler:** 3 katmanlı VAD. Biz sadece Silero VAD kullanıyoruz. Enerji eşiği ve minimum süre koruması YOK.

### 2.2 Barge-in (Araya Girme) ⚠️ KRİTİK EKSİK

**Prodüksiyon barge-in şöyle çalışır:**
1. VAD sürekli çalışır (TTS oynarken bile)
2. Kullanıcı konuşmaya başlayınca → 200-300ms içinde algıla
3. TTS'yi **60ms içinde durdur** (flush + drop buffer)
4. LLM'i **40ms içinde iptal et** (cancel generation)
5. Bağlamı koru (kesilen cümle + araç çağrıları)
6. Yeni konuşmayı işle

**Bizde:**
- Barge-in VAD çalışıyor ama çok hassas (echo'dan tetikleniyor)
- TTS flush: ffplay kill (200-500ms, hedef 60ms)
- LLM cancel: ❌ YOK
- Context preservation: ❌ YOK

### 2.3 Turn-Taking (Konuşma Sırası)

**Prodüksiyon sistemleri 3 sinyal kullanır:**
- **VAD timeout** (300-500ms sessizlik = konuşma bitti)
- **Semantic end-of-turn** (LLM cümle yapısından konuşmanın bittiğini anlama)
- **Backchannel detection** ("mm-hmm", "evet" gibi onaylamaları konuşma sanma)

**Bizde:**
- Sadece VAD timeout (500ms) — çok basit
- Semantic EOT: ❌ YOK
- Backchannel detection: ❌ YOK

### 2.4 TTS Streaming (Cümle Bazlı)

**Prodüksiyon:**
```
LLM token → Sentence Aggregator → Tüm cümle tamam → TTS başla
                                   ↓
                           Cümle birikene kadar bekle
```

**Bizde:**
- Edge-TTS batch (tüm metni bekle → WAV oluştur → ffplay)
- Cümle bazlı chunking var ama TTS hala batch

**Hedef TTS latency: 60ms flush / 200ms sentence-to-speech**

### 2.5 Echo Cancellation (AEC)

**Prodüksiyon:**
- Her ses sisteminde AEC zorunlu
- Tesla'da: araç içi akustik ortam → özel AEC modelleri
- WebRTC AEC: standart, tüm voice AI kullanır

**Bizde:**
- PulseAudio module-echo-cancel mevcut ama test edilmedi
- Echo loop sorunu: TTS çalınca mikrofon duyuyor → tekrar VAD → döngü

### 2.6 Noise Suppression

**Prodüksiyon:**
- RNNoise / Krisp benzeri gürültü filtresi
- Atölye ortamı: matkap, öğütücü sesleri filtre edilmeli

**Bizde:** ❌ YOK

### 2.7 Context Management

**Prodüksiyon:**
- Konuşma bağlamı: son 5-10 tur
- Barge-in sonrası: kesilen cümle + yeni soru
- Session persistence: tüm konuşma kaydı

**Bizde:**
- OpenClaw session ("atolye") bağlam tutar
- Barge-in sonrası bağlam koruma: ❌ YOK

---

## 3. Detaylı Eksik Listesi (Biz vs Tesla)

| Bileşen | Tesla/Grok | Biz | Öncelik |
|---------|-----------|-----|---------|
| Mimari | S2S Half-Cascade (Seviye 2) | Chained (Seviye 1) | 🟡 Orta |
| VAD 3 katmanlı | ✅ | ❌ (sadece Silero) | 🟢 Kolay |
| Barge-in | ✅ <150ms | ❌ (kapalı) | 🔴 KRİTİK |
| TTS flush | ✅ <60ms | ❌ ~500ms ffplay kill | 🔴 KRİTİK |
| LLM cancel | ✅ <40ms | ❌ YOK | 🔴 KRİTİK |
| Semantic EOT | ✅ | ❌ YOK | 🟡 Orta |
| AEC | ✅ (araç akustik) | ⚠️ PulseAudio AEC test edilmedi | 🔴 KRİTİK |
| Noise suppression | ✅ | ❌ YOK | 🟢 Kolay |
| Context preservation | ✅ | ❌ (kesinti sonrası) | 🟡 Orta |
| Wake word | ✅ "Hey Grok" | ❌ (sonra eklenecek) | 🟢 Kolay |
| TTS quality | xAI voice engine | Edge-TTS AhmetNeural | 🟡 Orta |
| Function calling | ✅ | ✅ (OpenClaw) | ✅ |
| TTFT (gecikme) | ~0.78sn | ~3-8sn | 🔴 KRİTİK |

---

## 4. Yol Haritası (Öncelik Sırasıyla)

### Faz 1 — Temel Düzeltmeler (Hemen Yapılır)
1. **AEC'yi aktifleştir** → PulseAudio module-echo-cancel test et
2. **Barge-in aç** → VAD eşiği + minimum ses süresi (200ms guard)
3. **Enerji eşiği ekle** → -45dBFS altını sessizlik say

### Faz 2 — Streaming İyileştirme (1-2 Gün)
4. **TTS flush hızlandır** → ffplay kill → ses buffer temizliği
5. **LLM cancel** → OpenClaw WS bridge'den cancel RPC
6. **Sentence-aware streaming** → LLM tokenları cümle birikince TTS

### Faz 3 — Profesyonel Seviye (1 Hafta)
7. **3 katmanlı VAD** → energy + voice + duration guard
8. **Semantic end-of-turn** → LLM cümle yapısı analizi
9. **Backchannel detection** → "mm-hmm" gibi onayları filtrele
10. **Noise suppression** → RNNoise veya benzeri

### Faz 4 — Tesla Seviyesi (Uzun Vadeli)
11. **Wake word "Atılgan"** → openWakeWord debug
12. **Context preservation** → kesinti sonrası bağlam
13. **Prosody/emotion koruma** → ses tonunu TTS'e aktar
14. **Full-duplex** → aynı anda konuş/dinle

---

## 5. Önemli Tespitler

### 5.1 Neden "Basit Streaming" Yetmez?
Tüm bileşenler stream edilse bile:
- **VAD gecikmesi:** ~300ms
- **STT streaming:** ~200ms
- **LLM TTFT:** ~500ms (DeepSeek)
- **TTS sentence:** ~500ms
- **Toplam:** ~1500ms

> **Problem:** VAD konuşma bitti kararını 500ms'de veriyor. Ama kullanıcı daha bitirmedi.
> **Çözüm:** LLM partial transcript'ten çalışmaya başlamalı (VAD bitmeden).

### 5.2 Tesla Nasıl 0.78sn Yapıyor?
xAI Grok Voice Agent:
- **Native S2S model** (Seviye 2): ses direkt işlenir
- **Özel donanım:** Tesla AI4/AI5 çipi
- **Özel ağ:** xAI/Starlink altyapısı
- **Optimize edilmiş pipeline:** Tüm aşamalar tek kod tabanı

### 5.3 Biz Ne Yapabiliriz?
Jetson Orin Nano 8GB'da:
- Seviye 2'ye geçmek: OpenAI Realtime API ≈ $0.06/dk (pahalı)
- Seviye 1'de kalmak: 1-2sn hedef mümkün
- **En iyi strateji:** Chained Pipeline'ı optimize et (AEC + Barge-in + Streaming)

---

## 6. Kaynaklar

1. arXiv 2603.05413: Building Enterprise Realtime Voice Agents from Scratch
2. FutureAGI: Voice AI Barge-In and Turn-Taking 2026 Guide
3. Softcery: Real-Time vs Turn-Based Voice Agents Architecture
4. Towards AI: Seven Voice AI Architectures That Work in Production
5. Retell: How Real-Time Voice AI Actually Works
6. OpenAI: gpt-realtime API documentation
7. xAI: Grok Voice Agent API
