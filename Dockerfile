# Build stage / Runtime stage
FROM python:3.12-slim-bookworm

# System dependencies and Playwright browser requirements
RUN apt-get update --fix-missing && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (only chromium is needed for ui_tester.py)
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy the rest of the application
COPY . .

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV MAIL_AUTO_CONFIG_DIR=/app/data
ENV PORT=5000

# Create data directories
RUN mkdir -p /app/data /app/reports /app/logs /app/attachments

# Expose the port
EXPOSE 5000

# Command to run the application
# We use 'app.py' which starts the Flask server
CMD ["python", "app.py"]
