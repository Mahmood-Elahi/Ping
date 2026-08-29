#!/usr/bin/env python
"""
Test script to verify Windows async Playwright fix
"""
import sys
import asyncio

import pytest

from src.utils.job_search_pipeline import run_job_search_async

# Apply Windows fix
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

@pytest.mark.asyncio
async def test_async_search():
    """Test async job search with Windows compatibility"""
    try:
        print("🧪 Testing async job search with Windows compatibility...")
        
        # Run a small test search
        result = await run_job_search_async(
            keywords="Python Developer",
            locations=["Netherlands"],
            max_jobs=1  # Very small test
        )
        
        print(f"✅ Test completed successfully!")
        print(f"📊 Result: {result}")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_async_search())
