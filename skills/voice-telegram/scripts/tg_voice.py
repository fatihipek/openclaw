#!/usr/bin/env python3
"""
tg_voice.py — Telegram Ses Asistanı
=====================================
Atılgan'ın ses sistemi: STT (faster-whisper CPU) + TTS (Fahrettin GPU)
Telegram voice message'larını indirir, yazıya çevirir, yanıt üretir,
TTS ile seslendirip geri gönderir.

Kullanım:
  from tg_voice import voice_pipeline
  
  # Ses dosyasını yazıya çevir
  text = voice_pipeline.stt('/tmp/gelen_ses.wav')
  
  # Yazıyı sese çevir
  audio_path = voice_pipeline.tts('Merhaba Fatih')
  
  # Telegram file_id ile direkt indir + STT
  text = voice_pipeline.download_and_stt('AwACAgQAAxkB...')
  
  # Tam pipeline
  text, audio_path = voice_pipeline.full_pipeline(telegram_file_id='AwACAgQAAxkB...')
"""

import asyncio
import edge_tts
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import soundfile as sf

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('tg_voice')

# ─── Yapılandırma ────────────────────────────────────────────────────────────

# Türkçe fonem düzeltme haritası
# espeak-ng Türkçe'de hatalı IPA fonemleri üretiyor:
#   'ɛ' açık "e" (İngilizce "bed") yerine doğru Türkçe 'e' (kapalı)
#   'ɪ' neredeyse-kapalı "i" yerine doğru Türkçe 'i' (kapalı)
#   'æ' neredeyse-açık "e" (İngilizce "cat") yerine doğru Türkçe 'e'
#   'ɔ' açık "o" yerine doğru Türkçe 'o' (kapalı)
TURKISH_PHONEME_FIX = {
    'ɛ': 'e',  # open-mid front unrounded → close-mid front unrounded (Türkçe "e")
    'ɪ': 'i',  # near-close near-front unrounded → close front unrounded (Türkçe "i")
    'æ': 'e',  # near-open front unrounded → close-mid front unrounded (Türkçe "e")
    'ɔ': 'o',  # open-mid back rounded → close-mid back rounded (Türkçe "o")
    'ɒ': 'o',  # open back rounded → close-mid back rounded
    'ʊ': 'u',  # near-close near-back rounded → close back rounded (Türkçe "u")
    'ə': 'e',  # schwa → close-mid front unrounded
}

CONFIG = {
    # Telegram
    'bot_token': '',  # TELEGRAM_BOT_TOKEN env'inden alınır
    
    # STT (faster-whisper)
    'stt_model': 'base',
    'stt_device': 'cpu',
    'stt_compute': 'int8',
    'stt_language': 'tr',
    
    # TTS (Fahrettin / Piper)
    'tts_model': '/home/atilgan/.openclaw/workspace/piper-models/tr_TR-fahrettin-medium.onnx',
    'tts_use_cuda': True,
    
    # Çıktı
    'output_dir': '/tmp/tg_voice',
}

# Bot token: önce env, sonra openclaw.json
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not BOT_TOKEN:
    try:
        import json
        oc = json.load(open(os.path.expanduser('~/.openclaw/openclaw.json')))
        BOT_TOKEN = oc.get('channels',{}).get('telegram',{}).get('botToken','')
    except:
        pass
TELEGRAM_API = f'https://api.telegram.org/bot{BOT_TOKEN}'


# ─── STT Motoru ──────────────────────────────────────────────────────────────

class STTEngine:
    """faster-whisper ile konuşma tanıma (CPU)."""
    
    def __init__(self):
        self._model = None
    
    def _load(self):
        if self._model is not None:
            return
        log.info(f"🔊 STT yükleniyor: {CONFIG['stt_model']} ({CONFIG['stt_device']}, {CONFIG['stt_compute']})")
        t0 = time.time()
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            CONFIG['stt_model'],
            device=CONFIG['stt_device'],
            compute_type=CONFIG['stt_compute'],
        )
        log.info(f"   ✅ {time.time()-t0:.2f}s")
    
    def transcribe(self, audio_path: str, language: str = None) -> str:
        """Ses dosyasını yazıya çevirir.
        
        Args:
            audio_path: WAV dosyası yolu (16kHz mono önerilir)
            language: Dil kodu (None=otomatik)
            
        Returns:
            Transkribe edilmiş metin
        """
        self._load()
        lang = language or CONFIG['stt_language']
        t0 = time.time()
        segments, info = self._model.transcribe(
            audio_path,
            language=lang,
            beam_size=5,
        )
        text = ' '.join(s.text for s in segments)
        duration = os.path.getsize(audio_path) / (16000 * 2)  # yaklaşık
        log.info(f"   📝 STT: {time.time()-t0:.3f}s | dil: {info.language} (%{info.language_probability*100:.1f})")
        return text.strip()


# ─── TTS Motoru (Hibrit: Edge-TTS → Piper Fahrettin) ─────────────────────────

class TTSHybridEngine:
    """Hibrit TTS Motoru.
    
    Sıralama:
    1. Edge-TTS (Microsoft cloud) — doğal, ücretsiz, hızlı
    2. Piper Fahrettin (GPU) — yedek, offline çalışır, robotik
    """
    
    def __init__(self):
        self._piper_voice = None
    
    def _load_piper(self):
        if self._piper_voice is not None:
            return
        log.info(f"🔊 Piper TTS yükleniyor: {CONFIG['tts_model']} (CUDA={CONFIG['tts_use_cuda']})")
        t0 = time.time()
        from piper.voice import PiperVoice
        self._piper_voice = PiperVoice.load(CONFIG['tts_model'], use_cuda=CONFIG['tts_use_cuda'])
        log.info(f"   ✅ {time.time()-t0:.2f}s")
    
    def _piper_synthesize(self, text: str, output_path: str) -> str:
        """Piper Fahrettin ile TTS (GPU, yedek)."""
        self._load_piper()
        from piper.config import SynthesisConfig
        
        cfg = SynthesisConfig(
            length_scale=CONFIG.get('tts_length_scale', 1.2),
            noise_scale=CONFIG.get('tts_noise_scale', 0.667),
            noise_w_scale=CONFIG.get('tts_noise_w_scale', 0.8),
        )
        
        try:
            sentence_phonemes = self._piper_voice.phonemize(text)
            fixed = [[TURKISH_PHONEME_FIX.get(c, c) for c in phonemes] for phonemes in sentence_phonemes]
            
            chunks = []
            for phonemes in fixed:
                if not phonemes:
                    continue
                phoneme_ids = self._piper_voice.phonemes_to_ids(phonemes)
                audio_result = self._piper_voice.phoneme_ids_to_audio(
                    phoneme_ids, cfg, include_alignments=False
                )
                audio = audio_result[0] if isinstance(audio_result, tuple) else audio_result
                max_val = np.max(np.abs(audio))
                if max_val > 1e-8:
                    audio = audio / max_val
                audio = np.clip(audio, -1.0, 1.0).astype(np.float32)
                chunks.append(audio)
            
            audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
            sf.write(output_path, audio, 22050)
        except Exception as e:
            log.error(f"Piper TTS hatası: {e}")
            # Sessiz WAV üret (1 saniye)
            sf.write(output_path, np.zeros(22050, dtype=np.float32), 22050)
        
        return output_path
    
    @staticmethod
    def _clean_text(text: str) -> str:
        """Noktalama işaretlerini düzenle.
        
        MEMORY.md'deki ayarlar:
        - Nokta (.) → virgüle (,) çevrilir — kısa duraksama için
        - Diğer tüm işaretler (! ? ; : " ' vb.) temizlenir
        - Üç nokta (... → ... korunur, noktalar ayrı ayrı çevrilmez)
        """
        import re
        # 1. Üç noktayı koru (geçici yer tutucu)
        text = text.replace('...', '\x00ELLIPSIS\x00')
        
        # 2. Noktaları virgüle çevir (cümle sonu yerine kısa duraksama)
        text = text.replace('.', ',')
        
        # 3. Üç noktayı geri koy (korunmuş yer tutucudan)
        text = text.replace('\x00ELLIPSIS\x00', '...')
        
        # 4. Diğer tüm noktalama işaretlerini temizle, sadece virgül kalsın
        text = re.sub(r'[!?;:"\'()\[\]{}]', '', text)
        # Virgülden önceki boşlukları temizle
        text = re.sub(r'\s+,', ',', text)
        # Çoklu boşluğu tek boşluğa indir
        text = re.sub(r'\s+', ' ', text)
        # Baştaki/sondaki virgül ve boşlukları temizle
        text = text.strip().strip(',').strip()
        return text
    
    def _try_edge_tts(self, text: str, output_path: str) -> bool:
        """Edge-TTS dener. Başarılıysa True döner."""
        t0 = time.time()
        try:
            mp3_path = output_path + '.mp3'
            # Hız ayarı yok (+0%), sadece noktalama temizliği ile akıcılık
            clean_text = self._clean_text(text)
            
            # edge-tts asenkron, sync wrapper
            async def _do_tts():
                communicate = edge_tts.Communicate(clean_text, "tr-TR-AhmetNeural", rate="+0%")
                await communicate.save(mp3_path)
            
            asyncio.run(_do_tts())
            
            if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 100:
                raise RuntimeError("Edge-TTS boş dosya üretti")
            
            # MP3 → WAV 22050Hz mono (Piper formatına uyumlu)
            subprocess.run([
                'ffmpeg', '-y', '-i', mp3_path,
                '-ar', '22050', '-ac', '1', '-acodec', 'pcm_f32le',
                output_path
            ], capture_output=True, check=True, timeout=30)
            
            os.remove(mp3_path)
            log.info(f"   ☁️ Edge-TTS: {time.time()-t0:.2f}s | kaydedildi: {output_path}")
            return True
            
        except Exception as e:
            # Temizlik
            for f in [mp3_path, output_path]:
                try: os.remove(f)
                except: pass
            log.warning(f"   ⚠️ Edge-TTS başarısız: {str(e)[:80]}")
            return False
    
    def synthesize(self, text: str, output_path: str = None) -> str:
        """Metni sese çevirir.
        
        1. deneme: Edge-TTS (Microsoft cloud, doğal ses)
        2. deneme: Piper Fahrettin (GPU, offline yedek)
        
        Args:
            text: Sentezlenecek metin (Türkçe)
            output_path: Çıktı WAV yolu (None=otomatik)
            
        Returns:
            WAV dosyasının yolu
        """
        if output_path is None:
            os.makedirs(CONFIG['output_dir'], exist_ok=True)
            output_path = os.path.join(CONFIG['output_dir'], f"tts_{int(time.time())}.wav")
        
        # 1. Edge-TTS dene (birincil, doğal ses)
        if self._try_edge_tts(text, output_path):
            return output_path
        
        # 2. Fallback: Piper Fahrettin (GPU, offline)
        log.info("   ⬇️ Piper Fahrettin fallback...")
        return self._piper_synthesize(text, output_path)


# ─── Telegram Ses İndirici ───────────────────────────────────────────────────

class TelegramVoiceDownloader:
    """Telegram voice message'larını indirir ve WAV'a çevirir."""
    
    def _api_get(self, method: str, params: dict) -> dict:
        """Telegram Bot API'ye curl ile GET isteği yapar."""
        import urllib.parse
        query = urllib.parse.urlencode(params)
        url = f'{TELEGRAM_API}/{method}?{query}'
        try:
            result = subprocess.run(
                ['curl', '-s', url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception as e:
            log.error(f"API hatası ({method}): {e}")
        return {'ok': False}
    
    def _api_post_file(self, method: str, file_path: str, field_name: str = 'voice', **fields) -> dict:
        """Telegram Bot API'ye curl ile multipart POST yapar."""
        import urllib.parse
        cmd = ['curl', '-s']
        for key, val in fields.items():
            cmd += ['-F', f'{key}={val}']
        cmd += ['-F', f'{field_name}=@{file_path}']
        cmd += [f'{TELEGRAM_API}/{method}']
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception as e:
            log.error(f"API POST hatası ({method}): {e}")
        return {'ok': False}
    
    def get_file_path(self, file_id: str) -> Optional[str]:
        """file_id'den Telegram sunucusundaki dosya yolunu alır."""
        resp = self._api_get('getFile', {'file_id': file_id})
        if resp.get('ok'):
            return resp['result']['file_path']
        else:
            log.error(f"Telegram API hatası: {resp.get('description', 'bilinmiyor')}")
            return None
    
    def download(self, file_id: str, output_path: str = None) -> Optional[str]:
        """Voice message'i indirir ve WAV 16kHz mono'ya çevirir."""
        file_path = self.get_file_path(file_id)
        if not file_path:
            return None
        
        if output_path is None:
            os.makedirs(CONFIG['output_dir'], exist_ok=True)
            output_path = os.path.join(CONFIG['output_dir'], f"voice_{int(time.time())}.wav")
        
        # OGG/OPUS'u curl ile indir
        dl_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
        ogg_path = output_path + '.oga'
        
        t0 = time.time()
        try:
            subprocess.run(['curl', '-s', '-o', ogg_path, dl_url],
                          capture_output=True, check=True, timeout=30)
        except Exception as e:
            log.error(f"İndirme hatası: {e}")
            return None
        
        # Dosya boyutunu kontrol et
        if not os.path.exists(ogg_path) or os.path.getsize(ogg_path) < 10:
            log.error("İndirilen dosya çok küçük veya boş")
            return None
            
        log.info(f"   ⬇️ İndirme: {time.time()-t0:.2f}s ({os.path.getsize(ogg_path)} bytes)")
        
        # OGG → WAV 16kHz mono
        t0 = time.time()
        try:
            subprocess.run([
                'ffmpeg', '-y', '-i', ogg_path,
                '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
                output_path
            ], capture_output=True, check=True)
        except Exception as e:
            log.error(f"FFmpeg dönüşüm hatası: {e}")
            # Python ile dönüşüm dene
            try:
                import soundfile as sf
                data, sr = sf.read(ogg_path)
                if sr != 16000:
                    import torchaudio
                    import torch
                    resample = torchaudio.transforms.Resample(sr, 16000)
                    data = resample(torch.from_numpy(data).float().unsqueeze(0)).squeeze(0).numpy()
                sf.write(output_path, data, 16000)
            except Exception as e2:
                log.error(f"Python dönüşüm de başarısız: {e2}")
                return None
        
        # Temp OGG'yi temizle
        try: os.remove(ogg_path)
        except: pass
        
        log.info(f"   🔄 Dönüşüm: {time.time()-t0:.2f}s → {output_path}")
        return output_path


# ─── Pipeline ────────────────────────────────────────────────────────────────

class VoicePipeline:
    """STT + TTS + Telegram entegrasyonu."""
    
    def __init__(self):
        self.stt = STTEngine()
        self.tts = TTSHybridEngine()
        self.downloader = TelegramVoiceDownloader()
        os.makedirs(CONFIG['output_dir'], exist_ok=True)
    
    def download_and_stt(self, file_id: str) -> Optional[str]:
        """Telegram voice message'ini indir, WAV'a çevir, yazıya dök."""
        wav_path = self.downloader.download(file_id)
        if not wav_path:
            return None
        text = self.stt.transcribe(wav_path)
        return text
    
    def stt_from_file(self, wav_path: str) -> Optional[str]:
        """Doğrudan WAV dosyasından yazıya çevir."""
        if not os.path.exists(wav_path):
            log.error(f"Dosya bulunamadı: {wav_path}")
            return None
        return self.stt.transcribe(wav_path)
    
    def tts_to_file(self, text: str) -> str:
        """Metni sese çevirir ve WAV yolu döndürür."""
        return self.tts.synthesize(text)
    
    def resample_to_16k(self, input_wav: str, output_wav: str = None) -> str:
        """Herhangi bir WAV'ı 16kHz mono s16'e çevirir (STT için)."""
        if output_wav is None:
            os.makedirs(CONFIG['output_dir'], exist_ok=True)
            output_wav = os.path.join(CONFIG['output_dir'], f"resampled_{int(time.time())}.wav")
        
        try:
            subprocess.run([
                'ffmpeg', '-y', '-i', input_wav,
                '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
                output_wav
            ], capture_output=True, check=True)
        except Exception:
            # FFmpeg yoksa soundfile + torchaudio ile dene
            data, sr = sf.read(input_wav)
            if sr != 16000:
                import torch
                import torchaudio
                if data.ndim > 1:
                    data = data.mean(axis=1)
                resample = torchaudio.transforms.Resample(sr, 16000)
                data = resample(torch.from_numpy(data).float().unsqueeze(0)).squeeze(0).numpy()
            sf.write(output_wav, data, 16000)
        
        return output_wav
    
    def full_pipeline_file(self, input_wav: str, response_text: str = None) -> Tuple[str, str]:
        """Ses dosyası → STT → (opsiyonel cevap) → TTS → ses dosyası.
        
        Args:
            input_wav: Giriş WAV dosyası (16kHz önerilir)
            response_text: TTS ile seslendirilecek metin (None=STT çıktısı kullanılır)
            
        Returns:
            (transkripsiyon_metni, tts_wav_yolu)
        """
        text = self.stt.transcribe(input_wav)
        tts_text = response_text if response_text else text
        audio_path = self.tts.synthesize(tts_text)
        return text, audio_path
    
    def send_voice(self, chat_id: int, text: str, reply_to: int = None) -> bool:
        """Metni TTS ile seslendirip Telegram'a voice message olarak gönderir.
        
        Args:
            chat_id: Telegram chat ID
            text: Seslendirilecek metin
            reply_to: Yanıtlanacak mesaj ID'si (opsiyonel)
            
        Returns:
            Başarılı mı?
        """
        # TTS
        wav_path = self.tts.synthesize(text)
        
        # OGG'ye çevir (Telegram voice formatı)
        ogg_path = wav_path + '.ogg'
        try:
            subprocess.run([
                'ffmpeg', '-y', '-i', wav_path,
                '-c:a', 'libopus', '-b:a', '32k', '-ar', '24000',
                '-ac', '1',
                ogg_path
            ], capture_output=True, check=True)
        except Exception as e:
            log.error(f"OGG dönüşüm hatası: {e}")
            return False
        
        # curl ile Telegram'a yükle
        cmd = ['curl', '-s', '-F', f'chat_id={chat_id}']
        if reply_to:
            cmd += ['-F', f'reply_to_message_id={reply_to}']
        cmd += ['-F', f'voice=@{ogg_path}', f'{TELEGRAM_API}/sendVoice']
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                resp = json.loads(result.stdout)
                if resp.get('ok'):
                    log.info(f"   ✅ Ses gönderildi (chat: {chat_id})")
                    return True
                else:
                    log.error(f"Telegram API: {resp.get('description', '?')}")
            else:
                log.error(f"curl hatası: {result.stderr[:200]}")
        except Exception as e:
            log.error(f"Gönderme hatası: {e}")
        finally:
            # Temp dosyaları temizle
            try: os.remove(ogg_path)
            except: pass
            try: os.remove(wav_path)
            except: pass
        return False


# ─── Singleton ───────────────────────────────────────────────────────────────

voice_pipeline = VoicePipeline()


# ─── CLI Test ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Atılgan Ses Asistanı')
    parser.add_argument('action', choices=['stt', 'tts', 'pipeline', 'download'],
                        help='Yapılacak işlem')
    parser.add_argument('input', nargs='?', help='Dosya yolu, file_id veya metin')
    parser.add_argument('--output', '-o', help='Çıktı dosya yolu')
    
    args = parser.parse_args()
    
    if args.action == 'stt':
        if not args.input:
            print("Hata: Ses dosyası yolu gerekli")
            sys.exit(1)
        text = voice_pipeline.stt_from_file(args.input)
        print(f"\n📝 Transkripsiyon:\n{text}")
    
    elif args.action == 'tts':
        if not args.input:
            print("Hata: Metin gerekli")
            sys.exit(1)
        path = voice_pipeline.tts_to_file(args.input)
        print(f"\n🔊 Ses kaydedildi: {path}")
    
    elif args.action == 'download':
        if not args.input:
            print("Hata: Telegram file_id gerekli")
            sys.exit(1)
        wav = voice_pipeline.downloader.download(args.input)
        text = None
        try:
            if wav:
                print(f"\n✅ İndirildi: {wav}")
                text = voice_pipeline.stt.transcribe(wav)
                print(f"📝 Transkripsiyon: {text}")
        finally:
            # Her durumda WAV'ı temizle (Talimat #8)
            if wav and os.path.exists(wav):
                try:
                    os.remove(wav)
                    log.info(f"   🧹 WAV temizlendi: {os.path.basename(wav)}")
                except Exception as e:
                    log.warning(f"   ⚠️ WAV temizlik hatası: {e}")
    
    elif args.action == 'pipeline':
        if not args.input:
            print("Hata: Ses dosyası yolu gerekli")
            sys.exit(1)
        text, audio = voice_pipeline.full_pipeline_file(args.input)
        print(f"\n📝 STT: {text}")
        print(f"🔊 TTS: {audio}")
