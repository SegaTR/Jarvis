import json
import vosk
import pyaudio
import requests
import threading
import queue
import time
import subprocess
import os
import asyncio
import tempfile
import importlib
from datetime import datetime
from ollama import chat
from ollama import ChatResponse

edge_tts = None
playsound = None

edge_tts_spec = importlib.util.find_spec("edge_tts")
if edge_tts_spec:
    edge_tts = importlib.import_module("edge_tts")

playsound_spec = importlib.util.find_spec("playsound")
if playsound_spec:
    playsound_module = importlib.import_module("playsound")
    playsound = getattr(playsound_module, "playsound", None)


class JarvisVoiceEngine:
    """Генерация голоса в стиле Джарвиса через Microsoft Edge TTS."""

    def __init__(self, voice_name=None, rate=None, volume=None):
        if not self.is_available():
            raise RuntimeError("Jarvis voice engine недоступен (edge-tts или playsound не установлены)")
        self.voice_name = voice_name or os.getenv("JARVIS_VOICE_NAME", "en-GB-RyanNeural")
        self.rate = rate or os.getenv("JARVIS_VOICE_RATE", "-10%")
        self.volume = volume or os.getenv("JARVIS_VOICE_VOLUME", "+0%")

    @staticmethod
    def is_available():
        return edge_tts is not None and playsound is not None

    async def _synthesize_to_file(self, text, file_path):
        communicator = edge_tts.Communicate(text, voice=self.voice_name, rate=self.rate, volume=self.volume)
        with open(file_path, "wb") as audio_file:
            async for chunk in communicator.stream():
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])

    def speak(self, text):
        tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp_path = tmp_file.name
        tmp_file.close()

        try:
            asyncio.run(self._synthesize_to_file(text, tmp_path))
            playsound(tmp_path, block=True)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

class StableTTSJarvis:
    def __init__(self, model_path="vosk-model-small-ru-0.22"):
        # Инициализация Vosk
        self.model = vosk.Model(model_path)
        self.recognizer = vosk.KaldiRecognizer(self.model, 16000)
        
        # Аудиопоток для микрофона
        self.audio = pyaudio.PyAudio()
        self.stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=4000
        )
        
        # Очередь для обработки команд и TTS
        self.command_queue = queue.Queue()
        self.tts_queue = queue.Queue()
        self.is_listening = True
        
        # История разговора
        self.conversation_history = []
        
        # Голосовая активация
        self.activation_phrase = "джарвис"
        self.is_activated = False
        
        # Состояние ожидания подтверждения для LLM
        self.awaiting_llm_confirmation = False
        self.pending_command = ""
        
        # Флаг для TTS потока
        self.tts_running = True
        
        print("🔊 Джарвис инициализирован. Скажите 'Джарвис' для активации...")

    def speak(self, text):
        """Добавляет текст в очередь TTS"""
        self.tts_queue.put(text)

    def tts_worker(self):
        """Рабочий поток для TTS - использует системный TTS"""
        while self.tts_running or not self.tts_queue.empty():
            try:
                text = self.tts_queue.get(timeout=1)
                if text:
                    print(f"🤖 Джарвис: {text}")
                    
                    # Способ 1: Используем subprocess с разными TTS движками
                    try:
                        # Для Windows (раскомментируйте нужный)
                        # subprocess.run(['powershell', '-Command', f'Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak("{text}")'], 
                        #               capture_output=True, timeout=10)
                        
                        # Для Linux (eSpeak)
                        subprocess.run(['espeak', '-v', 'ru', '-s', '150', text], 
                                      capture_output=True, timeout=10)
                        
                        # Для Linux (RHVoice)
                        # subprocess.run(['echo', text, '|', 'rhvoice-client'], 
                        #               shell=True, capture_output=True, timeout=10)
                        
                        # Для macOS
                        # subprocess.run(['say', '-v', 'Milena', text], 
                        #               capture_output=True, timeout=10)
                        
                    except subprocess.TimeoutExpired:
                        print("TTS timeout")
                    except Exception as e:
                        print(f"TTS error: {e}")
                        
                    self.tts_queue.task_done()
                    
            except queue.Empty:
                continue
            except Exception as e:
                print(f"TTS worker error: {e}")

    def think_with_llm(self, user_input):
        """Запрос к локальной LLM через Ollama"""
        try:
            context = "\n".join([f"User: {msg['user']}\nAssistant: {msg['assistant']}" 
                               for msg in self.conversation_history[-2:]])
            
            system_prompt = f"""Ты Джарвис - интеллектуальный голосовой помощник. 
Текущее время: {datetime.now().strftime('%H:%M %d.%m.%Y')}

Контекст разговора:
{context}

Пользователь: {user_input}
Твой ответ должен быть кратким (1-2 предложения), полезным и естественным для голосового воспроизведения.
Ответ:"""

            response: ChatResponse = chat(model='gemma3:1b', messages=[
  {
    'role': 'user',
    'content': user_input,
  },
])
            
            if response.status_code == 200:
                result = response.json()['response'].strip()
                
                self.conversation_history.append({
                    'user': user_input,
                    'assistant': result,
                    'timestamp': datetime.now().isoformat()
                })
                
                return result
            else:
                return "Извините, возникла проблема с обработкой запроса."
                
        except Exception as e:
            print(f"Ошибка LLM: {e}")
            return "Произошла ошибка при обработке вашего запроса."

    def process_local_command(self, text):
        """Обработка локальных команд без LLM"""
        text_lower = text.lower()
        
        # Базовые команды
        if any(word in text_lower for word in ['стоп', 'выход', 'закройся']):
            return "exit", "Завершаю работу. До свидания!"
            
        elif any(word in text_lower for word in ['время', 'который час']):
            current_time = datetime.now().strftime('%H:%M')
            return "local", f"Сейчас {current_time}"
            
        elif any(word in text_lower for word in ['дата', 'число', 'какое число']):
            current_date = datetime.now().strftime('%d %B %Y')
            return "local", f"Сегодня {current_date}"
            
        elif any(word in text_lower for word in ['спасибо', 'благодарю']):
            return "local", "Всегда рад помочь!"
            
        elif any(word in text_lower for word in ['очисти историю', 'забудь всё']):
            self.conversation_history.clear()
            return "local", "История разговора очищена."
            
        elif any(word in text_lower for word in ['да', 'конечно', 'ага', 'угу', 'согласен']):
            if self.awaiting_llm_confirmation:
                self.awaiting_llm_confirmation = False
                return "llm_confirm", self.pending_command
            return "local", "Хорошо"
            
        elif any(word in text_lower for word in ['нет', 'не надо', 'отмена', 'отменить']):
            if self.awaiting_llm_confirmation:
                self.awaiting_llm_confirmation = False
                return "local", "Понимаю, отменяю запрос к нейросети."
            return "local", "Хорошо"
        
        # Если это не локальная команда, спросим про LLM
        return "ask_llm", text

    def listen_continuous(self):
        """Непрерывное прослушивание с активацией по ключевой фразе"""
        print("🎤 Слушаю...")
        
        while self.is_listening:
            try:
                data = self.stream.read(2000, exception_on_overflow=False)
                
                if self.recognizer.AcceptWaveform(data):
                    result = json.loads(self.recognizer.Result())
                    text = result.get("text", "").strip().lower()
                    
                    if text:
                        print(f"🎤 Распознано: {text}")
                        
                        # Проверка фразы активации
                        if self.activation_phrase in text:
                            if not self.is_activated:
                                self.is_activated = True
                                self.speak("Слушаю вас")
                            continue
                        
                        # Если активирован, обрабатываем команду
                        if self.is_activated:
                            # Убираем фразу активации из текста
                            clean_text = text.replace(self.activation_phrase, "").strip()
                            if clean_text:
                                self.command_queue.put(clean_text)
                                self.is_activated = False  # Сбрасываем активацию после команды
                            
            except Exception as e:
                print(f"Ошибка при прослушивании: {e}")

    def process_commands(self):
        """Обработка команд из очереди"""
        while self.is_listening:
            try:
                command = self.command_queue.get(timeout=1)
                
                if command:
                    print(f"🔧 Обрабатываю команду: {command}")
                    
                    # Обрабатываем команду
                    command_type, response = self.process_local_command(command)
                    
                    if command_type == "exit":
                        self.speak(response)
                        self.is_listening = False
                        break
                        
                    elif command_type == "local":
                        self.speak(response)
                        
                    elif command_type == "ask_llm":
                        # Спрашиваем подтверждение для LLM
                        self.awaiting_llm_confirmation = True
                        self.pending_command = command
                        self.speak(f"Это сложный запрос: '{command}'. Обратиться к нейросети для ответа? Скажите 'да' или 'нет'.")
                        
                    elif command_type == "llm_confirm":
                        # Пользователь подтвердил использование LLM
                        self.speak("Обрабатываю ваш запрос...")
                        llm_response = self.think_with_llm(response)
                        self.speak(llm_response)
                        
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Ошибка обработки команды: {e}")
                self.speak("Произошла ошибка при обработке команды")

    def run(self):
        """Запуск Джарвиса"""
        # Запускаем поток для TTS
        tts_thread = threading.Thread(target=self.tts_worker, daemon=True)
        tts_thread.start()
        
        # Запускаем потоки для прослушивания и обработки
        listen_thread = threading.Thread(target=self.listen_continuous, daemon=True)
        process_thread = threading.Thread(target=self.process_commands, daemon=True)
        
        listen_thread.start()
        process_thread.start()
        
        try:
            # Главный цикл
            while self.is_listening:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nЗавершение работы по запросу пользователя...")
        finally:
            self.cleanup()

    def cleanup(self):
        """Очистка ресурсов"""
        self.is_listening = False
        self.tts_running = False
        time.sleep(1)
        
        # Очищаем очереди
        while not self.tts_queue.empty():
            try:
                self.tts_queue.get_nowait()
                self.tts_queue.task_done()
            except:
                pass
                
        self.stream.stop_stream()
        self.stream.close()
        self.audio.terminate()
        print("✅ Ресурсы освобождены")

# Версия с проверкой доступности TTS движков
class CompatibleJarvis(StableTTSJarvis):
    def __init__(self, model_path="vosk-model-small-ru-0.22"):
        super().__init__(model_path)
        self.tts_engine = self.detect_tts_engine()
        self.jarvis_voice = JarvisVoiceEngine() if self.tts_engine == 'jarvis' else None
        print(f"🔊 Используется TTS движок: {self.tts_engine}")

    def detect_tts_engine(self):
        """Определяет доступный TTS движок"""
        jarvis_disabled = os.getenv("DISABLE_JARVIS_VOICE", "").lower() in ("1", "true", "yes")
        if not jarvis_disabled and JarvisVoiceEngine.is_available():
            return 'jarvis'

        try:
            # Проверяем eSpeak (Linux)
            result = subprocess.run(['which', 'espeak'], capture_output=True)
            if result.returncode == 0:
                return 'espeak'
        except:
            pass
            
        try:
            # Проверяем say (macOS)
            result = subprocess.run(['which', 'say'], capture_output=True)
            if result.returncode == 0:
                return 'say'
        except:
            pass
            
        try:
            # Проверяем PowerShell (Windows)
            result = subprocess.run(['powershell', '-Command', 'echo test'], capture_output=True)
            if result.returncode == 0:
                return 'powershell'
        except:
            pass
            
        return 'none'

    def tts_worker(self):
        """Рабочий поток для TTS с поддержкой разных движков"""
        while self.tts_running or not self.tts_queue.empty():
            try:
                text = self.tts_queue.get(timeout=1)
                if text:
                    print(f"🤖 Джарвис: {text}")
                    
                    try:
                        if self.tts_engine == 'jarvis' and self.jarvis_voice:
                            self.jarvis_voice.speak(text)
                        elif self.tts_engine == 'espeak':
                            subprocess.run(['espeak', '-v', 'ru', '-s', '150', text], 
                                          capture_output=True, timeout=10)
                        elif self.tts_engine == 'say':
                            subprocess.run(['say', '-v', 'Milena', text],
                                          capture_output=True, timeout=10)
                        elif self.tts_engine == 'powershell':
                            # Экранируем кавычки для PowerShell
                            escaped_text = text.replace('"', '`"')
                            subprocess.run([
                                'powershell', '-Command', 
                                f'Add-Type -AssemblyName System.Speech; $speak = New-Object System.Speech.Synthesis.SpeechSynthesizer; $speak.Speak("{escaped_text}")'
                            ], capture_output=True, timeout=10)
                        else:
                            print(f"TTS не доступен. Текст: {text}")
                            
                    except subprocess.TimeoutExpired:
                        print("TTS timeout")
                    except Exception as e:
                        print(f"TTS error: {e}")
                        
                    self.tts_queue.task_done()
                    
            except queue.Empty:
                continue
            except Exception as e:
                print(f"TTS worker error: {e}")

# Простая версия без TTS (только текст)
class TextOnlyJarvis(StableTTSJarvis):
    def speak(self, text):
        """Просто выводит текст в консоль"""
        print(f"🤖 Джарвис: {text}")

# Запуск
if __name__ == "__main__":
    try:
        print("🚀 Запуск Джарвиса...")
        
        # Проверяем доступность Ollama
        try:
            response = requests.get('http://localhost:11434/api/tags', timeout=5)
            if response.status_code == 200:
                print("✅ Ollama доступен")
            else:
                print("❌ Ollama не отвечает")
        except:
            print("❌ Ollama не запущен. Запустите: ollama run mistral")
        
        # Выберите версию:
        jarvis = CompatibleJarvis(model_path="vosk-model-small-ru-0.22")
        # jarvis = TextOnlyJarvis(model_path="vosk-model-ru-0.42")  # Только текст
        
        jarvis.run()
        
    except Exception as e:
        print(f"❌ Ошибка запуска: {e}")
        print("\nПроверьте:")
        print("1. Скачана ли модель Vosk для русского языка")
        print("2. Установлен ли один из TTS движков (espeak, say, или Windows TTS)")
        print("3. Запущен ли Ollama: ollama run mistral")
        print("4. Работает ли микрофон")