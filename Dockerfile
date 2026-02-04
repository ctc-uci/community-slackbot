# ---------------------------
# Dockerfile for Slack Study Bot
# ---------------------------

# Use official Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy your requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy all app files
COPY . .

# Set environment variables (optional defaults)
ENV TZ=America/Los_Angeles
ENV TEST_CHANNEL_ID="C0ACQP6P3T2"

# Expose port if needed (for Bolt Socket Mode this isn't strictly necessary)
EXPOSE 3000

# Command to run your bot
# Adjust if your entrypoint file is named differently
CMD ["python", "app.py"]
