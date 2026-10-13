# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required by Playwright
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY requirements.txt ./

# Install Python dependencies using pip
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers with system dependencies
# Use PLAYWRIGHT_BROWSERS_PATH to install to a specific location
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium

# Copy only runtime inputs. .dockerignore additionally prevents local secrets,
# evidence, archives, logs, and generated data from entering the build context.
COPY main.py main_api.py start_api.py __init__.py ./
COPY src ./src
COPY config/jobsearch_config.yaml config/file_config.yaml ./config/
COPY config/job_app_agent_config.yaml config/cv_app_agent_config.yaml ./config/
COPY config/litellm_config.yaml config/candidate_profile.yaml ./config/
COPY config/freshness.yaml config/company_industries.yaml ./config/
COPY config/target_companies.yaml ./config/
COPY config/company_careers_reliability_repairs_v1.yaml ./config/
COPY config/company_parent_source_mappings.yaml ./config/
COPY config/company_remaining_inventory_v2.csv ./config/
COPY config/discovery_only_source_classification.yaml ./config/
COPY data/*.example.txt ./data/

# Create necessary directories
RUN mkdir -p output jobs data

# Expose port (Cloud Run uses PORT environment variable)
ENV PORT=8080
EXPOSE 8080

# Run the application
CMD ["uvicorn", "main_api:app", "--host", "0.0.0.0", "--port", "8080"]
