FROM postgres:16-alpine
RUN apk add --no-cache python3 py3-cryptography py3-psycopg2
COPY backup/worker.py /opt/backup.py
ENTRYPOINT ["python3", "/opt/backup.py"]
CMD ["schedule"]
