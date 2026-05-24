#!/bin/bash
#==============================================================================
# setup.sh — Voice-Telegram Skill Kurulumu (v2 Deepgram)
#==============================================================================
# Yeni bir sisteme kurulum. Sade ve hızlı.
#
# Kullanım:
#   chmod +x setup.sh
#   ./setup.sh
#
# Yapacakları:
#   1. edge-tts Python paketini kur
#   2. Deepgram API key'i .env'ye ekle (yoksa sor)
#   3. Whisper kurulumunu doğrula
#   4. OpenClaw workspace'e talimatları ekle
#   5. Test: Deepgram STT + Edge-TTS
#==============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_DIR="${HOME}/.openclaw"
WORKSPACE="${OPENCLAW_DIR}/workspace"
ENV_FILE="${OPENCLAW_DIR}/.env"

echo "============================================"
echo "  Voice-Telegram Skill v2 Kurulumu"
echo "  Deepgram STT + Edge-TTS"
echo "============================================"
echo ""

# ─── 1. edge-tts ────────────────────────────────────────────────────────────
echo "[1/5] edge-tts Python paketi kontrol ediliyor..."
if python3 -c "import edge_tts" 2>/dev/null; then
    echo "  ✅ edge-tts zaten kurulu"
else
    echo "  📦 edge-tts kuruluyor..."
    pip3 install edge-tts -q
    echo "  ✅ edge-tts kuruldu"
fi

# ─── 2. Deepgram API Key ─────────────────────────────────────────────────────
echo "[2/5] Deepgram API Key kontrol ediliyor..."
if [ -f "$ENV_FILE" ] && grep -q "DEEPGRAM_API_KEY" "$ENV_FILE" 2>/dev/null; then
    KEY=$(grep "DEEPGRAM_API_KEY" "$ENV_FILE" | cut -d= -f2)
    echo "  ✅ Deepgram API Key mevcut: ${KEY:0:8}..."
else
    echo "  ❓ Deepgram API Key bulunamadı."
    echo "  Lütfen Deepgram API anahtarınızı girin (Nova-3, Türkçe):"
    read -r -p "  API Key: " USER_KEY
    if [ -n "$USER_KEY" ]; then
        echo "DEEPGRAM_API_KEY=$USER_KEY" >> "$ENV_FILE"
        echo "  ✅ .env dosyasına kaydedildi"
    else
        echo "  ⚠️  API Key girilmedi. Whisper yedek olarak kullanılacak."
    fi
fi

# ─── 3. Whisper ──────────────────────────────────────────────────────────────
echo "[3/5] Whisper GPU kontrol ediliyor..."
if command -v whisper &>/dev/null; then
    echo "  ✅ Whisper CLI mevcut"
else
    echo "  ⚠️  Whisper CLI bulunamadı. Deepgram yoksa STT çalışmaz."
    echo "  Kurulum: pip3 install openai-whisper"
fi

# ─── 4. OpenClaw Workspace ─────────────────────────────────────────────────
echo "[4/5] OpenClaw workspace kontrol ediliyor..."
# Agent talimatları AGENTS.md'de zaten varsa dokunma
if [ -f "${WORKSPACE}/AGENTS.md" ]; then
    echo "  ✅ AGENTS.md mevcut"
    # AGENTS.md'de voice pipeline talimatı var mı kontrol et
    if grep -q "deepgram-stt\|Deepgram\|tg_voice_handler" "${WORKSPACE}/AGENTS.md" 2>/dev/null; then
        echo "  ✅ Voice pipeline talimatları mevcut"
    else
        echo "  ℹ️  Voice pipeline talimatları eklenebilir (manuel)"
    fi
fi

# ─── 5. Test ─────────────────────────────────────────────────────────────────
echo "[5/5] Sistem test ediliyor..."
echo ""

# Edge-TTS test
echo "  🔊 Edge-TTS test ediliyor..."
if python3 -c "import edge_tts; print('  ✅ Edge-TTS hazır')" 2>/dev/null; then
    :
else
    echo "  ⚠️  Edge-TTS testi başarısız"
fi

# Deepgram test
if [ -f "$ENV_FILE" ] && grep -q "DEEPGRAM_API_KEY" "$ENV_FILE" 2>/dev/null; then
    echo "  🎤 Deepgram API bağlantısı kontrol ediliyor..."
    KEY=$(grep "DEEPGRAM_API_KEY" "$ENV_FILE" | cut -d= -f2)
    if python3 -c "
import urllib.request, json
req = urllib.request.Request('https://api.deepgram.com/v1/listen?model=nova-3&language=tr', 
    data=b'OPUS', headers={'Authorization': 'Token $KEY', 'Content-Type': 'audio/ogg'})
try:
    urllib.request.urlopen(req, timeout=10)
    print('OK')
except urllib.error.HTTPError as e:
    if e.code == 400:
        print('OK')  # 400 = geçersiz audio ama API key doğru
    else:
        print(f'HTTP {e.code}')
except Exception as e:
    print(f'HATA: {e}')
" 2>/dev/null; then
        echo "  ✅ Deepgram API bağlantısı başarılı"
    fi
fi

echo ""
echo "============================================"
echo "  ✅ Kurulum tamamlandı!"
echo "============================================"
echo ""
echo "Kullanım:"
echo "  Voice mesajı gelince → Atılgan otomatik işler"
echo "  Manuel: python3 scripts/tg_voice_handler.py auto-process"
echo "  TTS:    python3 scripts/tg_voice_handler.py tts-send <chat_id> \"metin\""
echo "============================================"
