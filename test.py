from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:20128/v1", api_key="placeholder")

response = client.chat.completions.create(model="kr/claude-sonnet-4.5", messages=[{"role": "user", "content": "Ответь ровно: PAI_PYTHON_OK"}])

print(response.choices[0].message.content)
