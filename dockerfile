FROM python:3.13.9-slim

# Create work directory
WORKDIR /app

# Copy requirements first
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose port (Railway will assign $PORT)
EXPOSE 5000

# Run Flask app directly
CMD ["python", "app.py"]
