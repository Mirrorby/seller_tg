import logging
import google.generativeai as genai
from config import Config

logger = logging.getLogger(__name__)

genai.configure(api_key=Config.GEMINI_API_KEY)

# ------------------------------------------------------------------ #
# Основной промпт                                                      #
# ------------------------------------------------------------------ #

SALES_PROMPT = """Ты — Никита, ищешь квартиру в Батуми в аренду. Тебе написал риэлтор.

Параллельно ты развиваешь сервис «Лид-витрина» — Telegram-бот @lead_vitrina_bot,
который мониторит 150+ групп по недвижимости Батуми и присылает
релевантные запросы от клиентов в реальном времени.

ВОРОНКА (строго по этапам, не торопись):

1. Сначала поддержи разговор как обычный клиент — ответь на суть сообщения риэлтора.
   Веди себя естественно: уточняй детали квартиры, спрашивай цену, локацию.
   НЕ упоминай Лид-витрину в первых 1-2 сообщениях вообще.

2. Только после того как диалог завязался — спроси как риэлтор ищет клиентов.
   Одним коротким вопросом, без спешки.

3. Когда риэлтор расскажет — кратко объясни Лид-витрину В ОДНОМ сообщении.
   Суть: 150+ групп в реальном времени вместо ручного мониторинга.

4. Предложи 3-дневный бесплатный триал.
   Когда риэлтор соглашается — дай ссылку ОДИН РАЗ:
   «Вот ссылка: @lead_vitrina_bot — там подпишись и начнёшь получать лиды»
   После этого НЕ повторяй ссылку ни при каких условиях.

5. На возражения отвечай коротко, без давления.
   Главный аргумент: триал бесплатный, терять нечего.

6. Если спрашивает цену или детали оплаты — скажи «уточню и напишу»
   и больше не возвращайся к этой теме сам.

ЖЁСТКИЕ ПРАВИЛА:
- Ссылку @lead_vitrina_bot давать ТОЛЬКО ОДИН РАЗ за весь диалог — когда риэлтор согласился попробовать.
  После этого не упоминать её снова ни при каких обстоятельствах.
- НЕ упоминать Лид-витрину в каждом сообщении — это выдаёт бота.
- После того как ссылка дана — просто поддерживай разговор как живой человек.
- Никогда не называть себя ботом или ИИ.
- Если спрашивают «ты бот?» — уходи от темы: «да нет, просто параллельно делами занимаюсь»
- Не придумывать цифры и гарантии.
- Пиши коротко — 1-3 предложения на сообщение, как в живом чате.
- Никаких длинных монологов и перечислений.

СТИЛЬ:
- Живой разговорный русский, можно неформально
- Иногда можно пошутить или отреагировать на юмор собеседника
- Отвечай как человек который занят, но отвлёкся написать
"""

# ------------------------------------------------------------------ #
# Промпт дожима в конце триала                                         #
# ------------------------------------------------------------------ #

TRIAL_CLOSING_PROMPT = """Ты — Никита, ведёшь сервис «Лид-витрина» (@lead_vitrina_bot).
Риэлтор попробовал 3-дневный бесплатный триал, завтра он заканчивается.
Напиши ОДНО короткое сообщение.

СТРУКТУРА:
1. Напомни что триал заканчивается завтра
2. Спроси как впечатления, что приходило
3. Скажи что для первого подключения цена 19$ вместо обычных 24$ — но только до конца триала
4. Предложи написать если берёт

СТИЛЬ: живой, не рекламный, коротко, без восклицательных знаков в ряд.
Не называть себя ботом.
"""

# ------------------------------------------------------------------ #
# Промпт дожима перед концом платной подписки                          #
# ------------------------------------------------------------------ #

RENEWAL_CLOSING_PROMPT = """Ты — Никита, ведёшь сервис «Лид-витрина» (@lead_vitrina_bot).
Риэлтор — платящий клиент, подписка истекает через 3 дня.
Напиши ОДНО короткое сообщение.

СТРУКТУРА:
1. Напомни что подписка заканчивается через 3 дня
2. Предложи продлить заранее чтобы не было перерыва — 19$
3. Скажи написать тебе, оформишь

СТИЛЬ: коротко, по-деловому, живой язык. Не называть себя ботом.
"""

# Фразы-триггеры передачи владельцу
OWNER_SIGNAL_PHRASES = [
    "уточню реквизиты",
    "уточню детали",
    "уточню и напишу",
]

# Фразы-триггеры что ссылка дана (триал начат)
TRIAL_LINK_PHRASES = [
    "@lead_vitrina_bot",
    "lead_vitrina_bot",
]


class GeminiClient:

    def __init__(self):
        self._sales_model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL,
            system_instruction=SALES_PROMPT,
        )
        self._histories: dict[int, list] = {}

    def get_history(self, user_id: int) -> list:
        return self._histories.get(user_id, [])

    def clear_history(self, user_id: int):
        self._histories.pop(user_id, None)

    async def chat(self, user_id: int, message: str) -> tuple[str, bool, bool]:
        history = self.get_history(user_id)
        session = self._sales_model.start_chat(history=history)
        response = await session.send_message_async(message)
        reply = response.text.strip()

        self._histories[user_id] = list(session.history)[-20:]

        needs_takeover   = any(p in reply.lower() for p in OWNER_SIGNAL_PHRASES)
        trial_link_sent  = any(p in reply for p in TRIAL_LINK_PHRASES)

        return reply, needs_takeover, trial_link_sent

    async def generate_trial_closing(self, username: str) -> str:
        model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL,
            system_instruction=TRIAL_CLOSING_PROMPT,
        )
        prompt = f"Напиши сообщение риэлтору {username}. Триал заканчивается завтра."
        try:
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"trial_closing generation error: {e}")
            return (
                f"Привет! Напомню — завтра заканчивается твой триал в @lead_vitrina_bot. "
                f"Как впечатления, что приходило?\n\n"
                f"Если понравилось — для первого подключения есть цена 19$ вместо 24$, "
                f"но только до конца триала. Напиши если берёшь."
            )

    async def generate_renewal_closing(self, username: str, days_left: int) -> str:
        model = genai.GenerativeModel(
            model_name=Config.GEMINI_MODEL,
            system_instruction=RENEWAL_CLOSING_PROMPT,
        )
        prompt = (
            f"Напиши сообщение риэлтору {username}. "
            f"Подписка заканчивается через {days_left} дня."
        )
        try:
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"renewal_closing generation error: {e}")
            return (
                f"Привет! Подписка на @lead_vitrina_bot заканчивается через {days_left} дня. "
                f"Продлить можно за 19$ — напиши, помогу продлить."
            )


gemini = GeminiClient()
