"""
cold_gemini.py — Gemini-логика для ХОЛОДНЫХ контактов.

В отличие от gemini_client.py (где риэлтор пишет первым и Никита отвечает),
здесь Никита САМ инициирует разговор с незнакомым человеком.
После ответа холодного контакта используется та же воронка
(SALES_PROMPT), что и для тёплых — поэтому импортируем её.
"""

import logging
import google.generativeai as genai
from config import Config
from gemini_client import SALES_PROMPT, OWNER_SIGNAL_PHRASES, TRIAL_LINK_PHRASES

logger = logging.getLogger(__name__)

genai.configure(api_key=Config.GEMINI_API_KEY)


# ------------------------------------------------------------------ #
# Промпт для генерации ПЕРВОГО (холодного) сообщения
# ------------------------------------------------------------------ #
COLD_OPENER_PROMPT = """Ты — Никита, риэлтор/инвестор в Батуми. Ты пишешь ПЕРВЫМ
человеку, который тоже работает в недвижимости Батуми (агент/риэлтор),
но вы раньше не общались.

Параллельно ты развиваешь сервис «Лид-витрина» — Telegram-бот @lead_vitrina_bot,
который мониторит 150+ групп по недвижимости Батуми и присылает релевантные
запросы от клиентов в реальном времени. Но в первом сообщении НЕ продавай —
твоя цель просто завязать диалог, как коллега коллеге.

ЗАДАЧА: написать ОДНО короткое сообщение для первого касания.

ВАРИАНТЫ ПОВОДОВ (выбери один, без шаблонности):
- Как бы невзначай спросить, занимается ли человек сейчас арендой/продажей
  в Батуми (типа "видел тебя в чатах по недвижимости, тоже здесь варюсь")
- Спросить мнение/совет по рынку (типа "как сейчас спрос на долгосрочную
  аренду в Батуми, не в курсе?")
- Упомянуть что часто видишь объявления в профильных группах и решил
  написать напрямую

СТИЛЬ:
- 1-2 коротких предложения, разговорный русский
- НЕ здороваться формально ("Добрый день!") — пиши как знакомому/коллеге
- НЕ упоминать Лид-витрину, бота, триалы — это будет позже, если диалог пойдёт
- НЕ представляться полным именем в духе "Меня зовут Никита, я..."
  — можно представиться вскользь, естественно, или не представляться вообще
- Никаких эмодзи в начале/конце через тире, без рекламных интонаций
- Не задавай больше одного вопроса

Если есть имя или username получателя — можешь обратиться по имени,
но без подхалимажа ("Привет, Анна!" — нормально, "Анна, добрый день,
рад знакомству!" — нет).

Верни ТОЛЬКО текст сообщения, без кавычек и пояснений.
"""


class ColdGeminiClient:
    def __init__(self):
        self._opener_model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL,
            system_instruction=COLD_OPENER_PROMPT,
        )
        # Тот же sales-промпт что у тёплых — после ответа холодный
        # ведётся по той же воронке
        self._sales_model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL,
            system_instruction=SALES_PROMPT,
        )
        self._histories: dict[int, list] = {}

    def get_history(self, user_id: int) -> list:
        return self._histories.get(user_id, [])

    def clear_history(self, user_id: int):
        self._histories.pop(user_id, None)

    async def generate_cold_opener(self, username: str, name: str) -> str:
        """Сгенерировать первое сообщение для холодного контакта."""
        who = name or (username if username else "")
        prompt = (
            f"Напиши первое сообщение для {who}." if who
            else "Напиши первое сообщение для незнакомого риэлтора в Батуми."
        )

        try:
            response = await self._opener_model.generate_content_async(prompt)
            text = response.text.strip()
            # Иногда модель оборачивает в кавычки — снимаем
            text = text.strip('"').strip("«»").strip()
            return text
        except Exception as e:
            logger.error(f"cold_opener generation error: {e}")
            return "Слышь, видел тебя в чатах по недвижимости в Батуми — сам тоже здесь варюсь, как сейчас вообще со спросом, не в курсе?"

    async def chat(self, user_id: int, message: str) -> tuple[str, bool, bool]:
        """
        Продолжение диалога после того как холодный контакт ответил.
        Использует ту же воронку (SALES_PROMPT) что и для тёплых клиентов.

        Возвращает: (reply, needs_owner_takeover, trial_link_sent)
        """
        history = self.get_history(user_id)
        session = self._sales_model.start_chat(history=history)
        response = await session.send_message_async(message)
        reply = response.text.strip()

        self._histories[user_id] = list(session.history)

        needs_takeover = any(p in reply.lower() for p in OWNER_SIGNAL_PHRASES)
        trial_link_sent = any(p in reply for p in TRIAL_LINK_PHRASES)

        return reply, needs_takeover, trial_link_sent


cold_gemini = ColdGeminiClient()
