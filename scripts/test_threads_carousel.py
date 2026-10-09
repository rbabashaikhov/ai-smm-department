from dotenv import load_dotenv


load_dotenv()

from pprint import pprint

from ai_smm.publishing.threads import ThreadsPublisher


publisher = ThreadsPublisher()

images = [
    {
        "url": (
            "https://files.apps.leadmeter.ru/"
            "ai-smm/ai-catalog-consultant/"
            "telegram-01-recommendation-baseline.jpg"
        ),
        "alt_text": (
            "AI Catalog Consultant — первая рекомендация "
            "по запросу пользователя"
        ),
    },
    {
        "url": (
            "https://files.apps.leadmeter.ru/"
            "ai-smm/ai-catalog-consultant/"
            "telegram-02-budget.jpg"
        ),
        "alt_text": (
            "AI Catalog Consultant — уточнение вариантов "
            "по бюджету"
        ),
    },
    {
        "url": (
            "https://files.apps.leadmeter.ru/"
            "ai-smm/ai-catalog-consultant/"
            "telegram-03-comparison.jpg"
        ),
        "alt_text": (
            "AI Catalog Consultant — сравнение "
            "выбранных моделей"
        ),
    },
    {
        "url": (
            "https://files.apps.leadmeter.ru/"
            "ai-smm/ai-catalog-consultant/"
            "telegram-04-tradeoff.jpg"
        ),
        "alt_text": (
            "AI Catalog Consultant — объяснение компромисса "
            "при выборе более дешёвого варианта"
        ),
    },
]

result = publisher.publish_carousel(
    text=(
        "AI Catalog Consultant в действии: "
        "от первого запроса до сравнения и выбора компромисса."
    ),
    images=images,
)

print("\n=== CAROUSEL RESULT ===")
pprint(result)