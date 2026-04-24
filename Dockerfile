FROM python:3.12-slim

RUN groupadd -g 32767 hsdl && useradd -u 32767 -g 32767 -m hsdl

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir .

COPY widgets.json apps.json entrypoint.sh ./

RUN chmod +x entrypoint.sh && \
    mkdir -p data && chown -R hsdl:hsdl /app

USER hsdl

EXPOSE 7780

ENTRYPOINT ["./entrypoint.sh"]
CMD ["serve"]
