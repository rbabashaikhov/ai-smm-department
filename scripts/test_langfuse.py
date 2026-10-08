from dotenv import load_dotenv

load_dotenv()

from langfuse import get_client


langfuse = get_client()

print("Langfuse client created:", type(langfuse).__name__)