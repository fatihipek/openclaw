#!/bin/bash
#==============================================================================
# setup_voice.sh — Atılgan Ses Sistemi Tek Tık Kurulum
#==============================================================================
# Bu script yeni bir Jetson Orin Nano / OpenClaw kurulumunda
# tüm ses altyapısını ayağa kaldırır.
#
# Kullanım:
#   chmod +x setup_voice.sh
#   sudo ./setup_voice.sh
#
# Yapacakları:
#   1. Sistem bağımlılıkları (ffmpeg, espeak-ng, libportaudio2)
#   2. Python paketleri (piper-tts, faster-whisper, onnxruntime-gpu)
#   3. Fahrettin TTS modeli (HuggingFace)
#   4. piper_tr_fix.py (sistem geneli fonem düzeltmesi)
#   5. Systemd servisler (gateway + voice service)
#   6. OpenClaw Telegram config
#   7. Test
#
# Gereksinimler:
#   - Ubuntu/Jetson (aarch64), Python 3.10
#   - OpenClaw kurulu olmalı
#   - sudo yetkisi
#==============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE="${HOME}/.openclaw/workspace"
SITE_PKG=$(python3 -c "import site; print(site.getusersitepackages())")
PIPER_MODELS="${WORKSPACE}/piper-models"
TG_OUTPUT="/tmp/tg_voice"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅${NC} $1"; }
warn() { echo -e "${YELLOW}⚠️${NC} $1"; }
fail() { echo -e "${RED}❌${NC} $1"; }

echo -e "${CYAN}══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  🚀 Atılgan Ses Sistemi Kurulumu${NC}"
echo -e "${CYAN}══════════════════════════════════════════════${NC}"
echo ""

# ──────── 1. Sistem Bağımlılıkları ────────
echo -e "\n${YELLOW}[1/7] Sistem bağımlılıkları kuruluyor...${NC}"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg espeak-ng libportaudio2 curl 2>/dev/null
ok "ffmpeg, espeak-ng, libportaudio2, curl"

# ──────── 2. Python Paketleri ────────
echo -e "\n${YELLOW}[2/7] Python paketleri kuruluyor...${NC}"

# NumPy < 2 (Piper uyumluluğu için)
pip install 'numpy<2' -q 2>/dev/null
ok "numpy<2"

# onnxruntime-gpu
pip install onnxruntime-gpu --extra-index-url https://pypi.jetson-ai-lab.io/jp6/cu126 -q 2>/dev/null
ok "onnxruntime-gpu (CUDA 12.6)"

# Piper TTS
pip install piper-tts -q 2>/dev/null
ok "piper-tts"

# faster-whisper
pip install faster-whisper -q 2>/dev/null
ok "faster-whisper"

# edge-tts (Microsoft Cloud TTS, birincil ses motoru)
pip install edge-tts -q 2>/dev/null
ok "edge-tts (Microsoft Cloud TTS)"

# soundfile
pip install soundfile -q 2>/dev/null
ok "soundfile"

# ──────── 3. Türkçe TTS Modeli ────────
echo -e "\n${YELLOW}[3/7] Fahrettin TTS modeli indiriliyor...${NC}"
mkdir -p "${PIPER_MODELS}"

# Fahrettin-medium (Türkçe)
# HuggingFace commit 217ddc7'den indir
if [ ! -f "${PIPER_MODELS}/tr_TR-fahrettin-medium.onnx" ]; then
    curl -sL -o "${PIPER_MODELS}/tr_TR-fahrettin-medium.onnx" \
      "https://huggingface.co/rhasspy/piper-voices/resolve/217ddc7/tr/tr_TR/fahrettin/medium/tr_TR-fahrettin-medium.onnx"
    curl -sL -o "${PIPER_MODELS}/tr_TR-fahrettin-medium.onnx.json" \
      "https://huggingface.co/rhasspy/piper-voices/resolve/217ddc7/tr/tr_TR/fahrettin/medium/tr_TR-fahrettin-medium.onnx.json"
    ok "Fahrettin-medium modeli indirildi"
else
    ok "Fahrettin-medium zaten var"
fi

# ──────── 4. Fonem Düzeltme (piper_tr_fix) ────────
echo -e "\n${YELLOW}[4/7] Fonem düzeltme modülü kuruluyor...${NC}"
mkdir -p "${SITE_PKG}"

# piper_tr_fix.py'yi site-packages'e kopyala
cp "${SKILL_DIR}/scripts/piper_tr_fix.py" "${SITE_PKG}/piper_tr_fix.py"
ok "piper_tr_fix.py → site-packages"

# .pth dosyası ile otomatik yükleme
echo "import piper_tr_fix" > "${SITE_PKG}/piper_tr_fix_startup.pth"
ok ".pth otomatik yükleme aktif"

# Test: yeni Python process'inde patch çalışıyor mu?
python3 -c "import piper_tr_fix; print('OK: piper_tr_fix yuklendi')" 2>/dev/null
ok "piper_tr_fix testi gecti"

# ──────── 5. Scriptler ────────
echo -e "\n${YELLOW}[5/7] Scriptler kopyalanıyor...${NC}"

# tg_voice.py → workspace
cp "${SKILL_DIR}/scripts/tg_voice.py" "${WORKSPACE}/tg_voice.py"
chmod +x "${WORKSPACE}/tg_voice.py"
ok "tg_voice.py → workspace"

# tg_voice_handler.py → workspace
cp "${SKILL_DIR}/scripts/tg_voice_handler.py" "${WORKSPACE}/tg_voice_handler.py"
chmod +x "${WORKSPACE}/tg_voice_handler.py"
ok "tg_voice_handler.py → workspace"

# Skill dizinine plugin-skills linki oluştur
PLUGIN_SKILL_DIR="${HOME}/.openclaw/plugin-skills/voice-telegram"
mkdir -p "${PLUGIN_SKILL_DIR}/scripts"
cp "${SKILL_DIR}/SKILL.md" "${PLUGIN_SKILL_DIR}/SKILL.md"
cp "${SKILL_DIR}/scripts/tg_voice.py" "${PLUGIN_SKILL_DIR}/scripts/tg_voice.py"
cp "${SKILL_DIR}/scripts/tg_voice_handler.py" "${PLUGIN_SKILL_DIR}/scripts/tg_voice_handler.py"
cp "${SKILL_DIR}/scripts/tg_voice_service.py" "${PLUGIN_SKILL_DIR}/scripts/tg_voice_service.py"
ok "plugin-skills/voice-telegram guncellendi"

# Temp dizin oluştur
mkdir -p "${TG_OUTPUT}"
ok "temp dizin: ${TG_OUTPUT}"

# ──────── 6. Servisler ────────
echo -e "\n${YELLOW}[6/7] Systemd servisler kuruluyor...${NC}"

# OpenClaw Gateway servisi
if [ -f "${SKILL_DIR}/assets/openclaw-gateway.service" ]; then
    sudo cp "${SKILL_DIR}/assets/openclaw-gateway.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable openclaw-gateway 2>/dev/null
    ok "openclaw-gateway.service → enabled"
else
    warn "openclaw-gateway.service asset bulunamadi, atlaniyor"
fi

# Voice servisi
if [ -f "${SKILL_DIR}/assets/atilgan-voice.service" ]; then
    sudo cp "${SKILL_DIR}/assets/atilgan-voice.service" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable atilgan-voice 2>/dev/null
    ok "atilgan-voice.service → enabled"
else
    warn "atilgan-voice.service asset bulunamadi, atlaniyor"
fi

# ──────── 7. Konfigürasyon ────────
echo -e "\n${YELLOW}[7/7] OpenClaw Telegram konfigürasyonu...${NC}"

CONFIG_FILE="${HOME}/.openclaw/openclaw.json"
if [ -f "${CONFIG_FILE}" ]; then
    python3 -c "
import json
with open('${CONFIG_FILE}') as f:
    cfg = json.load(f)

# Mevcut token'ı koru, dmPolicy ekle
tg = cfg.get('channels', {}).get('telegram', {})
current_token = tg.get('botToken', '')

if 'channels' not in cfg:
    cfg['channels'] = {}
cfg['channels']['telegram'] = {
    'enabled': True,
    'groups': {'*': {'requireMention': False}},
    'allowFrom': ['1833986772'],
    'dmPolicy': 'allowlist',
    'botToken': current_token,
}

with open('${CONFIG_FILE}', 'w') as f:
    json.dump(cfg, f, indent=2)

if current_token:
    print('OK: Telegram config korundu (dmPolicy=allowlist)')
else:
    print('⚠️ Bot token bulunamadi! openclaw.json\'a el ile ekle:')
    print('   botToken: \"TOKEN\"')
" 2>/dev/null
    ok "openclaw.json guncellendi (dmPolicy=allowlist)"
else
    warn "openclaw.json bulunamadi, atlaniyor"
fi

# ──────── Test ────────
echo ""
echo -e "${CYAN}══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  🔍 Testler Başlıyor...${NC}"
echo -e "${CYAN}══════════════════════════════════════════════${NC}"

# Test 1: Python import
python3 -c "
import sys
sys.path.insert(0, '${WORKSPACE}')
from piper_tr_fix import patch_piper
patch_piper()
from tg_voice import voice_pipeline
print('OK: tg_voice pipeline import edildi')
" 2>/dev/null && ok "Python import testi" || fail "Python import"

# Test 2: TTS
python3 -c "
import sys, numpy as np, soundfile as sf
sys.path.insert(0, '${WORKSPACE}')
from piper_tr_fix import patch_piper; patch_piper()
from piper.voice import PiperVoice
from piper.config import SynthesisConfig
voice = PiperVoice.load('${PIPER_MODELS}/tr_TR-fahrettin-medium.onnx', use_cuda=True)
cfg = SynthesisConfig(length_scale=1.2, noise_scale=0.667, noise_w_scale=0.8)
audio = [c.audio_float_array for c in voice.synthesize('Merhaba dunya', syn_config=cfg)]
sf.write('${TG_OUTPUT}/setup_test.wav', np.concatenate(audio), 22050)
print(f'OK: TTS calisti (len_scale=1.2)')
" 2>/dev/null && ok "TTS testi (GPU)" || fail "TTS basarisiz"

# Test 3: Fonem düzeltmesi
python3 -c "
from piper_tr_fix import TURKISH_PHONEME_FIX
# Test: en az 3 düzeltme var mı
assert len(TURKISH_PHONEME_FIX) >= 5, 'Yetersiz fonem haritasi'
# Test: düzeltme çalışıyor mu
test_phonemes = ['m', 'ˈ', 'ɛ', 'r', 'h', 'a', 'b', 'a']
fixed = [TURKISH_PHONEME_FIX.get(c, c) for c in test_phonemes]
assert 'ɛ' not in fixed, 'ɛ düzeltilmedi!'
assert 'e' in fixed, 'e eklenmedi!'
print('OK: fonem duzeltmesi calisiyor')
" 2>/dev/null && ok "Fonem düzeltme testi" || fail "Fonem düzeltme basarisiz"

# Temizlik
rm -f "${TG_OUTPUT}/setup_test.wav"

echo ""
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ KURULUM TAMAMLANDI!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo ""
echo "Kullanici ID: 1833986772 (Fatih)"
echo "Gateway servisi: sudo systemctl restart openclaw-gateway"
echo "Voice servisi:   sudo systemctl start atilgan-voice"
echo ""
echo "Test: python3 ${WORKSPACE}/tg_voice_handler.py tts-send 1833986772 \"Merhaba sistem hazir\""
echo ""
