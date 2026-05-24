#!/bin/bash
# Atılgan Voice-Chat Skill V5 — Tek Tık Kurulum
# Kullanım: bash setup.sh

set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "📦 Atılgan Voice-Chat V5 Kurulumu"
echo "========================================"

# 1. Python kütüphaneleri
echo ""
echo "📚 Python kütüphaneleri..."
pip3 install deepgram-sdk websockets edge-tts sounddevice numpy --quiet 2>/dev/null
echo "  ✅ Kütüphaneler hazır"

# Silero VAD
if python3 -c "from silero_vad import load_silero_vad" 2>/dev/null; then
    echo "  ✅ silero-vad yüklü"
else
    pip3 install silero-vad --quiet 2>/dev/null
    echo "  ✅ silero-vad kuruldu"
fi

# 2. Scriptleri kopyala
echo ""
echo "📋 Scriptler kopyalanıyor..."
cp "$SKILL_DIR/scripts/voice_chat.py" /home/atilgan/.openclaw/workspace/scripts/
echo "  ✅ voice_chat.py → workspace/scripts/"

# 3. Servisi kur
echo ""
echo "🔧 Servis kuruluyor..."
sudo cp "$SKILL_DIR/service/atolye-voice.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable atolye-voice.service 2>/dev/null
echo "  ✅ Servis yüklendi"

# 4. PulseAudio AEC
echo ""
echo "🎤 AEC (Echo Cancellation)..."
pactl set-default-source aec_source 2>/dev/null && echo "  ✅ aec_source mevcut" || echo "  ⚠️  Servis ilk çalışmada AEC'yi yükleyecek"

# 5. Servisi başlat
echo ""
echo "🚀 Servis başlatılıyor..."
sudo systemctl restart atolye-voice.service
sleep 5
if systemctl is-active --quiet atolye-voice.service; then
    echo "  ✅ Servis çalışıyor!"
else
    echo "  ⚠️  Servis başlatılamadı. Log:"
    journalctl -u atolye-voice.service --no-pager -n 5
fi

echo ""
echo "========================================"
echo "✅ Kurulum tamamlandı!"
echo ""
echo "Loglari izlemek icin:"
echo "  journalctl -u atolye-voice.service -f"
echo ""
echo "Konuşmaya baslamak icin:"
echo "  DMK003 mikrofonu tak → 'Atılgan merhaba' de"
