"""
Job Search Pipeline - Complete workflow for finding and processing job listings.

This module implements both sync and async job search pipelines using direct scrapers:
1. Use LinkedIn scraper to search for jobs directly
2. Extract job details with the scraper
3. Save results to JSON and Database
4. Avoid duplicate jobs using database checks

Supports both synchronous and asynchronous execution modes for different use cases:
- Sync: For CLI scripts and standalone execution
- Async: For FastAPI background tasks and existing event loops
"""
import json
import sys
import os
import time
import asyncio
from typing import Any, Callable, Dict, List, Optional

from src.utils.file_utils import load_config
from src.utils.job_database import JobDatabase
# Don't import scrapers at module level to avoid potential initialization issues
# They will be imported dynamically when needed

# Load configuration
jobsearch_config = load_config("config/jobsearch_config.yaml")
file_config = load_config("config/file_config.yaml")

class JobSearchPipeline:
    """
    Complete job search pipeline using direct scrapers.
    Supports both synchronous and asynchronous execution modes.
    """
    
    def __init__(self,
                 keywords: str, 
                 locations: List[str] = None, 
                 job_type: str = None, 
                 experience_level: str = None,
                 max_jobs_per_site: int = 3,
                 output_dir: str = "jobs",
                 scrapers: List[str] = None,
                 use_database: bool = True,
                 async_mode: bool = False,
                 company_careers_scanner: Any = None,
                 company_careers_job_filter: Optional[
                     Callable[[Any], Optional[str]]
                 ] = None,
                 company_careers_location_filter: Optional[
                     Callable[[Any], Optional[str]]
                 ] = None,
                 company_careers_ranked_writer: Optional[Callable[..., None]] = None,
                 company_careers_diagnostics_builder: Optional[Callable[..., Dict[str, Any]]] = None,
                 company_careers_report_processor: Optional[Callable[[Any], Any]] = None):
        """
        Initialize the job search pipeline.
        
        Args:
            keywords: Job title or keywords to search for
            locations: List of locations to search in
            job_type: Type of job (full-time, contract, etc)
            experience_level: Experience level required
            max_jobs_per_site: Maximum number of jobs to collect per site
            output_dir: Directory to save results
            scrapers: List of scrapers to use (linkedin, indeed, glassdoor)
            use_database: Whether to save jobs to database and check for duplicates
            async_mode: Whether to use async scrapers (for FastAPI/event loop integration)
            company_careers_scanner: Optional injected direct-company scanner
            company_careers_job_filter: Optional opt-in title/filter callback
            company_careers_location_filter: Optional opt-in location callback
            company_careers_ranked_writer: Optional opt-in ranked-artifact writer
            company_careers_diagnostics_builder: Optional normalized-job diagnostics callback
            company_careers_report_processor: Optional report hook run before artifacts
        """
        self.keywords = keywords
        self.locations = locations or jobsearch_config.get('locations', ["remote"])
        self.job_type = job_type or jobsearch_config.get('job_type', "full-time")
        self.experience_level = experience_level or jobsearch_config.get('experience_level', "mid-level")
        self.max_jobs_per_site = max_jobs_per_site
        self.output_dir = output_dir
        self.scrapers = scrapers or ["linkedin"]  # Default to LinkedIn
        self.use_database = use_database
        self.async_mode = async_mode
        self.company_careers_output_paths = None
        self.company_careers_scanner = company_careers_scanner
        self.company_careers_job_filter = company_careers_job_filter
        self.company_careers_location_filter = company_careers_location_filter
        self.company_careers_ranked_writer = company_careers_ranked_writer
        self.company_careers_diagnostics_builder = company_careers_diagnostics_builder
        self.company_careers_report_processor = company_careers_report_processor
        self.company_careers_processing_result = None
        self.company_careers_report = None
        self.company_careers_summary = None
        
        # Initialize database connection
        self.db = JobDatabase() if use_database else None
        if self.db:
            print("[INIT] Database connection established")
        
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)
        
        # Initialize scrapers (sync mode only in __init__)
        self.linkedin_scraper = None
        if not async_mode and "linkedin" in self.scrapers:
            print("[INIT] Initializing LinkedIn scraper (sync mode)...")
            try:
                from src.scraper.search.linkedin_scraper.scraper import LinkedInScraperSync
                self.linkedin_scraper = LinkedInScraperSync(headless=True)
                print("[SUCCESS] LinkedIn scraper initialized")
            except Exception as e:
                print(f"[ERROR] Failed to initialize LinkedIn scraper: {e}")
                self.linkedin_scraper = None

    def _company_careers_job_to_database_record(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Map normalized company-careers jobs to the existing database schema aliases."""
        return {
            "job_id": job.get("job_id"),
            "source_url": job.get("url"),
            "source": job.get("source", "company_careers"),
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "job_title": job.get("title"),
            "company_name": job.get("company"),
            "job_description": job.get("description"),
            "job_location": job.get("location"),
            "date_posted": job.get("updated_at"),
            "job_insights": {
                "departments": job.get("departments", []),
                "offices": job.get("offices", []),
                "company_careers_job_id": job.get("job_id"),
                "child_brands": job.get("child_brands", []),
                "raw_metadata": job.get("raw_metadata", {}),
            },
            "apply_info": {"apply_url": job.get("url")},
        }

    def _company_careers_output_paths(self) -> Dict[str, str]:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        return {
            "coverage_json": os.path.join(self.output_dir, f"company_careers_coverage_{timestamp}.json"),
            "coverage_md": os.path.join(self.output_dir, f"company_careers_coverage_{timestamp}.md"),
            "coverage_csv": os.path.join(self.output_dir, f"company_careers_coverage_{timestamp}.csv"),
            "failures_json": os.path.join(self.output_dir, f"company_careers_failures_{timestamp}.json"),
            "ranked_json": os.path.join(self.output_dir, f"company_careers_ranked_{timestamp}.json"),
            "ranked_csv": os.path.join(self.output_dir, f"company_careers_ranked_{timestamp}.csv"),
            "jobs_json": os.path.join(self.output_dir, f"job_postings_{timestamp}.json"),
            "diagnostics_json": os.path.join(self.output_dir, f"student_search_diagnostics_{timestamp}.json"),
        }

    def _write_company_careers_report(self, report, paths: Dict[str, str]) -> None:
        with open(paths["coverage_json"], "w", encoding="utf-8") as file:
            json.dump(report.to_dict(), file, indent=2, ensure_ascii=False)
        from src.scraper.company_careers.coverage_report import write_coverage_artifacts

        write_coverage_artifacts(
            report,
            markdown_path=paths["coverage_md"],
            csv_path=paths["coverage_csv"],
            failures_json_path=paths["failures_json"],
            jobs_path=paths.get("jobs_json") if not self.use_database else None,
            ranked_jobs_path=paths["ranked_json"],
            coverage_json_path=paths["coverage_json"],
        )
        ranked_writer = self.company_careers_ranked_writer
        if ranked_writer is None:
            from src.scraper.company_careers.relevance import (
                write_ranked_jobs_artifacts,
            )

            ranked_writer = write_ranked_jobs_artifacts

        ranked_writer(
            report.jobs,
            json_path=paths["ranked_json"],
            csv_path=paths["ranked_csv"],
            keywords=self.keywords,
            locations=self.locations,
        )

    def _search_company_careers(self) -> List[Dict[str, Any]]:
        """Run the reusable company career-site scanner and persist compatible jobs."""
        print(f"\n[SITE] Searching company career sites...")

        try:
            from src.scraper.company_careers import CompanyCareersScanner
        except Exception as e:
            print(f"  [ERROR] Failed to import company careers scanner: {e}")
            return []

        scanner = self.company_careers_scanner or CompanyCareersScanner()
        report = scanner.scan(
            keywords=self.keywords,
            locations=self.locations,
            max_jobs_per_company=self.max_jobs_per_site,
            job_filter=self.company_careers_job_filter,
            location_filter=self.company_careers_location_filter,
        )
        self.company_careers_report = report
        if self.company_careers_report_processor is not None:
            self.company_careers_processing_result = (
                self.company_careers_report_processor(report)
            )
        output_paths = self._company_careers_output_paths()
        self.company_careers_output_paths = output_paths
        self._write_company_careers_report(report, output_paths)

        for coverage in report.coverage():
            print(
                "  [COVERAGE] "
                f"{coverage['company']}: {coverage['status']} "
                f"({coverage['matched_count']}/{coverage['fetched_count']} matching)"
            )
            if coverage.get("error"):
                print(f"    [ERROR] {coverage['error']}")

        results = []
        successful_saves = 0
        skipped_existing = 0
        failed_saves = 0

        for job_record in report.jobs:
            job = job_record.to_dict()
            db_job = self._company_careers_job_to_database_record(job)
            source_url = db_job.get("source_url")

            if self.db:
                job_title = db_job.get("job_title")
                company_name = db_job.get("company_name")
                already_exists = (
                    self.db.job_exists(source_url=source_url)
                    if source_url
                    else self.db.job_exists(
                        job_title=job_title,
                        company_name=company_name,
                    )
                )
                if already_exists:
                    skipped_existing += 1
                    continue

                feedback = self.db.add_job_with_immediate_feedback(db_job)
                if feedback["success"]:
                    successful_saves += 1
                    results.append({"saved": True, "source": "company_careers"})
                else:
                    failed_saves += 1
            else:
                results.append(job)

        exclusion_counts: Dict[str, int] = {}
        for scan_result in report.results:
            for reason, count in scan_result.exclusion_counts.items():
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + count
        processing_summary = getattr(
            self.company_careers_processing_result, "summary", None
        )
        date_parse_failures = list(
            getattr(self.company_careers_processing_result, "date_parse_failures", ())
        )
        diagnostics = None
        if self.company_careers_diagnostics_builder is not None:
            diagnostics = self.company_careers_diagnostics_builder(
                report.normalized_jobs,
                locations=self.locations,
            )
            if processing_summary is not None:
                diagnostics["freshness"] = dict(processing_summary)
                diagnostics["date_parse_error_count"] = len(date_parse_failures)
                diagnostics["date_parse_failures"] = date_parse_failures
            with open(output_paths["diagnostics_json"], "w", encoding="utf-8") as file:
                json.dump(diagnostics, file, indent=2, ensure_ascii=False)

        self.company_careers_summary = {
            "companies_checked": len(report.results),
            "canonical_sources_checked": len(report.results),
            "mapped_child_brands_covered": report.original_coverage.get(
                "mapped_child_brands_covered", 0
            ),
            "total_original_companies_covered": report.original_coverage.get(
                "total_original_companies_covered", 0
            ),
            "original_list_total": report.original_coverage.get("original_list_total", 0),
            "jobs_fetched": sum(item.fetched_count for item in report.results),
            "jobs_retained": len(report.jobs),
            "scan_health_counts": {
                state: sum(1 for item in report.results if item.scan_health == state)
                for state in ("complete", "incomplete", "failed")
            },
            "exclusion_counts": exclusion_counts,
            "database_inserts": successful_saves,
            "existing_duplicates": skipped_existing,
            "database_failures": failed_saves,
            "coverage_report_path": output_paths["coverage_md"],
            "ranked_results_path": output_paths["ranked_json"],
            "diagnostics_path": output_paths["diagnostics_json"] if diagnostics is not None else None,
            "diagnostics": diagnostics,
        }
        if processing_summary is not None:
            self.company_careers_summary["freshness"] = dict(processing_summary)
            self.company_careers_summary["date_parse_error_count"] = len(
                date_parse_failures
            )
            self.company_careers_summary["date_parse_failures"] = date_parse_failures

        print(
            "  [SUMMARY] Company careers legacy local-DB persistence: "
            f"{successful_saves} saved, {skipped_existing} skipped, "
            f"{failed_saves} failed, coverage report: {output_paths['coverage_json']}"
        )
        print(f"  [SUMMARY] Coverage summary: {output_paths['coverage_md']}")
        print(f"  [SUMMARY] Failure summary: {output_paths['failures_json']}")
        print(f"  [SUMMARY] Ranked review: {output_paths['ranked_json']}")
        if diagnostics is not None:
            print(f"  [SUMMARY] Search diagnostics: {output_paths['diagnostics_json']}")
        return results
    
    async def _initialize_scrapers_async(self):
        """Initialize scrapers asynchronously with proper error handling"""
        # Apply Windows async fix before initializing scrapers
        if sys.platform == "win32":
            try:
                # Ensure ProactorEventLoop policy is set for subprocess compatibility
                current_policy = asyncio.get_event_loop_policy()
                if not isinstance(current_policy, asyncio.WindowsProactorEventLoopPolicy):
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    print("[WINDOWS] Applied Windows async compatibility fix")
            except Exception as e:
                print(f"[WARN] Could not apply Windows async fix: {e}")
        
        if "linkedin" in self.scrapers:
            print("[INIT] Initializing LinkedIn scraper (async mode)...")
            try:
                # Import the async scraper dynamically to avoid module-level issues
                from src.scraper.search.linkedin_scraper.scraper import LinkedInScraper
                self.linkedin_scraper = LinkedInScraper(headless=True)
                print("[SUCCESS] LinkedIn scraper initialized (async)")
            except Exception as e:
                print(f"[ERROR] Failed to initialize LinkedIn scraper: {e}")
                self.linkedin_scraper = None
    
    def _initialize_scrapers_sync(self):
        """Initialize scrapers synchronously with proper error handling"""
        if "linkedin" in self.scrapers:
            print("[INIT] Initializing LinkedIn scraper (sync mode)...")
            try:
                # Use the sync version of the Playwright-based scraper
                from src.scraper.search.linkedin_scraper.scraper import LinkedInScraperSync
                self.linkedin_scraper = LinkedInScraperSync(headless=True)
                print("[SUCCESS] LinkedIn scraper initialized (Playwright sync)")
            except Exception as e:
                print(f"[ERROR] Failed to initialize LinkedIn scraper: {e}")
                self.linkedin_scraper = None
    
    def search_jobs(self) -> List[Dict[str, Any]]:
        """
        Execute the complete job search pipeline using direct scrapers.
        
        Returns:
            List of job postings with complete details
        """
        start_time = time.time()
        print("[START] Starting job search pipeline...")
        all_results = []
        
        # Initialize scrapers if not already done
        if not hasattr(self, 'linkedin_scraper') or self.linkedin_scraper is None:
            self._initialize_scrapers_sync()
        
        # LinkedIn scraping
        if "linkedin" in self.scrapers and self.linkedin_scraper:
            print(f"\n[SITE] Searching LinkedIn jobs...")
            
            for location in self.locations:
                print(f"  [LOCATION] Location: {location}")
                
                try:
                    # Collect job links using LinkedIn scraper
                    print(f"  [SEARCH] Searching LinkedIn for: {self.keywords}")
                    
                    # Calculate number of pages based on max_jobs_per_site
                    # Assuming ~25 jobs per page, calculate pages needed
                    max_pages = max(1, (self.max_jobs_per_site + 24) // 25)
                    print(f"  [PAGES] Will scrape {max_pages} page(s) to get up to {self.max_jobs_per_site} jobs")
                    
                    job_links = self.linkedin_scraper.collect_job_links(
                        keywords=self.keywords,
                        location=location,
                        max_pages=max_pages
                    )
                    
                    print(f"  [LINKS] Found {len(job_links)} job links")
                    
                    # Limit the number of jobs
                    if len(job_links) > self.max_jobs_per_site:
                        job_links = job_links[:self.max_jobs_per_site]
                        print(f"  [LIMIT] Limited to {self.max_jobs_per_site} jobs")                    # Get job details for each link with immediate database updates
                    location_results = []
                    successful_saves = 0
                    skipped_existing = 0
                    failed_scrapes = 0
                    
                    for i, job_url in enumerate(job_links):
                        print(f"  [SCRAPE] Processing job {i+1}/{len(job_links)}: {job_url}")
                        
                        # Check if job already exists in database
                        if self.db and self.db.job_exists(source_url=job_url):
                            print(f"    ⏭️  Job already exists in database, skipping...")
                            skipped_existing += 1
                            continue
                        
                        try:
                            job_details = self.linkedin_scraper.get_job_details(job_url)
                            if job_details:
                                # Add metadata
                                job_details['source'] = 'linkedin'
                                job_details['source_url'] = job_url
                                job_details['scraped_at'] = time.strftime("%Y-%m-%d %H:%M:%S")
                                  # Save to database immediately with detailed feedback
                                if self.db:
                                    feedback = self.db.add_job_with_immediate_feedback(job_details)
                                    if feedback["success"]:
                                        successful_saves += 1
                                        print(f"    💾 {feedback['message']} ({feedback['duration_ms']}ms)")
                                    else:
                                        print(f"    ⚠️  {feedback['message']} ({feedback['duration_ms']}ms)")
                                        failed_scrapes += 1
                                else:
                                    # If no database, add to results for JSON output
                                    location_results.append(job_details)
                                
                                job_title = job_details.get('job_title', job_details.get('title', 'N/A'))
                                company_name = job_details.get('company_name', job_details.get('company', 'N/A'))
                                print(f"    ✅ {job_title} at {company_name}")
                            else:
                                print(f"    ❌ Failed to get job details")
                                failed_scrapes += 1
                        except Exception as e:
                            print(f"    ❌ Error getting job details: {str(e)}")
                            failed_scrapes += 1
                        
                        # Small delay between requests to avoid rate limiting
                        time.sleep(2)
                    
                    # Print summary for this location
                    print(f"  [SUMMARY] Location {location}: {successful_saves} saved, {skipped_existing} skipped, {failed_scrapes} failed")
                    
                    # For reporting purposes, create fake result entries for saved jobs
                    if self.db and successful_saves > 0:
                        # Add placeholder results to represent saved jobs
                        location_results = [{"saved": True}] * successful_saves
                    
                    all_results.extend(location_results)
                    print(f"  [SUCCESS] Found {len(location_results)} jobs in {location}")
                    
                except Exception as e:
                    print(f"  [ERROR] Error searching LinkedIn in {location}: {str(e)}")
                
                # Delay between locations
                time.sleep(3)
            
            print(f"  [TOTAL] LinkedIn total: {len([r for r in all_results if r.get('source') == 'linkedin'])} jobs")

        if "company_careers" in self.scrapers:
            all_results.extend(self._search_company_careers())

        # TODO: Add other scrapers here (Indeed, Glassdoor, etc.)
        
        # Only save JSON if database is not used or explicitly requested
        if not self.use_database and all_results:
            output_file = (
                self.company_careers_output_paths.get("jobs_json")
                if self.company_careers_output_paths
                else os.path.join(self.output_dir, f"job_postings_{time.strftime('%Y%m%d_%H%M%S')}.json")
            )
            
            print(f"\n💾 Saving {len(all_results)} job postings to {output_file}")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)
        elif self.use_database:
            print(f"\n💾 Jobs saved to database only (no JSON output)")
        
        print(f"✅ Job search completed. Found {len(all_results)} job postings!")
          # Display final statistics with database breakdown
        if self.db:
            stats = self.db.get_stats()
            print(f"\n📊 Final Database Statistics:")
            print(f"  Total jobs in database: {stats['total_jobs']}")
            print(f"  Jobs by source:")
            for source in stats['by_source']:
                print(f"    - {source['source']}: {source['count']} jobs")
            print(f"  Top companies:")
            for company in stats['top_companies'][:3]:
                print(f"    - {company['company']}: {company['count']} jobs")
        
        # Display session summary
        print(f"\n📈 Session Summary:")
        print(f"  Jobs scraped this session: {len(all_results)}")
        print(f"  Total processing time: {time.time() - start_time:.1f} seconds")
        if all_results:
            print(f"  Average time per job: {(time.time() - start_time) / len(all_results):.1f} seconds")
        
        # Close scrapers and database
        if self.linkedin_scraper:
            try:
                self.linkedin_scraper.close()
            except:
                pass
        
        if self.db:
            try:
                self.db.close()
            except:
                pass
        
        return all_results
    
    async def search_jobs_async(self) -> List[Dict[str, Any]]:
        """
        Execute the complete job search pipeline using direct scrapers (async version).
        
        Returns:
            List of job postings with complete details
        """
        start_time = time.time()
        print("[START] Starting job search pipeline (async mode)...")
        all_results = []
        
        # Initialize scrapers
        await self._initialize_scrapers_async()
        
        # LinkedIn scraping
        if "linkedin" in self.scrapers and self.linkedin_scraper:
            print(f"\n[SITE] Searching LinkedIn jobs...")
            
            for location in self.locations:
                print(f"  [LOCATION] Location: {location}")
                
                try:
                    # Collect job links using LinkedIn scraper
                    print(f"  [SEARCH] Searching LinkedIn for: {self.keywords}")
                    
                    # Calculate number of pages based on max_jobs_per_site
                    # Assuming ~25 jobs per page, calculate pages needed
                    max_pages = max(1, (self.max_jobs_per_site + 24) // 25)
                    print(f"  [PAGES] Will scrape {max_pages} page(s) to get up to {self.max_jobs_per_site} jobs")
                    
                    # Use appropriate method based on scraper type
                    if self.async_mode and hasattr(self.linkedin_scraper, 'collect_job_links'):
                        # Async scraper
                        job_links = await self.linkedin_scraper.collect_job_links(
                            keywords=self.keywords,
                            location=location,
                            max_pages=max_pages
                        )
                    else:
                        # Sync scraper fallback
                        job_links = self.linkedin_scraper.collect_job_links(
                            keywords=self.keywords,
                            location=location,
                            max_pages=max_pages
                        )
                    
                    print(f"  [LINKS] Found {len(job_links)} job links")
                    
                    # Limit the number of jobs
                    if len(job_links) > self.max_jobs_per_site:
                        job_links = job_links[:self.max_jobs_per_site]
                        print(f"  [LIMIT] Limited to {self.max_jobs_per_site} jobs")
                    
                    # Get job details for each link with immediate database updates
                    location_results = []
                    successful_saves = 0
                    skipped_existing = 0
                    failed_scrapes = 0
                    
                    for i, job_url in enumerate(job_links):
                        print(f"  [SCRAPE] Processing job {i+1}/{len(job_links)}: {job_url}")
                        
                        # Check if job already exists in database
                        if self.db and self.db.job_exists(source_url=job_url):
                            print(f"    ⏭️  Job already exists in database, skipping...")
                            skipped_existing += 1
                            continue
                        
                        try:
                            # Use appropriate method based on scraper type
                            if self.async_mode and hasattr(self.linkedin_scraper, 'collect_job_links'):
                                # Async scraper
                                job_details = await self.linkedin_scraper.get_job_details(job_url)
                            else:
                                # Sync scraper fallback
                                job_details = self.linkedin_scraper.get_job_details(job_url)
                                
                            if job_details:
                                # Add metadata
                                job_details['source'] = 'linkedin'
                                job_details['source_url'] = job_url
                                job_details['scraped_at'] = time.strftime("%Y-%m-%d %H:%M:%S")
                                
                                # Save to database immediately with detailed feedback
                                if self.db:
                                    feedback = self.db.add_job_with_immediate_feedback(job_details)
                                    if feedback["success"]:
                                        successful_saves += 1
                                        print(f"    💾 {feedback['message']} ({feedback['duration_ms']}ms)")
                                    else:
                                        print(f"    ⚠️  {feedback['message']} ({feedback['duration_ms']}ms)")
                                        failed_scrapes += 1
                                else:
                                    # If no database, add to results for JSON output
                                    location_results.append(job_details)
                                
                                job_title = job_details.get('job_title', job_details.get('title', 'N/A'))
                                company_name = job_details.get('company_name', job_details.get('company', 'N/A'))
                                print(f"    ✅ {job_title} at {company_name}")
                            else:
                                print(f"    ❌ Failed to get job details")
                                failed_scrapes += 1
                        except Exception as e:
                            print(f"    ❌ Error getting job details: {str(e)}")
                            failed_scrapes += 1
                        
                        # Small delay between requests to avoid rate limiting
                        if self.async_mode:
                            await asyncio.sleep(2)
                        else:
                            time.sleep(2)
                    
                    # Print summary for this location
                    print(f"  [SUMMARY] Location {location}: {successful_saves} saved, {skipped_existing} skipped, {failed_scrapes} failed")
                    
                    # For reporting purposes, create fake result entries for saved jobs
                    if self.db and successful_saves > 0:
                        # Add placeholder results to represent saved jobs
                        location_results = [{"saved": True}] * successful_saves
                    
                    all_results.extend(location_results)
                    print(f"  [SUCCESS] Found {len(location_results)} jobs in {location}")
                    
                except Exception as e:
                    print(f"  [ERROR] Error searching LinkedIn in {location}: {str(e)}")
                
                # Delay between locations
                if self.async_mode:
                    await asyncio.sleep(3)
                else:
                    time.sleep(3)
            
            print(f"  [TOTAL] LinkedIn total: {len([r for r in all_results if r.get('source') == 'linkedin'])} jobs")

        if "company_careers" in self.scrapers:
            all_results.extend(self._search_company_careers())

        # TODO: Add other scrapers here (Indeed, Glassdoor, etc.)
        
        # Only save JSON if database is not used or explicitly requested
        if not self.use_database and all_results:
            output_file = (
                self.company_careers_output_paths.get("jobs_json")
                if self.company_careers_output_paths
                else os.path.join(self.output_dir, f"job_postings_{time.strftime('%Y%m%d_%H%M%S')}.json")
            )
            
            print(f"\n💾 Saving {len(all_results)} job postings to {output_file}")
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(all_results, f, indent=2, ensure_ascii=False)
        elif self.use_database:
            print(f"\n💾 Jobs saved to database only (no JSON output)")
        
        print(f"✅ Job search completed. Found {len(all_results)} job postings!")
        
        # Display final statistics with database breakdown
        if self.db:
            stats = self.db.get_stats()
            print(f"\n📊 Final Database Statistics:")
            print(f"  Total jobs in database: {stats['total_jobs']}")
            print(f"  Jobs by source:")
            for source in stats['by_source']:
                print(f"    - {source['source']}: {source['count']} jobs")
            print(f"  Top companies:")
            for company in stats['top_companies'][:3]:
                print(f"    - {company['company']}: {company['count']} jobs")
        
        # Display session summary
        print(f"\n📈 Session Summary:")
        print(f"  Jobs scraped this session: {len(all_results)}")
        print(f"  Total processing time: {time.time() - start_time:.1f} seconds")
        if all_results:
            print(f"  Average time per job: {(time.time() - start_time) / len(all_results):.1f} seconds")
        
        # Close scrapers and database
        if self.linkedin_scraper:
            try:
                if self.async_mode and hasattr(self.linkedin_scraper, 'close'):
                    await self.linkedin_scraper.close()
                elif hasattr(self.linkedin_scraper, 'close'):
                    self.linkedin_scraper.close()
            except:
                pass
        
        if self.db:
            try:
                self.db.close()
            except:
                pass
        
        return all_results
    
    def export_database_to_json(self, output_file: str = None, limit: int = None) -> str:
        """
        Export database contents to JSON file.
        
        Args:
            output_file: Path to output JSON file. If None, uses timestamped filename
            limit: Maximum number of jobs to export. If None, exports all jobs
            
        Returns:
            Path to the created JSON file
        """
        if not self.db:
            raise ValueError("Database is not initialized. Cannot export to JSON.")
        
        # Get jobs from database
        jobs = self.db.get_jobs(limit=limit or 10000)  # Default to large limit
        
        if not jobs:
            print("⚠️  No jobs found in database to export")
            return None
        
        # Generate output filename if not provided
        if not output_file:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            output_file = os.path.join(self.output_dir, f"db_export_{timestamp}.json")
        
        # Convert database rows to JSON-serializable format
        json_jobs = []
        for job in jobs:
            job_dict = dict(job)  # Convert sqlite3.Row to dict
            
            # Parse JSON fields back to objects
            for field in ['job_insights', 'apply_info', 'company_info', 'hiring_team', 'related_jobs']:
                if job_dict.get(field):
                    try:
                        job_dict[field] = json.loads(job_dict[field])
                    except (json.JSONDecodeError, TypeError):
                        pass  # Keep as string if not valid JSON
            
            json_jobs.append(job_dict)
        
        # Write to JSON file
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(json_jobs, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Exported {len(json_jobs)} jobs from database to {output_file}")
        return output_file
    def search_and_process(self, export_to_json: bool = False) -> str:
        """
        Run the search pipeline and optionally export to JSON (sync version).
        
        Args:
            export_to_json: Whether to export database contents to JSON after scraping
            
        Returns:
            Path to the JSON file if exported, otherwise None
        """
        results = self.search_jobs()
        
        # Only create JSON if requested or if database is not used
        if export_to_json and self.db:
            return self.export_database_to_json()
        elif not self.use_database and results:
            # Save the results to the standard job_postings.json file
            standard_output = os.path.join(self.output_dir, "job_postings.json")
            with open(standard_output, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            return standard_output
        
        return None

    async def search_and_process_async(self, export_to_json: bool = False) -> str:
        """
        Run the search pipeline and optionally export to JSON (async version).
        
        Args:
            export_to_json: Whether to export database contents to JSON after scraping
            
        Returns:
            Path to the JSON file if exported, otherwise None
        """
        results = await self.search_jobs_async()
        
        # Only create JSON if requested or if database is not used
        if export_to_json and self.db:
            return self.export_database_to_json()
        elif not self.use_database and results:
            # Save the results to the standard job_postings.json file
            standard_output = os.path.join(self.output_dir, "job_postings.json")
            with open(standard_output, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            return standard_output
        
        return None

def run_job_search(keywords: str, 
                  locations: List[str] = None,
                  job_type: str = None,
                  experience_level: str = None,
                  max_jobs: int = 10,
                  scrapers: List[str] = None,
                  use_database: bool = True,
                  export_to_json: bool = False) -> str:    
    """
    Convenience function to run the job search pipeline.
    
    Args:
        keywords: Job title or keywords to search for
        locations: List of locations to search in
        job_type: Type of job (full-time, contract, etc)
        experience_level: Experience level required
        max_jobs: Maximum number of jobs to collect total
        scrapers: List of scrapers to use
        use_database: Whether to save jobs to database and check for duplicates
        export_to_json: Whether to export database to JSON after scraping
        
    Returns:
        Path to the output JSON file if exported, otherwise None
    """
    # Calculate max jobs per site based on total and number of scrapers
    scrapers = scrapers or ["linkedin"]
    max_jobs_per_site = max(1, max_jobs // len(scrapers))    
    pipeline = JobSearchPipeline(
        keywords=keywords,
        locations=locations,
        job_type=job_type,
        experience_level=experience_level,
        max_jobs_per_site=max_jobs_per_site,
        scrapers=scrapers,
        use_database=use_database
    )
    
    return pipeline.search_and_process(export_to_json=export_to_json)

async def run_job_search_async(keywords: str, 
                              locations: List[str] = None,
                              job_type: str = None,
                              experience_level: str = None,
                              max_jobs: int = 10,
                              scrapers: List[str] = None,
                              use_database: bool = True,
                              export_to_json: bool = False) -> str:    
    """
    Async convenience function to run the job search pipeline.
    
    Args:
        keywords: Job title or keywords to search for
        locations: List of locations to search in
        job_type: Type of job (full-time, contract, etc)
        experience_level: Experience level required
        max_jobs: Maximum number of jobs to collect total
        scrapers: List of scrapers to use
        use_database: Whether to save jobs to database and check for duplicates
        export_to_json: Whether to export database to JSON after scraping
        
    Returns:
        Path to the output JSON file if exported, otherwise None
    """
    # Calculate max jobs per site based on total and number of scrapers
    scrapers = scrapers or ["linkedin"]
    max_jobs_per_site = max(1, max_jobs // len(scrapers))
    
    pipeline = JobSearchPipeline(
        keywords=keywords,
        locations=locations,
        job_type=job_type,
        experience_level=experience_level,
        max_jobs_per_site=max_jobs_per_site,
        scrapers=scrapers,
        use_database=use_database,
        async_mode=True  # Enable async mode
    )
    
    return await pipeline.search_and_process_async(export_to_json=export_to_json)

def export_jobs_to_json(output_file: str = None, limit: int = None) -> str:
    """
    Standalone function to export database jobs to JSON.
    
    Args:
        output_file: Path to output JSON file. If None, uses timestamped filename
        limit: Maximum number of jobs to export. If None, exports all jobs
        
    Returns:
        Path to the created JSON file
    """
    db = JobDatabase()
    try:
        # Create temporary pipeline just for export functionality
        pipeline = JobSearchPipeline("", use_database=True)
        pipeline.db = db
        return pipeline.export_database_to_json(output_file, limit)
    finally:
        db.close()
