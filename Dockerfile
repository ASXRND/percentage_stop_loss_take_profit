FROM python:3.12-slim

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    curl \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Установка и сборка TA-LIB
WORKDIR /tmp
RUN wget https://github.com/ta-lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz && \
    tar -xvzf ta-lib-0.6.4-src.tar.gz && \
    cd ta-lib-0.6.4 && \
    ./configure --prefix=/usr && \
    make -j$(nproc) && \
    make install
RUN pip3 install ta-lib    

# Установка переменных окружения для TA-LIB
ENV TA_INCLUDE_PATH=/usr/include
ENV TA_LIBRARY_PATH=/usr/lib

WORKDIR /app
COPY . .

# Очистка кэша, pycache, логов и временных файлов перед сборкой
RUN find . -type d -name '__pycache__' -exec rm -rf {} + && \
    find . -type f -name '*.pyc' -delete && \
    find . -type f -name '*.pyo' -delete && \
    rm -rf logs/* && \
    rm -rf /tmp/*

ENV LD_LIBRARY_PATH=/usr/local/lib:/usr/lib

# Установка Python-зависимостей
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Точка входа
CMD ["python", "bot.py"]