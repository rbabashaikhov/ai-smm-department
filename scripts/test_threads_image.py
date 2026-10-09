from dotenv import load_dotenv


load_dotenv()

from pprint import pprint

from ai_smm.publishing.threads import ThreadsPublisher


publisher = ThreadsPublisher()

image_url = (
    "https://files.apps.leadmeter.ru/"
    "ai-smm/ai-catalog-consultant/"
    "telegram-01-recommendation-baseline.jpg"
)

result = publisher.create_image_post(
    image_url=image_url,
    text="Тест публикации изображения через Threads API.",
    alt_text="AI Catalog Consultant — тестовое изображение",
)

print("\n=== IMAGE RESULT ===")
pprint(result)