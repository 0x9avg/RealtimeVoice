# RealtimeVoice — прототип на компьютере

Минимальный speech-to-speech прототип: браузер захватывает микрофон и текстовый ввод, FastAPI backend держит
одну сессию Gemini Live API (аудио-в/аудио-из, с tool calling) и стримит аудио-ответ обратно в браузер.

Зачем именно так и что из этого переиспользовано — см. `../CLAUDE.md`. Протокол общения с Gemini Live (WS-URL,
формат `setup`/`realtimeInput`/`clientContent`/`toolCall`/`toolResponse`) переиспользован из уже проверенной в
бою реализации в `../../humanoid-robot/packages/humanoid-robot-adapters-live-gemini/`, а не написан с нуля.

## Архитектура

```
Browser (index.html + app.js)              FastAPI backend (app/)              Gemini Live API
mic (getUserMedia, AudioWorklet, 16kHz) ─▶  /ws/session  ──send_audio──▶  GeminiLiveAdapter ──▶ BidiGenerateContent
speaker (Web Audio, 24kHz)             ◀─   (forwards AudioChunk)      ◀──audio/toolCall/turn──
текстовый ввод                          ─▶   (forwards send_text)      ──clientContent──▶
статус/транскрипт/tool-события         ◀─    (forwards adapter events)
                                              ToolRegistry.dispatch() ──▶ get_current_time
```

Ресемплинг делает сам браузер: `AudioContext` для захвата создаётся с `sampleRate: 16000`, для воспроизведения —
с `sampleRate: 24000`, поэтому на backend нет никакой обработки аудио, только пересылка байтов.

Непрерывный разговор без push-to-talk: пока микрофон включён, аудио стримится постоянно, конец хода определяет
серверный VAD самой Gemini Live. Barge-in: событие `interrupted` от Gemini backend пересылает в браузер как
`{"type":"interrupted"}`, а тот сразу останавливает и очищает очередь воспроизведения.

## Запуск

1. Скопируйте `.env.example` в `.env` и впишите свой `GEMINI_API_KEY` (ключ Google AI Studio с доступом к
   Gemini Live API).
2. Установите зависимости и запустите сервер:

   ```bash
   cd RealtimeVoice/prototype
   uv sync
   uv run uvicorn app.main:app --reload
   ```

3. Откройте `http://localhost:8000` в Chrome или Edge (нужны `getUserMedia` и `AudioWorklet`).

## Известные ограничения / что проверить в первую очередь (Phase 0 спайк)

- Модель по умолчанию — `models/gemini-3.1-flash-live-preview`. Это значение подтверждённо работало с ключом
  заказчика в `../../humanoid-robot` на 13.08.2026, но список доступных Live-моделей завязан на конкретный
  API-ключ/аккаунт. Если подключение сразу падает с ошибкой — задайте `GEMINI_LIVE_MODEL` в `.env` на модель,
  реально доступную вашему ключу.
- Рассчитано на запуск в браузере, а не на прямой доступ к аудио-устройствам с backend: в WSL2 (среда, где это
  писалось) backend не имеет прямого доступа к аудио Windows-хоста, поэтому весь звук идёт через браузер, а не
  через `sounddevice`/ALSA на сервере.
- Нет JSONL event log / p50/p95-метрик задержки, нет полноценного Tool Broker с idempotency — это осознанно
  отложено до момента, когда сам голосовой контур подтверждён рабочим (см. `../CLAUDE.md`).
- Тестов нет: корректность здесь по большей части проверяется только вживую против реального Gemini Live API.

## Ручной чек-лист

1. Нажать **Start mic**, разрешить доступ к микрофону, сказать что-нибудь — статус должен перейти
   `listening → speaking`, должен идти звук ответа, в транскрипте — реплики.
2. Ввести вопрос текстом и нажать **Send** — должен прийти голосовой ответ без использования микрофона.
3. Спросить "который час?" / "what time is it?" — в логе tool calls должен появиться вызов `get_current_time`,
   а озвученный ответ — содержать корректное текущее время.
4. Заговорить во время проигрывания ответа (barge-in) — воспроизведение должно оборваться быстро, а не
   доиграть до конца.
5. Нажать **Замолчи** во время ответа — воспроизведение должно остановиться мгновенно.
