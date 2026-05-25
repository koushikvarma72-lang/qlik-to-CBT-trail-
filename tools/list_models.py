import os
import requests
from dotenv import load_dotenv

load_dotenv()

key = os.environ.get('OPENROUTER_API_KEY')
url = "https://openrouter.ai/api/v1/models"

headers = {
    "Authorization": f"Bearer {key}"
}

try:
    response = requests.get(url, headers=headers)
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        models = response.json().get('data', [])
        print(f"Found {len(models)} models.")
        # Print first 10
        for m in models[:20]:
            print(f"- {m['id']}")
    else:
        print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")
