# RoleRadar Testing

## Overview

RoleRadar uses a single, comprehensive test file instead of multiple scattered test files. This approach provides better maintainability and complete coverage of all functionality.

## Running Tests

### Quick Test
```bash
python test_comprehensive.py
```

This will run all available tests and show a comprehensive summary.

## Test Coverage

The comprehensive test suite covers:

### 1. Database Operations
- ✅ Database initialization and connection
- ✅ Job insertion and retrieval  
- ✅ Duplicate detection
- ✅ Search functionality
- ✅ Statistics generation

### 2. Job Migration
- ✅ JSON file discovery
- ✅ Data migration from JSON to database
- ✅ Handling different data formats

### 3. Job Search Pipeline
- ✅ Unified pipeline initialization (sync/async modes)
- ✅ Pipeline class functionality verification
- ✅ LinkedIn scraper integration
- ✅ Page calculation based on max_jobs
- ✅ Convenience function testing (`run_job_search` and `run_job_search_async`)
- ✅ Database integration and deduplication
- ✅ Export functionality testing

### 4. Job Parser Agent
- ✅ Parser agent creation
- ✅ Agent functionality verification

### 5. API Endpoints (Optional)
- 🔗 REST API endpoint testing
- 🔗 Database API functionality
- 🔗 Search endpoint validation

*Note: API tests require the server to be running (`python main_api.py`)*

### 6. WebSocket Functionality (Optional)
- 🔗 WebSocket connection testing
- 🔗 Real-time search functionality
- 🔗 Message handling

*Note: WebSocket tests require the server to be running*

## Test Results

The test suite provides detailed output showing:
- ✅ Passed tests (core functionality working)
- ❌ Failed tests (issues that need attention)
- ⏭️ Skipped tests (optional components not running)
- ⚠️ Partial tests (working but with limitations)

## Previous Test Files (Removed)

The following redundant test files have been consolidated:
- `test_pipeline.py` → Integrated into comprehensive suite
- `test_database.py` → Integrated into comprehensive suite  
- `test_api_endpoints.py` → Integrated into comprehensive suite
- `test_api_websocket.py` → Integrated into comprehensive suite
- `tests/test_import.py` → Removed (simple import check)
- `tests/test_job_parser.py` → Integrated into comprehensive suite
- `tests/tests.py` → Removed (external service test)
- `tests/integrations.py` → Removed (empty file)
- `tests/tests.ipynb` → Removed (notebook duplicate)

## Benefits of Consolidation

1. **Single Entry Point**: One command tests everything
2. **Comprehensive Coverage**: All functionality tested together
3. **Better Reporting**: Clear summary of all test results
4. **Easier Maintenance**: One file to update instead of many
5. **Dependency Handling**: Smart skipping of optional components
6. **Clean Output**: Organized, easy-to-read test results

## Example Output

```
🧪 RoleRadar - Comprehensive Test Suite
============================================================

1️⃣ Testing Database Operations
----------------------------------------
✅ Database initialized successfully
✅ Job insertion: Success
✅ Duplicate detection: Job exists = True
✅ Search functionality: Found 1 jobs
✅ Statistics: 3 total jobs, 3 companies
✅ Job retrieval: Retrieved 3 jobs

[... other tests ...]

============================================================
🎉 TEST SUMMARY
============================================================

Test Results:
  ✅ Database: PASS
  ✅ Migration: PASS  
  ✅ Pipeline: PASS
  ✅ Parser: PASS
  ⏭️ Api: SKIP
  ⏭️ Websocket: SKIP

Overall Results:
  Total Tests: 6
  Passed: 4
  Failed: 0
  Skipped/Partial: 2

🎊 All core tests passed! RoleRadar is working correctly.
```

## Running Specific Components

While the comprehensive test runs everything, you can still test specific components by starting the API server first:

```bash
# Terminal 1: Start the API server
python main_api.py

# Terminal 2: Run comprehensive tests (will include API tests)
python test_comprehensive.py
```

This ensures complete coverage including the optional API and WebSocket functionality.
