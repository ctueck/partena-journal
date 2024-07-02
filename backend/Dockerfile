FROM python:3-alpine

ADD requirements.txt /app/

WORKDIR /app

RUN apk update && apk add --no-cache build-base

RUN pip install --upgrade pip
RUN apk update && apk add --no-cache poppler-dev
RUN pip install -r requirements.txt

ADD . /app

CMD python3 -m gunicorn -b :8000 'partena:app'

