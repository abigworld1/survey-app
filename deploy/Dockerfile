# パイプライン実行用の軽量イメージ。vLLM と同じ compose ネットワークに載せる。
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 既定では vLLM をネットワーク内部名で叩く（compose の service 名に合わせて上書き可）
ENV LLM_BASE_URL=http://vllm:8000/v1
ENV LLM_API_KEY=dummy

CMD ["python", "-m", "pipeline.run"]
