FROM hub-dev.hexin.cn/business-baseimage-python/python:v3.11.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY readiness.sh /opt/readiness.sh
RUN chmod +x /opt/readiness.sh

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
