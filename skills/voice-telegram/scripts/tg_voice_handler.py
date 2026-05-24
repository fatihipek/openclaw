#!/usr/bin/env python3
"""
tg_voice_handler.py — Telegram Voice Handler
=============================================
OpenClaw içinde exec ile çağrılır. Telegram voice işlemleri:
- STT
- TTS + gönderme
- Otomatik media temizliği (inbound/outbound)

Kullanım:
  python3 tg_voice_handler.py process <file_id>           # STT yap (önce Deepgram, yoksa whisper)
  python3 tg_voice_handler.py deepgram-stt [path]         # Deepgram STT (opsiyonel: dosya yolu)
  python3 tg_voice_handler.py auto-process [chat_id]      # Otomatik: inbound oku + Deepgram STT + çıktı
  python3 tg_voice_handler.py respond <chat_id> <file_id> <metin> [reply_to]
  python3 tg_voice_handler.py tts-send <chat_id> <metin> [reply_to]  # TTS + gönder + temizle
  python3 tg_voice_handler.py clean-inbound               # En son inbound dosyayı sil
"""

import io
import json
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ─── Deepgram (STT cloud API) ────────────────────────────────────────────────
# Önce .env dosyasından oku, yoksa ortam değişkenine bak
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '.env')
if not os.environ.get("DEEPGRAM_API_KEY"):
    try:
        with open(_env_path) as f:
            for line in f:
                if line.startswith("DEEPGRAM_API_KEY="):
                    os.environ["DEEPGRAM_API_KEY"] = line.strip().split("=", 1)[1]
                    break
    except:
        pass

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_LANGUAGE = "tr"
log = logging.getLogger('tg_voice_handler')

BOT_TOKEN = '8573120289:AAF6gWrA616dAGKRz1WPqL6LRaj6-AYq21o'
API = f'https://api.telegram.org/bot{BOT_TOKEN}'
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
OPENCLAW_DIR = os.path.expanduser('~/.openclaw')

# Media dizinleri
MEDIA_INBOUND = os.path.join(OPENCLAW_DIR, 'media', 'inbound')
MEDIA_OUTBOUND = os.path.join(OPENCLAW_DIR, 'media', 'outbound')


# ─── Media Temizliği ─────────────────────────────────────────────────────────

def _clean_latest_file(directory: str) -> bool:
    """Belirtilen dizinde en son değiştirilmiş dosyayı siler.
    
    Args:
        directory: Temizlenecek dizin yolu
        
    Returns:
        Başarılı mı?
    """
    if not os.path.isdir(directory):
        log.info(f"   📭 Dizin yok: {directory}")
        return False
    
    files = sorted(
        [f for f in Path(directory).iterdir() if f.is_file()],
        key=os.path.getmtime,
        reverse=True
    )
    
    if not files:
        log.info(f"   📭 Silinecek dosya yok ({directory})")
        return False
    
    newest = files[0]
    try:
        os.remove(newest)
        log.info(f"   🧹 Temizlendi: {newest.name}")
        return True
    except Exception as e:
        log.warning(f"   ⚠️ Temizlik hatası ({newest.name}): {e}")
        return False


def _clean_all_old_inbound(keep: int = 0) -> int:
    """Eski inbound media dosyalarını temizler.
    
    Args:
        keep: En son kaç dosya kalsın (0=hepsi silinsin)
        
    Returns:
        Silinen dosya sayısı
    """
    if not os.path.isdir(MEDIA_INBOUND):
        return 0
    
    files = sorted(
        [f for f in Path(MEDIA_INBOUND).iterdir() if f.is_file()],
        key=os.path.getmtime,
        reverse=True
    )
    
    deleted = 0
    for f in files[keep:]:
        try:
            os.remove(f)
            deleted += 1
        except Exception as e:
            log.warning(f"   ⚠️ Temizlik hatası: {f.name}: {e}")
    
    if deleted > 0:
        log.info(f"   🧹 {deleted} eski inbound dosya temizlendi")
    return deleted


def _clean_latest_outbound() -> bool:
    """En son gönderilen outbound ses dosyasını siler."""
    return _clean_latest_file(MEDIA_OUTBOUND)


def _clean_latest_inbound() -> bool:
    """En son gelen inbound ses dosyasını siler."""
    return _clean_latest_file(MEDIA_INBOUND)


def _clean_tmp_tg_voice() -> int:
    """/tmp/tg_voice/ dizinindeki tüm dosyaları temizler."""
    tmp_dir = '/tmp/tg_voice'
    if not os.path.isdir(tmp_dir):
        return 0
    
    deleted = 0
    for f in Path(tmp_dir).iterdir():
        if f.is_file():
            try:
                os.remove(f)
                deleted += 1
            except Exception as e:
                log.warning(f"   ⚠️ Temp temizlik: {f.name}: {e}")
    
    if deleted > 0:
        log.info(f"   🧹 {deleted} temp dosya temizlendi ({tmp_dir})")
    return deleted


# ─── STT ─────────────────────────────────────────────────────────────────────

def run_deepgram_stt(audio_path: str = None) -> str:
    """En son inbound ses dosyasını Deepgram Nova-3 ile transkript et.
    
    Dosyayı inbound'dan okur, Deepgram API'ye POST eder, transkript döndürür.
    İşlem sonrası temp dosya temizlenir.
    """
    if not DEEPGRAM_API_KEY:
        return "[HATA] DEEPGRAM_API_KEY bulunamadı"
    
    # En son inbound dosyayı bul (path verilmemişse)
    if not audio_path:
        if not os.path.isdir(MEDIA_INBOUND):
            return "[HATA] Inbound dizini yok"
        files = sorted(
            [f for f in Path(MEDIA_INBOUND).iterdir() if f.is_file()],
            key=os.path.getmtime, reverse=True
        )
        if not files:
            return "[HATA] İnbound'da ses dosyası yok"
        audio_path = str(files[0])
    
    log.info(f"   🎤 Deepgram STT: {audio_path}")
    
    # Deepgram API çağrısı
    url = f"https://api.deepgram.com/v1/listen?model={DEEPGRAM_MODEL}&language={DEEPGRAM_LANGUAGE}&smart_format=true&punctuate=true"
    
    try:
        with open(audio_path, 'rb') as f:
            audio_data = f.read()
        
        req = urllib.request.Request(url, data=audio_data)
        req.add_header('Authorization', f'Token {DEEPGRAM_API_KEY}')
        req.add_header('Content-Type', 'audio/ogg')
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        
        transcript = result['results']['channels'][0]['alternatives'][0]['transcript']
        if not transcript:
            transcript = "[ses algılanamadı]"
        
        log.info(f"   ✅ STT: {transcript}")
        return transcript.strip()
        
    except urllib.error.HTTPError as e:
        hata = e.read().decode()[:200]
        log.error(f"Deepgram HTTP {e.code}: {hata}")
        return f"[HATA] Deepgram HTTP {e.code}"
    except Exception as e:
        log.error(f"Deepgram: {e}")
        return f"[HATA] Deepgram: {e}"


def run_stt(file_id: str) -> str:
    """Voice mesajını Deepgram STT ile transkript et.
    
    OpenClaw inbound'daki dosyayı kullanır. Deepgram yoksa whisper yedek.
    İşlem sonrası inbound media otomatik temizlenir.
    """
    # Önce Deepgram dene (cloud, hızlı)
    if DEEPGRAM_API_KEY:
        text = run_deepgram_stt()
        if text and not text.startswith("[HATA"):
            _clean_latest_inbound()
            return text
    
    # Deepgram yoksa/hatalıysa whisper yedek
    log.info("   ⚠️ Deepgram yok/hatalı, whisper yedek kullanılıyor")
    result = subprocess.run(
        [sys.executable, os.path.join(WORKSPACE, 'tg_voice.py'), 'download', file_id],
        capture_output=True, text=True, timeout=120
    )
    
    output = result.stdout + result.stderr
    text = ''
    for line in output.split('\n'):
        if line.startswith('📝 Transkripsiyon:'):
            text = line.split(':', 1)[1].strip()
            break
    if not text:
        lines = [l for l in output.split('\n') if l.strip()]
        if lines:
            text = lines[-1]
    
    # OpenClaw inbound medyayı temizle (Talimat #8)
    _clean_latest_inbound()
    
    return text


# ─── TTS + Gönder + Temizle ─────────────────────────────────────────────────

def tts_and_send(text: str, chat_id: int, reply_to: int = None) -> bool:
    """Metni seslendir, Telegram'a voice mesajı olarak gönder, tüm temp dosyaları temizle."""
    # İşlem öncesi /tmp/tg_voice/ eski dosyaları temizle
    _clean_tmp_tg_voice()
    # TTS yap
    result = subprocess.run(
        [sys.executable, os.path.join(WORKSPACE, 'tg_voice.py'), 'tts', text],
        capture_output=True, text=True, timeout=60
    )
    
    # WAV dosyasını bul
    wav_path = None
    for line in (result.stdout + result.stderr).split('\n'):
        if 'kaydedildi:' in line:
            wav_path = line.split('kaydedildi:')[-1].strip()
            break
    
    if not wav_path or not os.path.exists(wav_path):
        for line in (result.stdout + result.stderr).split('\n'):
            if '.wav' in line:
                parts = line.split()
                for p in parts:
                    if p.endswith('.wav') and os.path.exists(p):
                        wav_path = p
                        break
    
    if not wav_path:
        print("❌ TTS çıktı dosyası bulunamadı", file=sys.stderr)
        return False
    
    # OGG'ye çevir
    ogg_path = wav_path + '.ogg'
    subprocess.run([
        'ffmpeg', '-y', '-i', wav_path,
        '-c:a', 'libopus', '-b:a', '32k', '-ar', '24000', '-ac', '1',
        ogg_path
    ], capture_output=True, check=True)
    
    # Telegram'a yükle
    boundary = '----VoiceBoundary'
    
    with open(ogg_path, 'rb') as f:
        file_data = f.read()
    
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f'{chat_id}\r\n'
    ).encode()
    
    if reply_to:
        body += (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="reply_to_message_id"\r\n\r\n'
            f'{reply_to}\r\n'
        ).encode()
    
    body += (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="voice"; filename="voice.ogg"\r\n'
        f'Content-Type: audio/ogg\r\n\r\n'
    ).encode() + file_data + f'\r\n--{boundary}--\r\n'.encode()
    
    req = urllib.request.Request(f'{API}/sendVoice', data=body)
    req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
    
    success = False
    try:
        with urllib.request.urlopen(req) as resp:
            result_json = json.loads(resp.read())
        if result_json.get('ok'):
            print(f"✅ Voice mesajı gönderildi (chat: {chat_id})")
            success = True
        else:
            print(f"❌ Telegram hatası: {result_json.get('description', '?')}")
    except Exception as e:
        print(f"❌ Gönderme hatası: {e}")
    finally:
        # Temp dosyaları temizle (TTS WAV, OGG)
        for f in [ogg_path, wav_path]:
            try: os.remove(f)
            except: pass
    
    # Başarılıysa outbound ve inbound kopyalarını temizle
    if success:
        _clean_latest_outbound()
        _clean_latest_inbound()
    
    return success


def get_file_id_from_update(update: dict) -> tuple:
    """Telegram update'inden voice mesajı file_id ve chat_id'sini çıkarır."""
    msg = update.get('message', {})
    voice = msg.get('voice')
    if voice:
        return voice['file_id'], msg['chat']['id'], msg['message_id']
    return None, None, None


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Kullanım:")
        print(f"  {sys.argv[0]} process <file_id>")
        print(f"  {sys.argv[0]} respond <chat_id> <file_id> <reply_to>")
        print(f"  {sys.argv[0]} tts-send <chat_id> <metin> [reply_to]")
        print(f"  {sys.argv[0]} clean-inbound              # En son gelen inbound dosyayı sil")
        print(f"  {sys.argv[0]} clean-outbound             # En son giden outbound dosyayı sil")
        print(f"  {sys.argv[0]} clean-all [keep]           # Tüm eski inbound'ları sil (opsiyonel: son X dosyayı tut)")
        print(f"  {sys.argv[0]} clean-tmp                   # /tmp/tg_voice/ temp dosyalarını temizle")
        sys.exit(1)
    
    action = sys.argv[1]
    
    if action == 'process':
        file_id = sys.argv[2]
        text = run_stt(file_id)
        print(text)
    
    elif action == 'deepgram-stt':
        # En son inbound dosyayı Deepgram ile transkript et
        audio_path = sys.argv[2] if len(sys.argv) > 2 else None
        text = run_deepgram_stt(audio_path)
        print(text)
    
    elif action == 'auto-process':
        # Otomatik: inbound'u oku, Deepgram STT yap, transkripti bas
        # Opsiyonel: chat_id verilirse direkt TTS cevap da gönder
        text = run_deepgram_stt()
        print(text)
        if text and text.startswith("[HATA"):
            sys.exit(1)
    
    elif action == 'respond':
        chat_id = int(sys.argv[2])
        file_id = sys.argv[3]
        reply_to = int(sys.argv[4]) if len(sys.argv) > 4 else None
        
        print("🎤 Ses işleniyor...", file=sys.stderr)
        stt_text = run_stt(file_id)
        print(f"   STT: {stt_text}", file=sys.stderr)
        
        # STT sonucunu stdout'a yaz
        print(stt_text)
    
    elif action == 'tts-send':
        chat_id = int(sys.argv[2])
        text = sys.argv[3]
        reply_to = int(sys.argv[4]) if len(sys.argv) > 4 else None
        success = tts_and_send(text, chat_id, reply_to)
        sys.exit(0 if success else 1)
    
    elif action == 'clean-inbound':
        ok = _clean_latest_inbound()
        print(f"{'✅' if ok else 'ℹ️'} Inbound temizlendi" if ok else "Silinecek inbound dosya yok")
    
    elif action == 'clean-outbound':
        ok = _clean_latest_outbound()
        print(f"{'✅' if ok else 'ℹ️'} Outbound temizlendi" if ok else "Silinecek outbound dosya yok")
    
    elif action == 'clean-all':
        keep = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        count = _clean_all_old_inbound(keep)
        _clean_latest_outbound()
        tmp_count = _clean_tmp_tg_voice()
        print(f"✅ {count} inbound + {tmp_count} temp dosya temizlendi (son {keep} inbound tutuldu)")
    
    elif action == 'clean-tmp':
        count = _clean_tmp_tg_voice()
        print(f"✅ {count} temp dosya temizlendi")
    
    else:
        print(f"Bilinmeyen aksiyon: {action}", file=sys.stderr)
        sys.exit(1)
