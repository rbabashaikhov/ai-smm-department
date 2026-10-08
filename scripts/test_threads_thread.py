from dotenv import load_dotenv

load_dotenv()

from pprint import pprint

from ai_smm.publishing.threads import ThreadsPublisher


publisher = ThreadsPublisher()

items = [
    {
        "order": 1,
        "text": (
            "Тест треда из AI SMM Department. "
            "Сообщение 1 из 3."
        ),
    },
    {
        "order": 2,
        "text": (
            "Сообщение 2 из 3. "
            "Проверяю автоматическую публикацию reply."
        ),
    },
    {
        "order": 3,
        "text": (
            "Сообщение 3 из 3. "
            "Цепочка собрана через Threads API."
        ),
    },
]

result = publisher.publish_thread(items)

print("\n=== THREAD RESULT ===")
pprint(result)