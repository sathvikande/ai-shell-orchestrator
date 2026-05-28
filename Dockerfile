# Use the lightweight Python 3.11 image
FROM python:3.11-slim

# Install system dependencies and C++ compilers required by duckduckgo-search and cryptography
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first (for efficient Docker caching)
COPY requirements.txt .

# Upgrade pip and install the Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files into the container
COPY . .

# Set the default command to start the bot
CMD ["python", "vlsi_telegram_bot.py"]
