release: python manage.py migrate && python manage.py createcachetable
web: python manage.py createcachetable; gunicorn project.wsgi --workers 2 --threads 2 --worker-class gthread --timeout 60 --max-requests 500 --max-requests-jitter 50
worker: celery -A project worker --loglevel=info --concurrency=2
beat: celery -A project beat --loglevel=info