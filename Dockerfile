# Dockerfile for Django QR Project on Vercel
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect static files
RUN python manage.py collectstatic --noinput

# Expose port 8000
EXPOSE 8000

CMD ["gunicorn", "qr_project.wsgi:application", "--bind", "0.0.0.0:8000"]
