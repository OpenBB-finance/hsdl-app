FROM python:3.12-slim

RUN groupadd -r hsdl && useradd -r -g hsdl -m hsdl

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
