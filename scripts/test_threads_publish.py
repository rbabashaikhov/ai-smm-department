from dotenv import load_dotenv

load_dotenv()

from pprint import pprint

from ai_smm.publishing.threads import ThreadsPublisher


publisher = ThreadsPublisher()

result = publisher.publish_text(
    "Тест публикации из AI SMM Department. "
    "Проверяю прямую интеграцию LangGraph-проекта с Threads API."
)

print("\n=== THREADS RESPONSE ===")
pprint(result)