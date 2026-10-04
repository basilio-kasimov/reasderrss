FROM python:3.12-slim

WORKDIR /app

# Сначала ставим torch в CPU-версии: обычная тянет ~5 ГБ CUDA-библиотек,
# которые на сервере без GPU бесполезны
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]